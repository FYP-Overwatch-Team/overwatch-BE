"""Turning an import specifier into a file in this repository, or a package.

This is where today's biggest gap is fixed. `@/features/landing` is currently
treated as a third-party package because nothing reads the alias table from
tsconfig; in the user's own repository that is 98 of 173 internal imports.

Resolution order follows what the toolchains actually do:

* **JavaScript/TypeScript** — relative paths, then `paths` aliases, then
  `baseUrl`, then a package name.
* **Python** — explicit relative imports, then each configured import root,
  then a distribution name.

Everything is computed against `ModuleIndex`; nothing here reads from disk.
"""

import posixpath
from dataclasses import dataclass
from enum import StrEnum

from app.knowledge_graph.link.module_index import ModuleIndex
from app.knowledge_graph.project_config import ProjectConfig


class TargetKind(StrEnum):
    FILE = "file"
    PACKAGE = "package"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class ImportTarget:
    kind: TargetKind
    #: Repo-relative path for FILE, distribution name for PACKAGE, the raw
    #: specifier for UNRESOLVED.
    value: str

    @property
    def is_file(self) -> bool:
        return self.kind is TargetKind.FILE


PYTHON_EXTENSION = ".py"


def resolve_import(
    importer_path: str,
    specifier: str,
    index: ModuleIndex,
    config: ProjectConfig,
) -> ImportTarget:
    """Resolve one import as written in `importer_path`."""
    specifier = specifier.strip()
    if not specifier:
        return ImportTarget(TargetKind.UNRESOLVED, specifier)

    if importer_path.endswith(PYTHON_EXTENSION):
        return _resolve_python(importer_path, specifier, index, config)
    return _resolve_javascript(importer_path, specifier, index, config)


# --------------------------------------------------------------------------
# JavaScript / TypeScript
# --------------------------------------------------------------------------


def _resolve_javascript(
    importer_path: str, specifier: str, index: ModuleIndex, config: ProjectConfig,
) -> ImportTarget:
    if specifier.startswith("."):
        directory = posixpath.dirname(importer_path)
        resolved = index.resolve_javascript(posixpath.join(directory, specifier))
        return (
            ImportTarget(TargetKind.FILE, resolved)
            if resolved
            else ImportTarget(TargetKind.UNRESOLVED, specifier)
        )

    for candidate in _alias_candidates(specifier, config):
        resolved = index.resolve_javascript(candidate)
        if resolved:
            return ImportTarget(TargetKind.FILE, resolved)

    if config.ts_base_url is not None:
        resolved = index.resolve_javascript(posixpath.join(config.ts_base_url, specifier))
        if resolved:
            return ImportTarget(TargetKind.FILE, resolved)

    return ImportTarget(TargetKind.PACKAGE, npm_package_name(specifier))


def _alias_candidates(specifier: str, config: ProjectConfig) -> list[str]:
    """Paths a tsconfig alias maps this specifier to, most specific first.

    TypeScript allows one `*` per pattern; `@/*` -> `./src/*` means the text
    matched by `*` is substituted into each target.
    """
    candidates: list[tuple[int, str]] = []
    for pattern, targets in config.ts_paths.items():
        if "*" in pattern:
            prefix, _, suffix = pattern.partition("*")
            if not specifier.startswith(prefix) or not specifier.endswith(suffix):
                continue
            matched = specifier[len(prefix): len(specifier) - len(suffix) or None]
            for target in targets:
                candidates.append((len(prefix), _with_base(target.replace("*", matched), config)))
        elif specifier == pattern:
            for target in targets:
                candidates.append((len(pattern), _with_base(target, config)))

    # Longer prefixes win, as in TypeScript's own resolution.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


def _with_base(target: str, config: ProjectConfig) -> str:
    return posixpath.join(config.ts_base_url or "", target.removeprefix("./"))


def npm_package_name(specifier: str) -> str:
    """`@scope/pkg/sub/path` -> `@scope/pkg`; `lodash/merge` -> `lodash`."""
    specifier = specifier.removeprefix("node:")
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def _resolve_python(
    importer_path: str, specifier: str, index: ModuleIndex, config: ProjectConfig,
) -> ImportTarget:
    if specifier.startswith("."):
        resolved = _resolve_python_relative(importer_path, specifier, index)
        return (
            ImportTarget(TargetKind.FILE, resolved)
            if resolved
            else ImportTarget(TargetKind.UNRESOLVED, specifier)
        )

    module_path = specifier.replace(".", "/")
    for root in config.python_roots:
        resolved = index.resolve_python(posixpath.join(root, module_path))
        if resolved:
            return ImportTarget(TargetKind.FILE, resolved)

    return ImportTarget(TargetKind.PACKAGE, python_distribution_name(specifier))


def _resolve_python_relative(importer_path: str, specifier: str, index: ModuleIndex) -> str | None:
    """`from ..services import users` inside `pkg/api/handlers.py`."""
    leading_dots = len(specifier) - len(specifier.lstrip("."))
    remainder = specifier[leading_dots:].replace(".", "/")

    # One dot means "this package": the importer's own directory. Each extra
    # dot climbs one level, and climbing past the repository root is an error
    # in Python too — resolve it to nothing rather than to a root-level file.
    directory = posixpath.dirname(importer_path)
    parts = directory.split("/") if directory else []
    climb = leading_dots - 1
    if climb > len(parts):
        return None
    base = "/".join(parts[: len(parts) - climb]) if climb else directory

    return index.resolve_python(posixpath.join(base, remainder) if remainder else base)


def python_distribution_name(specifier: str) -> str:
    """`os.path` -> `os`; `google.genai` -> `google`."""
    return specifier.split(".")[0]
