"""Graph endpoints.

Every route here depends on `require_connected_repo`, so authorisation is one
decision made in one place rather than a check each handler could forget. Node
ids embed the repository name, so each handler also passes the *authorised*
repository into the store, and the store filters on it — an id belonging to
another repository finds nothing rather than leaking a subgraph.

`GET /graph` keeps the response shape the dashboard already consumes, whether
it is served by the old pipeline or the new one.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import require_connected_repo
from app.core.exceptions import NotFoundError
from app.knowledge_graph.build.model import EdgeType
from app.services.knowledge_graph_store import (
    get_knowledge_graph_store,
    validate_depth,
)

router = APIRouter(prefix="/graph", tags=["graph"])

#: Caps on what one request may pull back, so a crafted query cannot ask for
#: an unbounded traversal.
MAX_SEARCH_RESULTS = 50
MAX_NEIGHBOURS = 500
MIN_SEARCH_TERM = 2

ConnectedRepo = Annotated[dict, Depends(require_connected_repo)]


@router.get("")
async def get_graph(response: Response, repo: ConnectedRepo) -> dict:
    """The module-level diagram, in the shape the dashboard already consumes."""
    repo_full_name = repo["repo_full_name"]
    graph = await get_knowledge_graph_store().module_view(repo_full_name)

    # The graph only changes when the indexed commit changes, so polling can
    # be answered from cache.
    if version := repo.get("graph_version"):
        response.headers["ETag"] = f'W/"{version}"'

    return {
        "repo_full_name": repo_full_name,
        "parse_status": repo["parse_status"],
        **graph,
    }


@router.get("/node")
async def get_node(
    repo: ConnectedRepo,
    node_id: Annotated[str, Query(min_length=1, max_length=512)],
) -> dict:
    """One node and what it contains: a module's files, a file's symbols."""
    detail = await get_knowledge_graph_store().node_detail(repo["repo_full_name"], node_id)
    if detail is None:
        raise NotFoundError("node not found in this repository", error_code="node_not_found")
    return detail


@router.get("/neighbours")
async def get_neighbours(
    repo: ConnectedRepo,
    node_id: Annotated[str, Query(min_length=1, max_length=512)],
    direction: Annotated[str, Query(pattern="^(in|out|both)$")] = "both",
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_NEIGHBOURS)] = 100,
    edge_types: Annotated[list[str] | None, Query()] = None,
) -> dict:
    """What a node uses and what uses it."""
    return await get_knowledge_graph_store().neighbours(
        repo["repo_full_name"],
        node_id,
        direction=direction,
        edge_types=edge_types,
        depth=validate_depth(depth),
        limit=limit,
    )


@router.get("/search")
async def search_graph(
    repo: ConnectedRepo,
    q: Annotated[str, Query(min_length=MIN_SEARCH_TERM, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_RESULTS)] = 20,
) -> dict:
    """Find files and symbols by name."""
    results = await get_knowledge_graph_store().search(repo["repo_full_name"], q, limit=limit)
    return {"results": results, "count": len(results)}


@router.get("/stats")
async def get_stats(repo: ConnectedRepo) -> dict:
    """What the graph contains, and how much of the code it could resolve.

    Coverage is reported rather than implied: calls we could not resolve are
    counted, not hidden.
    """
    repo_full_name = repo["repo_full_name"]
    counts = await get_knowledge_graph_store().counts(repo_full_name)
    indexed = repo.get("graph_stats") or {}

    return {
        "repo_full_name": repo_full_name,
        "parse_status": repo["parse_status"],
        "graph_version": repo.get("graph_version"),
        "indexed_at": repo.get("indexed_at"),
        **counts,
        "files": indexed.get("files"),
        "symbols": indexed.get("symbols"),
        "symbol_layer": indexed.get("symbol_layer"),
        "resolution": indexed.get("resolution"),
        "skipped": indexed.get("skipped"),
    }


@router.get("/edge-types")
async def list_edge_types() -> dict:
    """The relationship types a client may filter `/graph/neighbours` by."""
    return {"edge_types": sorted(str(edge_type) for edge_type in EdgeType)}
