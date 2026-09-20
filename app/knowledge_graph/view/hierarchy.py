"""Where a node sits in the containment tree, worked out from its id alone.

Graph ids are path-derived (`owner/name:api/users.py#UserService.get`), so the
chain of containers above a node is arithmetic on the id — no query, no extra
stored property, nothing to fall out of step with the graph.

An id is not self-describing, though: `owner/name:api` is a folder and
`owner/name:api.py` is a file, and only the label tells them apart. Every
function here therefore takes the label with the id.

The chain this produces must match the `CONTAINS` / `DEFINES` / `HAS_MEMBER`
edges the builder writes. Both are derived from the same two helpers in
`ids.py` — `owning_module` and `parent_module` — which is what keeps them
honest, and a test walks a real snapshot to prove it.
"""

from collections.abc import Mapping

from app.knowledge_graph.build.ids import (
    EXTERNAL_MODULE,
    ROUTES_MODULE,
    file_id,
    module_ancestry,
    module_id,
    owning_module,
    parent_module,
    repository_id,
)
from app.knowledge_graph.build.model import NodeLabel


def container_chain(node_id: str, repo_full_name: str, label: NodeLabel) -> list[str]:
    """Ids of the containers above a node, nearest first, ending at the repository.

    Returns an empty list for the repository itself, and for any id that does
    not belong to this repository — such an id is simply not placed, which is
    the behaviour that keeps one repository's nodes out of another's view.
    """
    repository = repository_id(repo_full_name)
    if label is NodeLabel.REPOSITORY or node_id == repository:
        return []

    prefix = f"{repo_full_name}:"
    if not node_id.startswith(prefix):
        return []
    remainder = node_id[len(prefix):]

    if label is NodeLabel.PACKAGE:
        return [module_id(repo_full_name, EXTERNAL_MODULE), repository]
    if label is NodeLabel.ROUTE:
        return [module_id(repo_full_name, ROUTES_MODULE), repository]

    if label is NodeLabel.SYMBOL:
        # A symbol is contained by its file, whatever nests it in between:
        # a method's `HAS_MEMBER` parent is a symbol in the same file, so the
        # file is the first container the view can ever render.
        path = remainder.partition("#")[0]
        return [
            file_id(repo_full_name, path),
            *_modules_above(repo_full_name, owning_module(path)),
            repository,
        ]
    if label is NodeLabel.FILE:
        return [*_modules_above(repo_full_name, owning_module(remainder)), repository]
    if label is NodeLabel.MODULE:
        parent = parent_module(remainder)
        if parent is None:
            return [repository]
        return [*_modules_above(repo_full_name, parent), repository]

    return [repository]


def is_within(node_id: str, container_id: str) -> bool:
    """Whether one node lies inside another, by id arithmetic alone.

    Ids are path-derived, so this is a prefix test — but only at a separator,
    or `repo:apiary` would count as being inside `repo:api`, and `repo:app.tsx`
    as being inside `repo:app`.
    """
    if node_id == container_id:
        return True
    if not node_id.startswith(container_id):
        return False
    separator = node_id[len(container_id)]
    return separator in ("/", "#")


def nearest_visible(
    node_id: str,
    label: NodeLabel,
    visible: frozenset[str] | set[str],
    repo_full_name: str,
    redirects: Mapping[str, str] | None = None,
) -> str | None:
    """The node itself if it is on screen, else the closest ancestor that is.

    `redirects` stands in for nodes that are *represented* on screen without
    being drawn — the children an open container did not have room for, which
    a "6 more" marker speaks for. Consulted at every step, so a symbol inside
    a file inside the overflow still finds its way to the marker.

    None means the node has no visible ancestor at all, which happens when a
    filter hid its whole branch. An edge to it is dropped rather than drawn to
    something arbitrary.
    """
    redirects = redirects or {}
    for step in (node_id, *container_chain(node_id, repo_full_name, label)):
        if step in visible:
            return step
        if step in redirects:
            return redirects[step]
    return None


class PlacementCache:
    """`nearest_visible` for one fixed view, memoised.

    Every edge endpoint is looked up, and a repository has far fewer distinct
    endpoints than edges, so the chain for each is computed once.
    """

    __slots__ = ("_repo", "_visible", "_redirects", "_resolved")

    def __init__(
        self,
        repo_full_name: str,
        visible: frozenset[str] | set[str],
        redirects: Mapping[str, str] | None = None,
    ) -> None:
        self._repo = repo_full_name
        self._visible = visible
        self._redirects = redirects or {}
        self._resolved: dict[str, str | None] = {}

    def place(self, node_id: str, label: NodeLabel) -> str | None:
        if node_id not in self._resolved:
            self._resolved[node_id] = nearest_visible(
                node_id, label, self._visible, self._repo, self._redirects,
            )
        return self._resolved[node_id]


def _modules_above(repo_full_name: str, module: str) -> list[str]:
    """Module ids from `module` up to the outermost one, nearest first."""
    return [module_id(repo_full_name, name) for name in reversed(module_ancestry(module))]


def parent_of(node_id: str, repo_full_name: str, label: NodeLabel) -> str | None:
    """The single container directly above a node, or None for the repository."""
    chain = container_chain(node_id, repo_full_name, label)
    return chain[0] if chain else None


def describe_tree(nodes: Mapping[str, NodeLabel], repo_full_name: str) -> dict[str, str | None]:
    """Parent id for each node in `nodes`. Used by tests to compare with the builder."""
    return {
        node_id: parent_of(node_id, repo_full_name, label)
        for node_id, label in nodes.items()
    }
