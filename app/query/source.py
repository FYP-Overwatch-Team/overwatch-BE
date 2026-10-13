"""Reading a symbol's source from the repo's checkout, for the AI chat's context.

The checkout is the one indexing builds the graph from, so a symbol's line range
matches it. Paths come from the graph but are still resolved inside the checkout,
and anything unreadable is skipped: a snippet is a bonus, never a failure.
"""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.repo_identity import InvalidRepoName
from app.github.checkout import repo_workdir

MAX_SNIPPET_LINES = 80
#: Larger files are generated or vendored; not worth reading for a snippet.
MAX_FILE_BYTES = 1_000_000

# Code can't close the fence or forge a reference; everything else stays as written.
_FENCE_RE = re.compile(r"</(graph_context)", re.IGNORECASE)
_REF_RE = re.compile(r"\[(n\d+)\]")


@dataclass(frozen=True, slots=True)
class Snippet:
    path: str
    #: 1-based, as in the graph
    start_line: int
    lines: tuple[str, ...]
    #: lines past the cap that weren't included
    omitted: int


async def read_snippet(repo_full_name: str, path: str, start_line: int | None, end_line: int | None) -> Snippet | None:
    """A symbol's lines (or a file's first lines when no range is given), capped."""
    return await asyncio.to_thread(_read, repo_full_name, path, start_line, end_line)


def _read(repo_full_name: str, path: str, start_line: int | None, end_line: int | None) -> Snippet | None:
    try:
        root = repo_workdir(repo_full_name).resolve()
    except InvalidRepoName:
        return None
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    all_lines = text.splitlines()
    start = max(start_line or 1, 1)
    end = min(end_line or len(all_lines), len(all_lines))
    if start > end:
        return None  # the file changed under the graph; skip rather than show the wrong lines
    wanted = all_lines[start - 1:end]
    shown = [_neutralise(line) for line in wanted[:MAX_SNIPPET_LINES]]
    return Snippet(path, start, tuple(shown), len(wanted) - len(shown))


def _neutralise(line: str) -> str:
    return _REF_RE.sub(r"(\1)", _FENCE_RE.sub(r"<\\/\1", line.rstrip()))
