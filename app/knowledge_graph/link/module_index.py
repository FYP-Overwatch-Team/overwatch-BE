"""Which files exist, for resolving imports without touching the filesystem.

The index is built from the paths we already discovered, so resolution is pure
computation over a known set. That is a security property as much as a
performance one: a specifier can name anything at all — "../../../etc/passwd",
a symlink, a device file — and the worst it can do is fail to match.
"""

import posixpath
from collections.abc import Iterable

#: Extensions tried for a bare JS/TS module path, in TypeScript's own order.
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
#: Directory imports resolve to an index file.
JS_INDEX_FILES = tuple(f"/index{extension}" for extension in JS_EXTENSIONS)

PYTHON_CANDIDATES = (".py", "/__init__.py")


class ModuleIndex:
    """The set of indexable files in a repository, keyed for lookup."""

    def __init__(self, paths: Iterable[str]):
        self._paths = frozenset(paths)

    def __contains__(self, path: str) -> bool:
        return path in self._paths

    def __len__(self) -> int:
        return len(self._paths)

    def resolve_javascript(self, module_path: str) -> str | None:
        """`src/lib/utils` -> `src/lib/utils.ts` or `src/lib/utils/index.ts`."""
        normalized = _normalize(module_path)
        if normalized is None:
            return None
        if normalized in self._paths:  # the specifier already carried an extension
            return normalized
        for suffix in (*JS_EXTENSIONS, *JS_INDEX_FILES):
            candidate = f"{normalized}{suffix}"
            if candidate in self._paths:
                return candidate
        return None

    def resolve_python(self, module_path: str) -> str | None:
        """`services/users` -> `services/users.py` or `services/users/__init__.py`."""
        normalized = _normalize(module_path)
        if normalized is None:
            return None
        for suffix in PYTHON_CANDIDATES:
            candidate = f"{normalized}{suffix}"
            if candidate in self._paths:
                return candidate
        return None


def _normalize(module_path: str) -> str | None:
    """Collapse `.` and `..`, rejecting anything that climbs out of the repo."""
    normalized = posixpath.normpath(module_path).removeprefix("./")
    if normalized.startswith("..") or normalized.startswith("/") or normalized in ("", "."):
        return None
    return normalized
