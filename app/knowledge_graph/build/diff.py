"""What changed between two snapshots.

Measured earlier: writing a whole graph to Neo4j is the slowest stage by far
(84s for a 3.9M-line repository, against 12s to parse it). Pushes touch a
handful of files, so an incremental sync writes a delta instead.

Comparing whole snapshots — rather than tracking dirty files — is what keeps
this correct in the awkward case: renaming a function changes edges in files
that did not themselves change.
"""

from app.knowledge_graph.build.model import GraphDelta, GraphEdge, GraphNode, GraphSnapshot


def diff_snapshots(previous: GraphSnapshot, current: GraphSnapshot) -> GraphDelta:
    """The writes and deletions that turn `previous` into `current`."""
    if previous.repo_full_name != current.repo_full_name:
        raise ValueError("cannot diff snapshots of different repositories")

    previous_nodes = previous.nodes_by_id()
    current_nodes = current.nodes_by_id()
    previous_edges = previous.edges_by_key()
    current_edges = current.edges_by_key()

    upserted_nodes = tuple(
        node for node_id, node in current_nodes.items()
        if _node_changed(previous_nodes.get(node_id), node)
    )
    upserted_edges = tuple(
        edge for key, edge in current_edges.items()
        if _edge_changed(previous_edges.get(key), edge)
    )

    deleted_node_ids = tuple(sorted(set(previous_nodes) - set(current_nodes)))
    deleted_edge_keys = tuple(sorted(set(previous_edges) - set(current_edges)))

    return GraphDelta(
        repo_full_name=current.repo_full_name,
        version=current.version,
        upserted_nodes=upserted_nodes,
        upserted_edges=upserted_edges,
        deleted_node_ids=deleted_node_ids,
        deleted_edge_keys=deleted_edge_keys,
    )


def full_delta(snapshot: GraphSnapshot) -> GraphDelta:
    """Everything in a snapshot, for a first index."""
    return GraphDelta(
        repo_full_name=snapshot.repo_full_name,
        version=snapshot.version,
        upserted_nodes=snapshot.nodes,
        upserted_edges=snapshot.edges,
    )


def _node_changed(previous: GraphNode | None, current: GraphNode) -> bool:
    if previous is None:
        return True
    return previous.label != current.label or previous.properties != current.properties


def _edge_changed(previous: GraphEdge | None, current: GraphEdge) -> bool:
    if previous is None:
        return True
    return (
        previous.properties != current.properties
        or previous.origin_file != current.origin_file
    )
