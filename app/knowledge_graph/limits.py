"""Every cap the indexing pipeline enforces, in one place.

We run a parser over source code we did not write, on a machine that also has
to keep serving an API. Limits are therefore part of the design, not an
afterthought: each one bounds either memory, CPU time or graph size, and each
is reported when it bites (see `SkipReason`) so coverage is visible instead of
silently partial.
"""

from dataclasses import dataclass
from enum import StrEnum


class SkipReason(StrEnum):
    """Why a file was not indexed. Counted per repository and surfaced in stats."""

    UNSUPPORTED_LANGUAGE = "unsupported_language"
    IGNORED_DIRECTORY = "ignored_directory"
    GITIGNORED = "gitignored"
    SYMLINK = "symlink"
    TOO_LARGE = "too_large"
    GENERATED = "generated"
    MINIFIED = "minified"
    BINARY = "binary"
    UNREADABLE = "unreadable"
    REPO_CAP_REACHED = "repo_cap_reached"
    PARSE_TIMEOUT = "parse_timeout"
    PARSE_FAILED = "parse_failed"


@dataclass(frozen=True, slots=True)
class Limits:
    """Caps applied while indexing one repository.

    Defaults come from the measurements in docs/knowledge-graph-plan.md §5:
    a 3.9M-line repository produced ~185k nodes and needed ~450MB to link, so
    these sit just above realistic repositories and well below what would
    exhaust a small instance.
    """

    #: Largest file to parse. Bigger files are almost always bundles or data.
    max_file_bytes: int = 512 * 1024
    #: Largest repository to index, by file count and by total source bytes.
    max_files: int = 20_000
    max_total_bytes: int = 200 * 1024 * 1024
    #: A file whose longest line exceeds this is treated as minified output.
    max_line_bytes: int = 2_000
    #: Per-file parse budget. Enforced by killing the worker process (PR 2).
    parse_timeout_seconds: float = 5.0
    #: Address-space cap per worker process, so one file cannot exhaust RAM.
    worker_memory_mb: int = 1_024
    #: Per-file fact caps: a generated file can contain a million calls.
    max_definitions_per_file: int = 10_000
    max_calls_per_file: int = 50_000
    #: Text we retain from source. Kept short deliberately — signatures and
    #: docstrings can contain secrets, and we store the minimum that is useful.
    max_signature_chars: int = 200
    max_docstring_chars: int = 400
    #: Above this many symbols, the graph keeps the module and file layers and
    #: omits the symbol layer, recording why. Sized from the measurements: a
    #: 3.9M-line repository produced ~171k symbols, and AuraDB Free caps at
    #: 200k nodes.
    max_symbols_per_repo: int = 200_000

    def is_oversized(self, size_bytes: int) -> bool:
        return size_bytes > self.max_file_bytes


DEFAULT_LIMITS = Limits()
