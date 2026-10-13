"""Deciding which files to index, and reading them safely.

This is the boundary between "a directory someone else controls" and the rest
of the pipeline. Two rules hold here:

1. **Never leave the checkout.** Symlinks are not followed and every path is
   verified to resolve inside the root, so a repository cannot point us at
   /etc/passwd, at another tenant's checkout, or at a symlink loop.
2. **Never accept unbounded input.** Size, count and line-length caps are
   applied before anything is parsed, and every rejection is counted.
"""

import hashlib
import os
import posixpath
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from app.knowledge_graph.limits import DEFAULT_LIMITS, Limits, SkipReason

LANGUAGE_BY_EXTENSION: Mapping[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

#: Directories that never contain first-party source.
IGNORED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "dist", "build", "out", "coverage",
    "vendor", "venv", ".venv", "env", "__pycache__", ".next", ".nuxt", ".turbo",
    ".cache", "target", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".idea", ".vscode", "site-packages", ".gradle", "bin", "obj",
})

#: Filenames that are build output rather than source.
GENERATED_SUFFIXES = (".min.js", ".min.mjs", ".bundle.js", ".d.ts", ".map", "_pb.py", "_pb2.py")


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """A candidate file: metadata only, contents not yet read."""

    path: Path  # absolute
    relative_path: str  # repo-relative, posix separators
    language: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A file that has been read and is safe to parse."""

    relative_path: str
    language: str
    content_hash: str
    source: bytes
    loc: int


@dataclass
class DiscoveryResult:
    files: list[DiscoveredFile] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    #: True when a repository-wide cap stopped the walk early.
    capped: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(file.size_bytes for file in self.files)


def detect_language(relative_path: str) -> str | None:
    return LANGUAGE_BY_EXTENSION.get(posixpath.splitext(relative_path)[1])


def is_generated(relative_path: str) -> bool:
    name = posixpath.basename(relative_path)
    return name.endswith(GENERATED_SUFFIXES)


def load_ignore_spec(root: Path) -> pathspec.PathSpec | None:
    gitignore = root / ".gitignore"
    if not gitignore.is_file() or gitignore.is_symlink():
        return None
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    return pathspec.PathSpec.from_lines("gitignore", lines)


def discover(root: Path, limits: Limits = DEFAULT_LIMITS) -> DiscoveryResult:
    """Every indexable file in a checkout, in a deterministic order."""
    resolved_root = root.resolve()
    spec = load_ignore_spec(resolved_root)
    result = DiscoveryResult()
    total_bytes = 0

    for path, relative_path in _walk(resolved_root, result):
        language = detect_language(relative_path)
        if language is None:
            continue  # not source we understand; not worth counting
        if is_generated(relative_path):
            result.skipped[SkipReason.GENERATED] += 1
            continue
        if spec is not None and spec.match_file(relative_path):
            result.skipped[SkipReason.GITIGNORED] += 1
            continue

        try:
            size = path.stat().st_size
        except OSError:
            result.skipped[SkipReason.UNREADABLE] += 1
            continue
        if limits.is_oversized(size):
            result.skipped[SkipReason.TOO_LARGE] += 1
            continue

        if len(result.files) >= limits.max_files or total_bytes + size > limits.max_total_bytes:
            result.skipped[SkipReason.REPO_CAP_REACHED] += 1
            result.capped = True
            break

        total_bytes += size
        result.files.append(
            DiscoveredFile(path=path, relative_path=relative_path, language=language, size_bytes=size)
        )

    result.files.sort(key=lambda file: file.relative_path)
    return result


def _walk(root: Path, result: DiscoveryResult) -> Iterator[tuple[Path, str]]:
    """Yield regular files under root, never following symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            if name in IGNORED_DIRECTORIES:
                result.skipped[SkipReason.IGNORED_DIRECTORY] += 1
            elif (current / name).is_symlink():
                result.skipped[SkipReason.SYMLINK] += 1
            else:
                kept.append(name)
        dirnames[:] = sorted(kept)

        for name in sorted(filenames):
            path = current / name
            if path.is_symlink():
                # Only counted for files we would otherwise have indexed.
                if detect_language(name):
                    result.skipped[SkipReason.SYMLINK] += 1
                continue
            yield path, path.relative_to(root).as_posix()


def resolve_within(root: Path, relative_path: str) -> Path | None:
    """Resolve a repo-relative path, or None if it escapes the checkout.

    Paths that arrive from outside (webhook payloads, API parameters) are
    attacker-influenced: "../../etc/passwd" and a symlinked leaf both read
    outside the repository unless checked here.
    """
    normalized = posixpath.normpath(relative_path)
    if normalized in ("", ".") or normalized.startswith(("/", "..")) or os.path.isabs(normalized):
        return None

    resolved_root = root.resolve()
    candidate = resolved_root / normalized
    if candidate.is_symlink():
        return None
    try:
        real = candidate.resolve()
    except OSError:
        return None
    if real == resolved_root or not real.is_relative_to(resolved_root):
        return None
    return candidate


def read_source(
    file: DiscoveredFile, limits: Limits = DEFAULT_LIMITS,
) -> tuple[SourceFile | None, SkipReason | None]:
    """Read a discovered file, or explain why it cannot be parsed.

    The content hash computed here is what makes incremental indexing cheap:
    a file whose hash is unchanged is never parsed again.
    """
    try:
        source = file.path.read_bytes()
    except OSError:
        return None, SkipReason.UNREADABLE

    if b"\x00" in source[:8192]:
        return None, SkipReason.BINARY
    if _longest_line(source) > limits.max_line_bytes:
        # Minified or generated output: parses slowly and tells us nothing.
        return None, SkipReason.MINIFIED

    return (
        SourceFile(
            relative_path=file.relative_path,
            language=file.language,
            content_hash=hashlib.sha256(source).hexdigest(),
            source=source,
            loc=source.count(b"\n") + 1,
        ),
        None,
    )


def _longest_line(source: bytes) -> int:
    longest = 0
    start = 0
    while (index := source.find(b"\n", start)) != -1:
        longest = max(longest, index - start)
        start = index + 1
    return max(longest, len(source) - start)
