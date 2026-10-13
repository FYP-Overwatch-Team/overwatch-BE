"""Graph endpoints.

Every route depends on `require_connected_repo`, so authorisation is one decision made
in one place rather than a check each handler could forget.
"""

from typing import Annotated

from fastapi import Depends, Query, Response

from app.api.deps import require_connected_repo
from app.core.responses import WithMeta, api_router
from app.graph import service
from app.graph.schemas import (
    MAX_NODE_ID,
    GraphStats,
    GraphView,
    GraphViewBody,
    ModuleGraph,
    Neighbourhood,
    NodeDetail,
    SearchResult,
    ViewOptions,
)
from app.knowledge_graph.build.model import EdgeType

router = api_router(prefix="/graph", tags=["graph"])

#: Caps on what one request may pull back, so a crafted query cannot ask for an unbounded traversal.
MAX_SEARCH_RESULTS = 50
MAX_NEIGHBOURS = 500
MIN_SEARCH_TERM = 2

ConnectedRepo = Annotated[dict, Depends(require_connected_repo)]
NodeIdQuery = Annotated[str, Query(min_length=1, max_length=MAX_NODE_ID)]


@router.get("")
async def get_graph(repo: ConnectedRepo, response: Response) -> ModuleGraph:
    """The module-level diagram."""
    # the graph only changes when the indexed commit does, so polling can be answered from cache
    if version := repo.get("graph_version"):
        response.headers["ETag"] = f'W/"{version}"'
    return await service.module_graph(repo)


@router.post("/view")
async def get_view(repo: ConnectedRepo, body: GraphViewBody | None = None) -> GraphView:
    """The graph at whatever level of detail the client asked for.

    A POST because the body carries open containers and two filter sets; it changes nothing.
    An empty body is the whole repository as a handful of boxes.
    """
    return await service.view(repo, body or GraphViewBody())


@router.get("/view/options")
async def view_options() -> ViewOptions:
    """Everything a client may filter a view by, so the UI is never out of date."""
    return service.view_options()


@router.get("/node")
async def get_node(repo: ConnectedRepo, node_id: NodeIdQuery) -> NodeDetail:
    """One node and what it contains: a module's files, a file's symbols."""
    return await service.node(repo, node_id)


@router.get("/neighbours")
async def get_neighbours(
    repo: ConnectedRepo,
    node_id: NodeIdQuery,
    direction: Annotated[str, Query(pattern="^(in|out|both)$")] = "both",
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_NEIGHBOURS)] = 100,
    edge_types: Annotated[list[str] | None, Query()] = None,
) -> Neighbourhood:
    """What a node uses and what uses it."""
    return await service.neighbours(
        repo, node_id, direction=direction, depth=depth, limit=limit, edge_types=edge_types,
    )


@router.get("/search")
async def search_graph(
    repo: ConnectedRepo,
    q: Annotated[str, Query(min_length=MIN_SEARCH_TERM, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_RESULTS)] = 20,
) -> WithMeta[list[SearchResult]]:
    """Find files and symbols by name."""
    results = await service.search(repo, q, limit)
    return WithMeta(results, {"count": len(results)})


@router.get("/stats")
async def get_stats(repo: ConnectedRepo) -> GraphStats:
    """What the graph contains, and how much of the code it could resolve."""
    return await service.stats(repo)


@router.get("/edge-types")
async def list_edge_types() -> list[str]:
    """The relationship types a client may filter `/graph/neighbours` by."""
    return sorted(str(edge_type) for edge_type in EdgeType)
