"""Building: facts and links become nodes, edges and a delta to write."""

from app.knowledge_graph.build.builder import build_snapshot
from app.knowledge_graph.build.diff import diff_snapshots, full_delta
from app.knowledge_graph.build.model import (
    EdgeType,
    GraphDelta,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    NodeLabel,
)

__all__ = [
    "EdgeType",
    "GraphDelta",
    "GraphEdge",
    "GraphNode",
    "GraphSnapshot",
    "NodeLabel",
    "build_snapshot",
    "diff_snapshots",
    "full_delta",
]
