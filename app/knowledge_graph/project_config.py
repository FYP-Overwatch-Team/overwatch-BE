"""Project settings that import resolution depends on.

Today an import like `@/features/landing` is treated as a third-party package,
because nothing reads the alias table in `tsconfig.json`. In the user's own
repository that is 57% of internal imports — the single biggest source of
missing edges in the current graph.

This module only *reads* configuration into plain data; turning a specifier
into a file is the linker's job (`link/imports.py`).

Everything here is untrusted input from the repository, so parsing is bounded:
files are size-capped, `extends` chains are depth-capped, and every referenced
path must resolve inside the checkout.
"""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.knowledge_graph.discovery import resolve_within

logger = structlog.get_logger("app.knowledge_graph.config")

#: Config files are small; anything larger is not a config we should parse.
MAX_CONFIG_BYTES = 256 * 1024
#: tsconfig files extend each other; stop before a malicious cycle bites.
MAX_EXTENDS_DEPTH = 5

TS_CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Resolution settings for one repository."""

    #: Repo-relative directory that non-relative TS imports resolve against.
    ts_base_url: str = ""
    #: TypeScript path aliases, e.g. {"@/*": ("./*",)} — patterns as written.
    ts_paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Repo-relative directories that are Python import roots, "" being the root.
    python_roots: tuple[str, ...] = ("",)


def load(root: Path) -> ProjectConfig:
    """Read whatever configuration the repository provides, tolerating absence."""
    base_url, paths = _load_typescript_paths(root)
    return ProjectConfig(
        ts_base_url=base_url,
        ts_paths=paths,
        python_roots=_load_python_roots(root),
    )


# --------------------------------------------------------------------------
# TypeScript / JavaScript
# --------------------------------------------------------------------------


def _load_typescript_paths(root: Path) -> tuple[str, Mapping[str, tuple[str, ...]]]:
    for name in TS_CONFIG_NAMES:
        config_path = root / name
        if not config_path.is_file() or config_path.is_symlink():
            continue
        options = _read_compiler_options(root, config_path, depth=0)
        if options is None:
            continue

        # `baseUrl` is relative to the file that declares it; we normalise to a
        # repo-relative directory so the linker deals in one kind of path.
        declared_base = options.get("baseUrl")
        base_dir = config_path.parent
        base_url = ""
        if isinstance(declared_base, str):
            resolved = resolve_within(root, _join_relative(root, base_dir, declared_base))
            base_url = resolved.relative_to(root.resolve()).as_posix() if resolved else ""

        raw_paths = options.get("paths")
        paths: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_paths, dict):
            for pattern, targets in raw_paths.items():
                if isinstance(pattern, str) and isinstance(targets, list):
                    values = tuple(t for t in targets if isinstance(t, str))
                    if values:
                        paths[pattern] = values
        return base_url, paths
    return "", {}


def _read_compiler_options(root: Path, config_path: Path, depth: int) -> dict | None:
    """compilerOptions for a tsconfig, merged with what it extends."""
    if depth > MAX_EXTENDS_DEPTH:
        logger.warning("tsconfig_extends_too_deep", path=str(config_path))
        return None

    data = _read_jsonc(config_path)
    if data is None:
        return None

    options: dict = {}
    extends = data.get("extends")
    if isinstance(extends, str):
        parent_path = _resolve_extends(root, config_path.parent, extends)
        if parent_path is not None:
            options |= _read_compiler_options(root, parent_path, depth + 1) or {}

    own = data.get("compilerOptions")
    if isinstance(own, dict):
        options |= own
    return options


def _resolve_extends(root: Path, base_dir: Path, extends: str) -> Path | None:
    """Resolve an `extends` target, but only inside the checkout.

    Package-style targets ("@tsconfig/node20/tsconfig.json") live in
    node_modules, which we do not index, so they are simply skipped.
    """
    if not extends.startswith("."):
        return None
    candidate = _join_relative(root, base_dir, extends)
    for suffix in ("", ".json"):
        resolved = resolve_within(root, candidate + suffix)
        if resolved is not None and resolved.is_file():
            return resolved
    return None


def _join_relative(root: Path, base_dir: Path, value: str) -> str:
    """Repo-relative posix path for `value` interpreted relative to base_dir."""
    resolved_root = root.resolve()
    prefix = base_dir.resolve().relative_to(resolved_root).as_posix()
    return f"{prefix}/{value}" if prefix not in ("", ".") else value


def _read_jsonc(path: Path) -> dict | None:
    """Parse JSON with comments and trailing commas, as tsconfig files use."""
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            logger.warning("config_too_large", path=str(path))
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    cleaned = _TRAILING_COMMA_RE.sub(r"\1", _strip_comments(text))
    try:
        data = json.loads(cleaned)
    except ValueError:
        logger.info("config_unparsable", path=str(path))
        return None
    return data if isinstance(data, dict) else None


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments, leaving string literals untouched."""
    out: list[str] = []
    index, length = 0, len(text)
    in_string = False

    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            out.append(char)
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            if newline == -1:
                break
            index = newline
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
        else:
            out.append(char)
            index += 1

    return "".join(out)


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def _load_python_roots(root: Path) -> tuple[str, ...]:
    """Directories Python imports resolve against: the repo root, plus a src layout."""
    roots = [""]
    if (root / "src").is_dir() and not (root / "src").is_symlink():
        roots.append("src")

    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and not pyproject.is_symlink():
        for declared in _declared_package_dirs(pyproject):
            if declared not in roots:
                roots.append(declared)
    return tuple(roots)


def _declared_package_dirs(pyproject: Path) -> list[str]:
    """`where`/`packages` entries from pyproject, when they name a directory."""
    try:
        if pyproject.stat().st_size > MAX_CONFIG_BYTES:
            return []
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []

    tool = data.get("tool", {})
    found: list[str] = []
    where = tool.get("setuptools", {}).get("packages", {}).get("find", {}).get("where")
    if isinstance(where, list):
        found += [w.strip("./") for w in where if isinstance(w, str)]
    for package in tool.get("poetry", {}).get("packages", []) or []:
        if isinstance(package, dict) and isinstance(package.get("from"), str):
            found.append(package["from"].strip("./"))
    return [directory for directory in found if directory and ".." not in directory]
