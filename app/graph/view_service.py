"""Assembling one interactive view of a repository's graph.

This is the only place that knows both how to *fetch* graph data and how to
*render* a view of it. The fetching is two bounded queries; the rendering is
the pure projection in `app.knowledge_graph.view`. Keeping the split here
means the interesting rule — what a collapsed folder stands for — is testable
without a database.

Two things are resolved here rather than in the projection, because both need
data the projection cannot ask for:

* **Granularity.** "Open everything down to files" is a request to expand
  containers we have not seen yet, so it is walked level by level, fetching as
  it goes, until it runs out of tree or out of budget. It is *scoped*: opening
  one folder to its files should cost that folder, not the repository.
* **Which files are open.** A symbol is folded into its file inside the
  database unless that file is open, and only this walk knows which files
  those are.

Every repository name reaching this module has already been authorised by
`require_connected_repo`, and every query the store runs is scoped to it.
"""

from dataclasses import replace

import structlog

from app.knowledge_graph.build.ids import repository_id
from app.knowledge_graph.build.model import NodeLabel
from app.knowledge_graph.ports import KnowledgeGraphStore
from app.knowledge_graph.view.hierarchy import is_within
from app.knowledge_graph.view.lod import opens_automatically, project
from app.knowledge_graph.view.model import (
    VIEWABLE_EDGE_TYPES,
    Granularity,
    ViewGraph,
    ViewNode,
    ViewRequest,
)

logger = structlog.get_logger("app.knowledge_graph.view")

#: Levels of the containment tree one request will walk. Deeper than any real
#: repository nests, and a hard stop if stored data ever contains a cycle.
MAX_LEVELS = 12
#: Containers one view may hold open. The cap is on the *fetch*, so a request
#: cannot turn into an unbounded number of queries.
MAX_OPEN_CONTAINERS = 64
#: Parents per `view_children` call, matching the store's own cap.
PARENTS_PER_QUERY = 64


async def build_view(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    request: ViewRequest,
    *,
    granularity: Granularity | None = None,
    scope: str | None = None,
) -> ViewGraph:
    """Fetch what the view needs and render it.

    `scope` confines `granularity` to one subtree. Without it, "open to files"
    means the whole repository, which is rarely what anyone wants and always
    what hurts most.
    """
    gathered = await _gather(store, repo_full_name, request, granularity, scope)

    edge_types = sorted(
        str(edge_type)
        for edge_type in (request.filters.edge_types & VIEWABLE_EDGE_TYPES)
    )
    edges = await store.rollup_edges(
        repo_full_name,
        edge_types=edge_types,
        expanded_files=sorted(gathered.open_files),
    )

    view = project(
        repo_full_name,
        gathered.children,
        edges,
        ViewRequest(
            expanded=sorted(gathered.opened),
            revealed=request.revealed,
            filters=request.filters,
            caps=request.caps,
        ),
    )
    # The fetch has its own ceiling, and a ceiling nobody is told about is a
    # silently wrong picture.
    if gathered.capped:
        view = replace(view, truncated=True)

    logger.info(
        "graph_view_built",
        repo=repo_full_name,
        opened=len(view.expanded),
        nodes=len(view.nodes),
        edges=len(view.edges),
        overflows=len(view.overflows),
        truncated=view.truncated,
    )
    return view


class _Gathered:
    """What the fetch produced, and whether it had to stop early."""

    __slots__ = ("children", "opened", "open_files", "capped")

    def __init__(self) -> None:
        self.children: dict[str, list[ViewNode]] = {}
        self.opened: set[str] = set()
        self.open_files: set[str] = set()
        self.capped = False


def _wants_opening(
    child: ViewNode,
    requested: set[str],
    granularity: Granularity | None,
    scope: str | None,
) -> bool:
    """Whether this container should be open in the view being built.

    Asking for it by id always wins. A granularity opens by label, and a scope
    confines that to one subtree — plus the containers on the way down to it,
    which have to be open for the subtree to be reachable at all.
    """
    if child.id in requested:
        return True
    if granularity is None:
        return False
    if scope is None:
        return opens_automatically(child.label, granularity)
    if is_within(child.id, scope):
        return opens_automatically(child.label, granularity)
    return is_within(scope, child.id)


async def _gather(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    request: ViewRequest,
    granularity: Granularity | None,
    scope: str | None,
) -> _Gathered:
    """Walk down the containment tree, fetching only what will be rendered."""
    requested = set(request.expanded)
    gathered = _Gathered()

    frontier = {repository_id(repo_full_name)}
    fetched: set[str] = set()

    for _ in range(MAX_LEVELS):
        pending = [parent for parent in sorted(frontier) if parent not in fetched]
        if not pending:
            break
        fetched.update(pending)

        for batch in _batched(pending, PARENTS_PER_QUERY):
            gathered.children.update(await store.view_children(repo_full_name, batch))

        frontier = set()
        for parent in pending:
            for child in gathered.children.get(parent, ()):
                if not child.is_container:
                    continue
                if not _wants_opening(child, requested, granularity, scope):
                    continue
                if len(gathered.opened) >= MAX_OPEN_CONTAINERS:
                    gathered.capped = True
                    continue
                gathered.opened.add(child.id)
                frontier.add(child.id)
                if child.label is NodeLabel.FILE and child.path:
                    gathered.open_files.add(child.path)

    return gathered


def _batched(values: list[str], size: int) -> list[list[str]]:
    return [values[start:start + size] for start in range(0, len(values), size)]
