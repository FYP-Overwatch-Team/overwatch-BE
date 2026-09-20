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
  it goes, until it runs out of tree or out of budget.
* **Which files are open.** A symbol is folded into its file inside the
  database unless that file is open, and only this walk knows which files
  those are.

Every repository name reaching this module has already been authorised by
`require_connected_repo`, and every query the store runs is scoped to it.
"""

import structlog

from app.knowledge_graph.build.ids import repository_id
from app.knowledge_graph.build.model import NodeLabel
from app.knowledge_graph.ports import KnowledgeGraphStore
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
) -> ViewGraph:
    """Fetch what the view needs and render it."""
    children, opened, open_files = await _gather(
        store, repo_full_name, request, granularity,
    )

    edge_types = sorted(
        str(edge_type)
        for edge_type in (request.filters.edge_types & VIEWABLE_EDGE_TYPES)
    )
    edges = await store.rollup_edges(
        repo_full_name, edge_types=edge_types, expanded_files=sorted(open_files),
    )

    view = project(
        repo_full_name,
        children,
        edges,
        ViewRequest(
            expanded=sorted(opened),
            filters=request.filters,
            caps=request.caps,
        ),
    )
    logger.info(
        "graph_view_built",
        repo=repo_full_name,
        opened=len(view.expanded),
        nodes=len(view.nodes),
        edges=len(view.edges),
        truncated=view.truncated,
    )
    return view


async def _gather(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    request: ViewRequest,
    granularity: Granularity | None,
) -> tuple[dict[str, list[ViewNode]], set[str], set[str]]:
    """Walk down the containment tree, fetching only what will be rendered.

    Returns the children of every container that ends up open, the set that is
    actually open, and the paths of the open files.
    """
    requested = set(request.expanded)
    children: dict[str, list[ViewNode]] = {}
    opened: set[str] = set()
    open_files: set[str] = set()

    frontier = {repository_id(repo_full_name)}
    fetched: set[str] = set()

    for _ in range(MAX_LEVELS):
        pending = [parent for parent in sorted(frontier) if parent not in fetched]
        if not pending:
            break
        fetched.update(pending)

        for batch in _batched(pending, PARENTS_PER_QUERY):
            children.update(await store.view_children(repo_full_name, batch))

        frontier = set()
        for parent in pending:
            for child in children.get(parent, ()):
                if not child.is_container or len(opened) >= MAX_OPEN_CONTAINERS:
                    continue
                if child.id not in requested and not opens_automatically(
                    child.label, granularity,
                ):
                    continue
                opened.add(child.id)
                frontier.add(child.id)
                if child.label is NodeLabel.FILE and child.path:
                    open_files.add(child.path)

    return children, opened, open_files


def _batched(values: list[str], size: int) -> list[list[str]]:
    return [values[start:start + size] for start in range(0, len(values), size)]
