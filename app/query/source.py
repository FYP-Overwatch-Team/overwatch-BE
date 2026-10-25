"""Reading a repo's code as of one commit, for the AI chat's context and its citations.

Code is read from git's objects at the commit the graph was built from, never the
working tree: a symbol's line range then always matches, even while a re-index is
moving the checkout on. Paths come from the graph or a client, so they're checked
before git sees them, and anything unreadable is skipped: a snippet is a bonus,
never a failure.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.repo_identity import InvalidRepoName
from app.github.checkout import repo_workdir
from app.integrations import git_cli

MAX_SNIPPET_LINES = 80
#: Larger files are generated or vendored; not worth reading.
MAX_FILE_BYTES = 1_000_000

_BINARY_SNIFF_CHARS = 8_000
#: Matches kept per file when searching code; enough to see where in it the words cluster.
MAX_HITS_PER_FILE = 20

COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# Code can't close the fence or forge a reference; everything else stays as written.
_FENCE_RE = re.compile(r"</(graph_context)", re.IGNORECASE)
_REF_RE = re.compile(r"\[(n\d+|s\d+:[\d\s-]*)\]")

#: File name or extension -> the language a code block is highlighted as.
_LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".json": "json", ".md": "markdown", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".sql": "sql", ".css": "css", ".scss": "scss",
    ".html": "html", ".vue": "vue", ".svelte": "svelte", ".graphql": "graphql", ".xml": "xml",
    "dockerfile": "dockerfile", "makefile": "makefile",
}


@dataclass(frozen=True, slots=True)
class Snippet:
    path: str
    #: 1-based, as in the graph
    start_line: int
    #: as written in the file; escape with `prompt_line` before showing them to a model
    lines: tuple[str, ...]
    #: lines past the cap that weren't included
    omitted: int


def safe_path(path: str) -> bool:
    """A plain path inside the repo: relative, no `..`, nothing git could read as an option."""
    parts = PurePosixPath(path).parts
    return bool(parts) and not (
        path.startswith(("/", "-")) or "\\" in path or "\0" in path or ".." in parts
    )


def language_of(path: str) -> str | None:
    name = PurePosixPath(path).name.lower()
    return _LANGUAGES.get(name) or _LANGUAGES.get(PurePosixPath(name).suffix)


def _checkout(repo_full_name: str, commit: str) -> Path | None:
    """The repo's checkout, if `commit` is a full sha and there's a git checkout to read it from."""
    if not COMMIT_RE.match(commit):
        return None
    try:
        workdir = repo_workdir(repo_full_name)
    except InvalidRepoName:
        return None
    return workdir if (workdir / ".git").exists() else None


async def read_file(repo_full_name: str, commit: str, path: str) -> str | None:
    """A whole file as of `commit`, or None when it can't be read."""
    workdir = _checkout(repo_full_name, commit) if safe_path(path) else None
    if workdir is None:
        return None
    text = await git_cli.read_blob(workdir, commit, path, max_bytes=MAX_FILE_BYTES)
    # git's own test for a binary file: a NUL byte near the start
    if text is None or "\0" in text[:_BINARY_SNIFF_CHARS]:
        return None
    return text


async def find_in_code(repo_full_name: str, commit: str, terms: list[str]) -> list[tuple[str, int, str]]:
    """`(path, line, term)` for the lines at `commit` that contain one of `terms`; nothing when it can't search."""
    workdir = _checkout(repo_full_name, commit)
    if workdir is None:
        return []
    try:
        return await git_cli.grep(workdir, commit, terms, per_file=MAX_HITS_PER_FILE)
    except git_cli.GitCommandError:
        return []


async def read_snippet(
    repo_full_name: str, commit: str, path: str, start_line: int | None, end_line: int | None,
    max_lines: int = MAX_SNIPPET_LINES,
) -> Snippet | None:
    """A symbol's lines (or a file's first lines when no range is given) as of `commit`, at most `max_lines`."""
    text = await read_file(repo_full_name, commit, path)
    if text is None:
        return None
    all_lines = text.splitlines()
    start = max(start_line or 1, 1)
    end = min(end_line or len(all_lines), len(all_lines))
    if start > end:
        return None  # the range doesn't fit the file; skip rather than show the wrong lines
    wanted = all_lines[start - 1:end]
    shown = tuple(line.rstrip() for line in wanted[:max_lines])
    return Snippet(path, start, shown, len(wanted) - len(shown))


def prompt_line(line: str) -> str:
    """A line of code made safe to place inside the fence."""
    return _REF_RE.sub(r"(\1)", _FENCE_RE.sub(r"<\\/\1", line))
