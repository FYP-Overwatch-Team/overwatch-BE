from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge_graph.build.model import EdgeType, NodeLabel
from app.knowledge_graph.view.model import VIEWABLE_EDGE_TYPES, VIEWABLE_NODE_LABELS, Granularity, ViewFilters

#: Containers one view request may ask to open. Matches the view service's own cap,
#: so a larger list is rejected at the edge rather than silently ignored.
MAX_EXPANDED = 64
MAX_NODE_ID = 512
MAX_SOURCE_PATH = 1024
#: Lines of a file sent for reading; longer files are cut, which is plenty for any citation.
MAX_SOURCE_FILE_LINES = 5_000

#: A node id as it arrives from a client. Bounded, and never concatenated into a query.
NodeId = Annotated[str, Field(min_length=1, max_length=MAX_NODE_ID)]


class GraphViewBody(BaseModel):
    """What to open and what to show.

    Both filters are lists of enum members, so an unknown label or relationship type
    is rejected by validation and never reaches a query.
    """

    model_config = ConfigDict(extra="forbid")

    #: Containers to open. Ids whose parent is shut are ignored; the response reports what is open.
    expand: list[NodeId] = Field(default_factory=list, max_length=MAX_EXPANDED)
    #: Containers whose children are drawn in full rather than paged (a "6 more" click).
    reveal: list[NodeId] = Field(default_factory=list, max_length=MAX_EXPANDED)
    #: Open everything down to this level in one step.
    expand_to: Granularity | None = None
    #: Confines `expand_to` to one subtree; without it the whole repository opens.
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


class ModuleNode(BaseModel):
    id: str
    name: str
    kind: str | None
    file_count: int | None
    definition_count: int | None


class ModuleEdge(BaseModel):
    source: str
    target: str
    type: str
    weight: int | None


class ModuleGraph(BaseModel):
    """The module-level diagram (`GET /graph`)."""

    repo_full_name: str
    parse_status: str
    nodes: list[ModuleNode]
    edges: list[ModuleEdge]


class ViewNode(BaseModel):
    id: str
    name: str
    label: str
    kind: str | None
    path: str | None
    depth: int
    file_count: int
    definition_count: int
    child_count: int
    language: str | None
    parent_id: str | None
    expandable: bool
    expanded: bool


class ViewEdge(BaseModel):
    source: str
    target: str
    type: str
    weight: int
    collapsed: bool


class Containment(BaseModel):
    parent: str
    child: str


class Overflow(BaseModel):
    id: str
    container_id: str
    container_name: str
    shown: int
    hidden: int


class ExpandedNode(BaseModel):
    id: str
    name: str
    label: str
    kind: str | None
    path: str | None
    parent_id: str | None


class GraphView(BaseModel):
    repo_full_name: str
    parse_status: str
    graph_version: str | None
    nodes: list[ViewNode]
    edges: list[ViewEdge]
    containment: list[Containment]
    overflows: list[Overflow]
    expanded: list[ExpandedNode]
    truncated: bool


class ViewOptions(BaseModel):
    node_labels: list[str]
    edge_types: list[str]
    granularities: list[str]
    #: lets the UI warn before a click that a folder is wider than one page
    page_size: int
    max_nodes: int
    #: how many containers one view request may open
    max_expanded: int


class NodeChild(BaseModel):
    id: str
    name: str | None
    kind: str | None
    path: str | None


class NodeDetail(BaseModel):
    id: str
    labels: list[str]
    properties: dict[str, Any]
    children: list[NodeChild]


class NeighbourNode(BaseModel):
    id: str
    name: str | None
    kind: str | None
    labels: list[str]


class NeighbourEdge(BaseModel):
    source: str
    target: str
    type: str
    count: int | None
    weight: int | None


class Neighbourhood(BaseModel):
    nodes: list[NeighbourNode]
    edges: list[NeighbourEdge]


class SearchResult(BaseModel):
    id: str
    name: str | None
    kind: str | None
    path: str | None
    file_path: str | None
    signature: str | None
    labels: list[str]


class GraphStats(BaseModel):
    """What the graph holds, and how much of the code it could resolve (counted, not hidden)."""

    repo_full_name: str
    parse_status: str
    graph_version: str | None
    indexed_at: datetime | None
    nodes_by_label: dict[str, int]
    edges_by_type: dict[str, int]
    files: int | None
    symbols: int | None
    symbol_layer: Any
    resolution: Any
    skipped: Any


class SourceFile(BaseModel):
    """A file of the repository as of one commit, for reading the code an answer cites."""

    path: str
    commit_sha: str
    #: what to highlight it as; null when unknown
    language: str | None
    #: the first `MAX_SOURCE_FILE_LINES` lines at most
    content: str
    line_count: int
    truncated: bool
    #: whether `commit_sha` is the commit the graph is built from now
    is_current: bool
