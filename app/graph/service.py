"""Graph reads for one authorised repository.

Node ids embed the repository name, so every call passes the authorised repo into the
store, which filters on it: an id from another repository finds nothing.
"""

from app.core.exceptions import NotFoundError
from app.graph.schemas import MAX_EXPANDED, GraphViewBody
from app.knowledge_graph.view.model import (
    VIEWABLE_EDGE_TYPES,
    VIEWABLE_NODE_LABELS,
    Granularity,
    ViewCaps,
    ViewGraph,
    ViewRequest,
)
from app.graph.view_service import build_view
from app.db.knowledge_graph_store import get_knowledge_graph_store, validate_depth


async def module_graph(repo: dict) -> dict:
    graph = await get_knowledge_graph_store().module_view(repo["repo_full_name"])
    return {"repo_full_name": repo["repo_full_name"], "parse_status": repo["parse_status"], **graph}


async def view(repo: dict, body: GraphViewBody) -> dict:
    graph = await build_view(
        get_knowledge_graph_store(),
        repo["repo_full_name"],
        ViewRequest(expanded=body.expand, revealed=body.reveal, filters=body.filters()),
        granularity=body.expand_to,
        scope=body.expand_from,
    )
    return {
        "repo_full_name": repo["repo_full_name"],
        "parse_status": repo["parse_status"],
        "graph_version": repo.get("graph_version"),
        **_serialise(graph),
    }


def view_options() -> dict:
    caps = ViewCaps()
    return {
        "node_labels": sorted(str(label) for label in VIEWABLE_NODE_LABELS),
        "edge_types": sorted(str(edge_type) for edge_type in VIEWABLE_EDGE_TYPES),
        "granularities": [str(level) for level in Granularity],
        "page_size": caps.page_size,
        "max_nodes": caps.max_nodes,
        "max_expanded": MAX_EXPANDED,
    }


async def node(repo: dict, node_id: str) -> dict:
    detail = await get_knowledge_graph_store().node_detail(repo["repo_full_name"], node_id)
    if detail is None:
        raise NotFoundError("node not found in this repository", error_code="node_not_found")
    return detail


async def neighbours(
    repo: dict, node_id: str, *, direction: str, depth: int, limit: int, edge_types: list[str] | None,
) -> dict:
    return await get_knowledge_graph_store().neighbours(
        repo["repo_full_name"], node_id,
        direction=direction, edge_types=edge_types, depth=validate_depth(depth), limit=limit,
    )


async def search(repo: dict, term: str, limit: int) -> list[dict]:
    return await get_knowledge_graph_store().search(repo["repo_full_name"], term, limit=limit)


async def stats(repo: dict) -> dict:
    counts = await get_knowledge_graph_store().counts(repo["repo_full_name"])
    indexed = repo.get("graph_stats") or {}
    return {
        "repo_full_name": repo["repo_full_name"],
        "parse_status": repo["parse_status"],
        "graph_version": repo.get("graph_version"),
        "indexed_at": repo.get("indexed_at"),
        **counts,
        **{key: indexed.get(key) for key in ("files", "symbols", "symbol_layer", "resolution", "skipped")},
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
                "expanded": node.expanded,
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
        "containment": [{"parent": line.parent, "child": line.child} for line in view.containment],
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
