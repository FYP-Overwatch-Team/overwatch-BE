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
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_connected_repo
from app.core.exceptions import NotFoundError
from app.knowledge_graph.build.model import EdgeType, NodeLabel
from app.knowledge_graph.view.model import (
    VIEWABLE_EDGE_TYPES,
    VIEWABLE_NODE_LABELS,
    Granularity,
    ViewCaps,
    ViewFilters,
    ViewGraph,
    ViewRequest,
)
from app.services.graph_view_service import build_view
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
#: Containers one view request may ask to open. Matches the service's own cap,
#: so a larger list is rejected at the edge rather than silently ignored.
MAX_EXPANDED = 64
MAX_NODE_ID = 512

#: A node id as it arrives from a client. Bounded, and never concatenated into
#: a query — it is matched as a parameter or compared as a string.
NodeId = Annotated[str, Field(min_length=1, max_length=MAX_NODE_ID)]

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


class GraphViewBody(BaseModel):
    """What to open and what to show.

    Both filters are lists of *enum members*, so an unknown label or
    relationship type is rejected by validation and never reaches a query.
    `expand` holds node ids, which are only ever compared as parameters.
    """

    model_config = ConfigDict(extra="forbid")

    #: Containers to open. Ids whose parent is shut are ignored, and the
    #: response reports what is actually open.
    expand: list[NodeId] = Field(default_factory=list, max_length=MAX_EXPANDED)
    #: Containers whose children should be drawn in full rather than paged.
    #: This is what clicking a "6 more" marker sends.
    reveal: list[NodeId] = Field(default_factory=list, max_length=MAX_EXPANDED)
    #: Open everything down to this level in one step, without naming ids.
    expand_to: Granularity | None = None
    #: Confines `expand_to` to one subtree. Without it the whole repository
    #: opens, which is rarely wanted and always the expensive answer.
    expand_from: NodeId | None = None
    #: Omit to see every kind of node / relationship.
    node_labels: list[NodeLabel] | None = None
    edge_types: list[EdgeType] | None = None

    def filters(self) -> ViewFilters:
        return ViewFilters(
            node_labels=_selected(self.node_labels, VIEWABLE_NODE_LABELS),
            edge_types=_selected(self.edge_types, VIEWABLE_EDGE_TYPES),
        )


def _selected(chosen, allowed: frozenset):
    """A client's choice, intersected with what a view may show at all."""
    if not chosen:
        return allowed
    return frozenset(chosen) & allowed


@router.post("/view")
async def get_view(repo: ConnectedRepo, body: GraphViewBody | None = None) -> dict:
    """The graph at whatever level of detail the client asked for.

    A POST because the request carries a list of open containers and two
    filter sets; it reads and changes nothing, and the same body always
    returns the same view of a given commit.

    With an empty body this is the whole repository as a handful of boxes —
    the view everyone starts from.
    """
    request = body or GraphViewBody()
    view = await build_view(
        get_knowledge_graph_store(),
        repo["repo_full_name"],
        ViewRequest(
            expanded=request.expand,
            revealed=request.reveal,
            filters=request.filters(),
        ),
        granularity=request.expand_to,
        scope=request.expand_from,
    )
    return {
        "repo_full_name": repo["repo_full_name"],
        "parse_status": repo["parse_status"],
        "graph_version": repo.get("graph_version"),
        **_serialise(view),
    }


@router.get("/view/options")
async def view_options() -> dict:
    """Everything a client may filter a view by, so the UI is never out of date.

    `page_size` is included so the UI can warn *before* a click that a folder
    is wider than one page, rather than repeating the number and drifting.
    """
    caps = ViewCaps()
    return {
        "node_labels": sorted(str(label) for label in VIEWABLE_NODE_LABELS),
        "edge_types": sorted(str(edge_type) for edge_type in VIEWABLE_EDGE_TYPES),
        "granularities": [str(level) for level in Granularity],
        "page_size": caps.page_size,
        "max_nodes": caps.max_nodes,
    }


def _serialise(view: ViewGraph) -> dict:
    return {
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "label": str(node.label),
                "kind": node.kind,
                "path": node.path,
                "depth": node.depth,
                "file_count": node.file_count,
                "definition_count": node.definition_count,
                "child_count": node.child_count,
                "language": node.language,
                "parent_id": node.parent_id,
                "expandable": node.is_container,
            }
            for node in view.nodes
        ],
        "edges": [
            {
                "source": edge.source,
                "target": edge.target,
                "type": str(edge.type),
                "weight": edge.weight,
                "collapsed": edge.collapsed,
            }
            for edge in view.edges
        ],
        "overflows": [
            {
                "id": overflow.id,
                "container_id": overflow.container_id,
                "container_name": overflow.container_name,
                "shown": overflow.shown,
                "hidden": overflow.hidden,
            }
            for overflow in view.overflows
        ],
        "expanded": [
            {
                "id": node.id,
                "name": node.name,
                "label": str(node.label),
                "kind": node.kind,
                "path": node.path,
                "parent_id": node.parent_id,
            }
            for node in view.expanded
        ],
        "truncated": view.truncated,
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
