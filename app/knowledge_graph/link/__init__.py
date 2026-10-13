"""Linking: specifiers and names become files and symbols.

Pure computation over facts and a known file set — this stage never touches
the filesystem or the network.
"""

from app.knowledge_graph.link.http import RouteKey, normalise_path, route_key
from app.knowledge_graph.link.results import (
    EdgeKind,
    FileImport,
    PackageUse,
    RequestLink,
    RouteLink,
    RepositoryLinks,
    ResolutionStats,
    SymbolEdge,
    SymbolRef,
)
from app.knowledge_graph.link.symbols import link_repository

__all__ = [
    "EdgeKind",
    "FileImport",
    "PackageUse",
    "RequestLink",
    "RouteKey",
    "RouteLink",
    "normalise_path",
    "route_key",
    "RepositoryLinks",
    "ResolutionStats",
    "SymbolEdge",
    "SymbolRef",
    "link_repository",
]
