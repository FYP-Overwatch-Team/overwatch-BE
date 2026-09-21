"""Every graph id is formatted here, and nowhere else.

Ids are **path-derived and stable**: the same commit always produces the same
ids, and an unchanged file keeps its ids across re-parses. That is what lets
the store upsert instead of duplicating, lets the frontend keep a selection
across a refresh, and lets an incremental sync write only what changed.

The module-level format is unchanged from the first version of the graph
(`owner/name:api`), so ids already held by the dashboard stay valid.
"""

import posixpath

#: The single node that stands for "everything outside this repository" in the
#: module-level view the dashboard renders. Packages hang under it.
EXTERNAL_MODULE = "__external__"
#: Routes belong to no directory, so they hang under their own module.
ROUTES_MODULE = "__routes__"
#: Modules that stand for something other than a directory. They have no
#: parent but the repository, and never gain one by path arithmetic.
SYNTHETIC_MODULES = frozenset({EXTERNAL_MODULE, ROUTES_MODULE})

_ECOSYSTEM_BY_LANGUAGE = {
    "python": "pypi",
    "javascript": "npm",
    "typescript": "npm",
    "tsx": "npm",
}


def repository_id(repo_full_name: str) -> str:
    return repo_full_name


def module_id(repo_full_name: str, module: str) -> str:
    return f"{repo_full_name}:{module}"


def file_id(repo_full_name: str, path: str) -> str:
    return f"{repo_full_name}:{path}"


def symbol_id(repo_full_name: str, path: str, qualified_name: str, ordinal: int = 1) -> str:
    """`owner/name:api/users.py#UserService.get`.

    `ordinal` disambiguates definitions that share a qualified name inside one
    file — two `helper` functions in different branches, say. The first keeps
    the clean id so the common case stays readable.
    """
    suffix = "" if ordinal <= 1 else f"@{ordinal}"
    return f"{repo_full_name}:{path}#{qualified_name}{suffix}"


def package_id(repo_full_name: str, ecosystem: str, name: str) -> str:
    return f"{repo_full_name}:pkg:{ecosystem}/{name}"


def route_id(repo_full_name: str, method: str, normalised_path: str) -> str:
    """`owner/name:route:GET /api/users/*`.

    Built from the *normalised* path, so a route declared `/users/{id}` and a
    call written `/users/123` address the same node.
    """
    return f"{repo_full_name}:route:{method.upper()} {normalised_path}"


def module_of(path: str) -> str | None:
    """The outermost directory a file belongs to, or None if it has none."""
    head, _, _ = path.partition("/")
    return head if "/" in path else None


def owning_module(path: str) -> str | None:
    """The directory that *directly* contains a file: `api/routes/users.py`
    -> `api/routes`. Contrast `module_of`, which returns the outermost one.

    None means the repository holds the file itself. Inventing a folder for
    those — there used to be one called `root` — puts a directory on screen
    that is not in the repository, and the tree stops matching what a person
    sees when they look at their own checkout.
    """
    head, separator, _ = path.rpartition("/")
    return head if separator else None


def module_ancestry(module: str) -> list[str]:
    """Every module on the path down to `module`, outermost first.

    `api/routes` -> `["api", "api/routes"]`. A synthetic module stands alone,
    because its name is not a path and must not be split like one.
    """
    if module in SYNTHETIC_MODULES:
        return [module]
    segments = module.split("/")
    return ["/".join(segments[: index + 1]) for index in range(len(segments))]


def parent_module(module: str) -> str | None:
    """The module one level up, or None when it sits directly in the repository."""
    if module in SYNTHETIC_MODULES:
        return None
    head, separator, _ = module.rpartition("/")
    return head if separator else None


def module_depth(module: str) -> int:
    """How deep a module sits: 1 directly under the repository."""
    return 1 if module in SYNTHETIC_MODULES else module.count("/") + 1


_SYNTHETIC_DISPLAY_NAMES = {
    EXTERNAL_MODULE: "external dependencies",
    ROUTES_MODULE: "http routes",
}


def module_display_name(module: str) -> str:
    return _SYNTHETIC_DISPLAY_NAMES.get(module, module)


def ecosystem_for_language(language: str) -> str:
    return _ECOSYSTEM_BY_LANGUAGE.get(language, "unknown")


def parent_directory(path: str) -> str:
    return posixpath.dirname(path)
