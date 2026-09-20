"""The intermediate representation the whole pipeline is built around.

One file in, one `FileFacts` out. Everything downstream — linking, graph
building, the incremental diff — reads these structures and never touches a
syntax tree, so each stage can be tested on plain data.

Facts are:

* **language-neutral** — a Python class and a TypeScript class produce the same
  shapes, so the linker has one code path rather than one per language;
* **immutable** — frozen dataclasses, safe to cache, share between threads and
  send to worker processes;
* **positional, not textual** — we keep names, kinds and line numbers, never
  file contents. Source code is a liability: the less we store, the less there
  is to leak.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

# Bump whenever extraction changes shape or meaning. It is part of the cache
# key, so a bump re-parses every file instead of mixing old and new facts.
PARSER_VERSION = 2

# Symbol kinds. `component` is a function that returns JSX — worth its own kind
# because React repositories are mostly components.
KIND_CLASS = "class"
KIND_FUNCTION = "function"
KIND_METHOD = "method"
KIND_INTERFACE = "interface"
KIND_TYPE = "type"
KIND_CONSTANT = "constant"
KIND_COMPONENT = "component"


@dataclass(frozen=True, slots=True)
class Definition:
    """A class, function, method, interface, type alias or constant."""

    name: str
    kind: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str | None = None
    exported: bool = False
    #: Qualified name of the enclosing definition, e.g. the class of a method.
    parent: str | None = None


@dataclass(frozen=True, slots=True)
class ImportRef:
    """An import statement, as written. Resolution happens later, in link/."""

    specifier: str
    names: tuple[str, ...] = ()
    alias: str | None = None
    line: int = 0
    is_wildcard: bool = False
    is_type_only: bool = False


@dataclass(frozen=True, slots=True)
class CallSite:
    """A call. `callee` is the source text, not a resolved target."""

    callee: str
    line: int
    #: Receiver of a method call: "db" in db.query(). None for a bare call.
    receiver: str | None = None
    #: Qualified name of the definition this call sits inside, if any.
    enclosing: str | None = None
    argument_count: int = 0
    is_construction: bool = False


@dataclass(frozen=True, slots=True)
class RenderSite:
    """JSX usage of a component: <Button/> inside another component."""

    component: str
    line: int
    enclosing: str | None = None


@dataclass(frozen=True, slots=True)
class RouteDef:
    """An HTTP endpoint this repository serves.

    `path` is as written (`/users/{id}`); normalising it for matching is the
    linker's job, because both sides of a match must normalise identically.
    """

    method: str
    path: str
    framework: str
    line: int = 0
    #: Qualified name of the function handling the route, when known.
    handler: str | None = None


@dataclass(frozen=True, slots=True)
class HttpCall:
    """An outbound HTTP request with a literal path.

    Computed URLs are skipped rather than guessed: a call we cannot read
    statically is not a call we can honestly draw an edge for.
    """

    method: str
    path: str
    line: int = 0
    caller: str | None = None


@dataclass(frozen=True, slots=True)
class InheritanceRef:
    """`class A extends B` / `class A(B)` / `implements C`."""

    child: str
    parent_name: str
    kind: str  # "extends" | "implements"
    line: int = 0


@dataclass(frozen=True, slots=True)
class FileFacts:
    """Everything extracted from a single file."""

    path: str  # repo-relative, posix separators
    language: str
    content_hash: str
    loc: int = 0
    definitions: tuple[Definition, ...] = ()
    imports: tuple[ImportRef, ...] = ()
    calls: tuple[CallSite, ...] = ()
    renders: tuple[RenderSite, ...] = ()
    inheritance: tuple[InheritanceRef, ...] = ()
    routes: tuple[RouteDef, ...] = ()
    http_calls: tuple[HttpCall, ...] = ()
    #: A cap was hit; the lists above are complete only up to that cap.
    truncated: bool = False
    #: The parser recovered from syntax errors. Facts are still usable —
    #: tree-sitter is error-tolerant — but coverage may be partial.
    has_syntax_errors: bool = False
    parser_version: int = PARSER_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for storage. Tuples become lists, which is what BSON wants."""
        return asdict(self)


_SEQUENCE_FIELDS = {
    "definitions": Definition,
    "imports": ImportRef,
    "calls": CallSite,
    "renders": RenderSite,
    "inheritance": InheritanceRef,
    "routes": RouteDef,
    "http_calls": HttpCall,
}


def facts_from_dict(data: dict[str, Any]) -> FileFacts:
    """Rebuild facts from storage, ignoring unknown keys from older versions."""
    known = {f for f in FileFacts.__dataclass_fields__}
    payload = {k: v for k, v in data.items() if k in known}
    for name, item_type in _SEQUENCE_FIELDS.items():
        rows = payload.get(name) or ()
        fields = {f for f in item_type.__dataclass_fields__}
        payload[name] = tuple(
            item_type(**{k: v for k, v in row.items() if k in fields}) for row in rows
        )
    return FileFacts(**payload)


@dataclass
class FactsBuilder:
    """Mutable accumulator used while walking one file's syntax tree.

    Extraction is the only place that mutates; `build()` freezes the result.
    Caps are enforced here so no single pathological file can produce an
    unbounded number of facts.
    """

    path: str
    language: str
    content_hash: str
    loc: int = 0
    max_definitions: int = 10_000
    max_calls: int = 50_000
    definitions: list[Definition] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    renders: list[RenderSite] = field(default_factory=list)
    inheritance: list[InheritanceRef] = field(default_factory=list)
    routes: list[RouteDef] = field(default_factory=list)
    http_calls: list[HttpCall] = field(default_factory=list)
    truncated: bool = False
    has_syntax_errors: bool = False

    def add_definition(self, definition: Definition) -> None:
        if len(self.definitions) >= self.max_definitions:
            self.truncated = True
            return
        self.definitions.append(definition)

    def add_import(self, reference: ImportRef) -> None:
        self.imports.append(reference)

    def add_call(self, call: CallSite) -> None:
        if len(self.calls) >= self.max_calls:
            self.truncated = True
            return
        self.calls.append(call)

    def add_render(self, render: RenderSite) -> None:
        if len(self.renders) >= self.max_calls:
            self.truncated = True
            return
        self.renders.append(render)

    def add_inheritance(self, reference: InheritanceRef) -> None:
        self.inheritance.append(reference)

    def add_route(self, route: RouteDef) -> None:
        self.routes.append(route)

    def add_http_call(self, call: HttpCall) -> None:
        self.http_calls.append(call)

    def build(self) -> FileFacts:
        """Freeze into FileFacts, ordered by position for deterministic output."""
        return FileFacts(
            path=self.path,
            language=self.language,
            content_hash=self.content_hash,
            loc=self.loc,
            definitions=tuple(sorted(self.definitions, key=lambda d: (d.start_line, d.qualified_name))),
            imports=tuple(sorted(self.imports, key=lambda i: (i.line, i.specifier))),
            calls=tuple(sorted(self.calls, key=lambda c: (c.line, c.callee))),
            renders=tuple(sorted(self.renders, key=lambda r: (r.line, r.component))),
            inheritance=tuple(sorted(self.inheritance, key=lambda i: (i.line, i.parent_name))),
            routes=tuple(sorted(self.routes, key=lambda r: (r.line, r.method, r.path))),
            http_calls=tuple(sorted(self.http_calls, key=lambda c: (c.line, c.method, c.path))),
            truncated=self.truncated,
            has_syntax_errors=self.has_syntax_errors,
        )
