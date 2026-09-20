"""What an interactive view of the graph is made of.

A view is one *level-of-detail* rendering: the repository seen with some
containers opened and the rest left shut. Opening `api` swaps that one box for
the folders and files inside it; shutting it puts the box back. Everything
else on screen is untouched, so a view can mix granularities — one folder
opened to its symbols while the rest of the repository stays a handful of
boxes.

These are plain dataclasses on purpose: the projection that produces them is
pure, and pure code is the part worth testing exhaustively.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.knowledge_graph.build.model import EdgeType, NodeLabel

#: The edges that describe *containment*. They build the hierarchy a view
#: walks, so they are never rendered as dependencies between nodes.
CONTAINMENT_EDGE_TYPES: frozenset[EdgeType] = frozenset({
    EdgeType.CONTAINS,
    EdgeType.DEFINES,
    EdgeType.HAS_MEMBER,
})

#: The materialised top-level summary. It is *derived* from the import edges,
#: so rolling it up alongside them would count every import twice. The view
#: reads the real edges and never these.
SUMMARY_EDGE_TYPES: frozenset[EdgeType] = frozenset({
    EdgeType.DEPENDS_ON,
    EdgeType.USES_EXTERNAL,
})

#: What a view may draw between nodes, and therefore what a client may filter
#: by. A closed set: it is the whole relationship vocabulary minus the
#: hierarchy and minus the summary.
VIEWABLE_EDGE_TYPES: frozenset[EdgeType] = frozenset(EdgeType) - (
    CONTAINMENT_EDGE_TYPES | SUMMARY_EDGE_TYPES
)

#: What a view may show as a node. The repository itself is the frame the view
#: is drawn inside, never a box within it.
VIEWABLE_NODE_LABELS: frozenset[NodeLabel] = frozenset(NodeLabel) - {NodeLabel.REPOSITORY}


class Granularity(StrEnum):
    """How deep `expand_to` opens the tree in one step."""

    MODULE = "module"
    FILE = "file"
    SYMBOL = "symbol"


#: Which labels a granularity is willing to open *through*. Expanding "to
#: files" opens modules until files appear, and stops there.
OPENS_THROUGH: dict[Granularity, frozenset[NodeLabel]] = {
    Granularity.MODULE: frozenset(),
    Granularity.FILE: frozenset({NodeLabel.MODULE}),
    Granularity.SYMBOL: frozenset({NodeLabel.MODULE, NodeLabel.FILE}),
}


@dataclass(frozen=True, slots=True)
class ViewNode:
    """One box in the view."""

    id: str
    name: str
    label: NodeLabel
    #: Finer than the label: a module's `module`/`external`/`routes`, or a
    #: symbol's `function`/`class`. May be absent for nodes that have no kind.
    kind: str | None = None
    path: str | None = None
    depth: int = 0
    file_count: int = 0
    definition_count: int = 0
    #: How many nodes this one contains. Zero means there is nothing to open.
    child_count: int = 0
    language: str | None = None
    #: The container this node was drawn under. The store leaves it unset —
    #: only the projection knows, because only it knows what is open. It is
    #: what lets the UI offer "close the folder this came out of".
    parent_id: str | None = None

    @property
    def is_container(self) -> bool:
        return self.child_count > 0


@dataclass(frozen=True, slots=True)
class RollupEdge:
    """A relationship as it comes out of the store, before it is rolled up.

    `source`/`target` are real node ids; the labels travel with them because
    an id alone does not say whether `owner/name:api` is a folder or a file.
    """

    source: str
    target: str
    type: EdgeType
    source_label: NodeLabel
    target_label: NodeLabel
    weight: int = 1


@dataclass(frozen=True, slots=True)
class ViewEdge:
    """A relationship between two *visible* nodes.

    `weight` is how many underlying relationships it stands for, and
    `collapsed` says whether either end was rolled up into an ancestor — which
    is what lets the UI show "3 calls inside" rather than implying one call.
    """

    source: str
    target: str
    type: EdgeType
    weight: int = 1
    collapsed: bool = False


@dataclass(frozen=True, slots=True)
class ViewFilters:
    """What the client asked to see. Empty means "everything"."""

    node_labels: frozenset[NodeLabel] = field(default_factory=lambda: VIEWABLE_NODE_LABELS)
    edge_types: frozenset[EdgeType] = field(default_factory=lambda: VIEWABLE_EDGE_TYPES)

    def allows_node(self, label: NodeLabel) -> bool:
        return label in self.node_labels

    def allows_edge(self, edge_type: EdgeType) -> bool:
        return edge_type in self.edge_types


@dataclass(frozen=True, slots=True)
class ViewCaps:
    """Ceilings applied to one view, so no request can ask for the whole graph."""

    max_nodes: int = 400
    max_edges: int = 1_200


@dataclass(frozen=True, slots=True)
class ViewGraph:
    """The finished view."""

    nodes: tuple[ViewNode, ...] = ()
    edges: tuple[ViewEdge, ...] = ()
    #: Containers that are actually open, described rather than just named:
    #: the client needs their names for the trail, and they are not on screen
    #: to be looked up. An id the client asked to expand but whose parent is
    #: shut is absent, so the client can drop it instead of resending it.
    expanded: tuple[ViewNode, ...] = ()
    #: True when a cap cut the result short, so the UI can say so instead of
    #: quietly showing a partial picture.
    truncated: bool = False

    def node_ids(self) -> set[str]:
        return {node.id for node in self.nodes}

    def expanded_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.expanded)


@dataclass(frozen=True, slots=True)
class ViewRequest:
    """Everything the projection needs to know about what was asked for."""

    expanded: Sequence[str] = ()
    filters: ViewFilters = field(default_factory=ViewFilters)
    caps: ViewCaps = field(default_factory=ViewCaps)
