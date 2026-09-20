"""Interactive, level-of-detail views of a stored knowledge graph.

`model` is the vocabulary, `hierarchy` works out where a node sits from its
id, and `lod` renders a view. All three are pure; the service layer in
`app/services/graph_view_service.py` supplies the data.
"""

from app.knowledge_graph.view.hierarchy import (
    PlacementCache,
    container_chain,
    is_within,
    nearest_visible,
    parent_of,
)
from app.knowledge_graph.view.lod import project
from app.knowledge_graph.view.model import (
    CONTAINMENT_EDGE_TYPES,
    SUMMARY_EDGE_TYPES,
    VIEWABLE_EDGE_TYPES,
    VIEWABLE_NODE_LABELS,
    Granularity,
    RollupEdge,
    ViewCaps,
    ViewEdge,
    ViewFilters,
    ViewGraph,
    ViewNode,
    ViewOverflow,
    ViewRequest,
)

__all__ = [
    "CONTAINMENT_EDGE_TYPES",
    "Granularity",
    "PlacementCache",
    "RollupEdge",
    "SUMMARY_EDGE_TYPES",
    "VIEWABLE_EDGE_TYPES",
    "VIEWABLE_NODE_LABELS",
    "ViewCaps",
    "ViewEdge",
    "ViewFilters",
    "ViewGraph",
    "ViewNode",
    "ViewOverflow",
    "ViewRequest",
    "container_chain",
    "is_within",
    "nearest_visible",
    "parent_of",
    "project",
]
