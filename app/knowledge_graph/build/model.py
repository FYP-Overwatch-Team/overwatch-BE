"""The graph as plain data, independent of any database.

A `GraphSnapshot` is the complete picture of one repository at one commit.
The store's job is to make the database match it; nothing here knows how.

Labels and relationship types are enums because they cannot be query
parameters in Cypher — they are concatenated into the query string. Keeping
them a closed set defined in code (never derived from repository content) is
what makes that safe.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeLabel(StrEnum):
    REPOSITORY = "Repository"
    MODULE = "Module"
    FILE = "File"
    SYMBOL = "Symbol"
    PACKAGE = "Package"
    ROUTE = "Route"


class EdgeType(StrEnum):
    #: Structure: repository → module → file → symbol.
    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    HAS_MEMBER = "HAS_MEMBER"
    #: Dependencies between files and on packages.
    IMPORTS = "IMPORTS"
    USES_PACKAGE = "USES_PACKAGE"
    #: References between symbols.
    CALLS = "CALLS"
    RENDERS = "RENDERS"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    #: HTTP layer: who serves a route, and who calls it.
    HANDLES = "HANDLES"
    REQUESTS = "REQUESTS"
    #: Materialised module-level view, which the dashboard reads directly.
    DEPENDS_ON = "DEPENDS_ON"
    USES_EXTERNAL = "USES_EXTERNAL"


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    label: NodeLabel
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    type: EdgeType
    properties: Mapping[str, Any] = field(default_factory=dict)
    #: The file whose code produced this edge. Incremental updates use it to
    #: know what to remove when that file changes.
    origin_file: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source, self.target, str(self.type)


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """One repository at one commit."""

    repo_full_name: str
    version: str
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    stats: Mapping[str, Any] = field(default_factory=dict)

    def nodes_by_id(self) -> dict[str, GraphNode]:
        return {node.id: node for node in self.nodes}

    def edges_by_key(self) -> dict[tuple[str, str, str], GraphEdge]:
        return {edge.key: edge for edge in self.edges}

    def count_by_label(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes:
            counts[str(node.label)] = counts.get(str(node.label), 0) + 1
        return counts

    def count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for edge in self.edges:
            counts[str(edge.type)] = counts.get(str(edge.type), 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class GraphDelta:
    """The difference between two snapshots: what to write, what to remove."""

    repo_full_name: str
    version: str
    upserted_nodes: tuple[GraphNode, ...] = ()
    upserted_edges: tuple[GraphEdge, ...] = ()
    deleted_node_ids: tuple[str, ...] = ()
    deleted_edge_keys: tuple[tuple[str, str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.upserted_nodes or self.upserted_edges
            or self.deleted_node_ids or self.deleted_edge_keys
        )

    def summary(self) -> dict[str, int]:
        return {
            "nodes_upserted": len(self.upserted_nodes),
            "edges_upserted": len(self.upserted_edges),
            "nodes_deleted": len(self.deleted_node_ids),
            "edges_deleted": len(self.deleted_edge_keys),
        }
