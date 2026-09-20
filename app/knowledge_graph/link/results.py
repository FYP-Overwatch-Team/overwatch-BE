"""What linking produces: resolved references plus an honest coverage report.

The graph builder (PR 5) consumes these; nothing here knows about Neo4j.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class EdgeKind(StrEnum):
    CALLS = "calls"
    RENDERS = "renders"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"


@dataclass(frozen=True, slots=True)
class SymbolRef:
    """A definition, addressed by the file that owns it."""

    file: str
    qualified_name: str


@dataclass(frozen=True, slots=True)
class FileImport:
    """A resolved file-to-file import."""

    source_file: str
    target_file: str
    names: tuple[str, ...] = ()
    line: int = 0


@dataclass(frozen=True, slots=True)
class PackageUse:
    """A file's dependency on a third-party package."""

    source_file: str
    package: str
    names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteLink:
    """An HTTP endpoint this repository serves."""

    file: str
    method: str
    path: str
    framework: str
    line: int = 0
    #: Qualified name of the handler, when the framework makes it knowable.
    handler: str | None = None

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True, slots=True)
class RequestLink:
    """A call to one of this repository's own routes."""

    source_file: str
    source_symbol: str | None
    method: str
    path: str
    line: int = 0

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True, slots=True)
class SymbolEdge:
    """One symbol referencing another: a call, a render, or inheritance."""

    source_file: str
    #: None when the reference sits at file level rather than inside a symbol.
    source_symbol: str | None
    target: SymbolRef
    kind: EdgeKind
    count: int = 1
    lines: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolutionStats:
    """Coverage, reported rather than implied.

    Resolution is partial by design: a call on a value whose type we do not
    track is left unresolved instead of guessed. These numbers are what make
    that visible — in `graph_stats` and, later, in the UI.
    """

    imports_total: int = 0
    imports_to_files: int = 0
    imports_to_packages: int = 0
    imports_unresolved: int = 0
    calls_total: int = 0
    calls_resolved: int = 0
    #: Calls into a third-party package. Accounted for, just not an edge
    #: between two of this repository's symbols.
    calls_to_packages: int = 0
    renders_total: int = 0
    renders_resolved: int = 0
    renders_to_packages: int = 0
    inheritance_total: int = 0
    inheritance_resolved: int = 0
    inheritance_to_packages: int = 0
    routes_total: int = 0
    http_calls_total: int = 0
    #: Requests matched to a route in this repository. The rest point at
    #: services we do not index, or at routes mounted behind a prefix we
    #: cannot read — counted, never guessed at.
    http_calls_matched: int = 0

    @property
    def internal_import_resolution(self) -> float:
        """Share of non-package imports that found a file (0.0 when there are none).

        Package imports are excluded: they are correctly resolved, just not to
        a file, and including them would flatter the number.
        """
        internal = self.imports_to_files + self.imports_unresolved
        return self.imports_to_files / internal if internal else 1.0

    @property
    def call_resolution(self) -> float:
        """Share of calls that became an edge between two internal symbols."""
        return self.calls_resolved / self.calls_total if self.calls_total else 1.0

    @property
    def calls_accounted(self) -> float:
        """Share of calls whose destination is known: internal or a package.

        The remainder are calls on values we have no type for — dynamic
        dispatch, callbacks, methods on objects. We leave those out rather
        than guess.
        """
        known = self.calls_resolved + self.calls_to_packages
        return known / self.calls_total if self.calls_total else 1.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "imports_total": self.imports_total,
            "imports_to_files": self.imports_to_files,
            "imports_to_packages": self.imports_to_packages,
            "imports_unresolved": self.imports_unresolved,
            "internal_import_resolution": round(self.internal_import_resolution, 4),
            "calls_total": self.calls_total,
            "calls_resolved": self.calls_resolved,
            "calls_to_packages": self.calls_to_packages,
            "call_resolution": round(self.call_resolution, 4),
            "calls_accounted": round(self.calls_accounted, 4),
            "renders_total": self.renders_total,
            "renders_resolved": self.renders_resolved,
            "renders_to_packages": self.renders_to_packages,
            "inheritance_total": self.inheritance_total,
            "inheritance_resolved": self.inheritance_resolved,
            "inheritance_to_packages": self.inheritance_to_packages,
            "routes_total": self.routes_total,
            "http_calls_total": self.http_calls_total,
            "http_calls_matched": self.http_calls_matched,
        }


@dataclass(frozen=True, slots=True)
class RepositoryLinks:
    """Everything the linker resolved for one repository."""

    files: tuple[str, ...] = ()
    imports: tuple[FileImport, ...] = ()
    packages: tuple[PackageUse, ...] = ()
    edges: tuple[SymbolEdge, ...] = ()
    routes: tuple[RouteLink, ...] = ()
    requests: tuple[RequestLink, ...] = ()
    stats: ResolutionStats = field(default_factory=ResolutionStats)

    def edges_of(self, kind: EdgeKind) -> tuple[SymbolEdge, ...]:
        return tuple(edge for edge in self.edges if edge.kind is kind)
