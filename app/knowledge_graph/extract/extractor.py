"""Source file in, FileFacts out.

The work is split so that the expensive part stays in C and the meaning stays
in Python:

1. tree-sitter parses the file and evaluates the language's query — both in C,
   and the only stages that touch every byte.
2. This module walks the *matches* (thousands, not millions of nodes),
   resolves scopes, and asks the language spec what each match means.

Measured on real repositories: ~100k lines per second per core, with query
evaluation roughly matching parse time. See docs/knowledge-graph-plan.md §5.

Pure and deterministic: no I/O, no globals except a query cache, and output
ordered by position. The same bytes always produce the same facts.
"""

from dataclasses import dataclass, replace
from functools import lru_cache

from tree_sitter import Node, Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser

from app.knowledge_graph.discovery import SourceFile
from app.knowledge_graph.extract.languages import LanguageSpec, spec_for
from app.knowledge_graph.extract.languages.base import (
    CAPTURE_CALL,
    CAPTURE_CALLEE,
    CAPTURE_COMPONENT,
    CAPTURE_CONSTRUCTION,
    CAPTURE_IMPORT,
    CAPTURE_NAME,
    DEFINITION_PREFIX,
    node_text,
)
from app.knowledge_graph.facts import (
    KIND_CLASS,
    KIND_COMPONENT,
    KIND_FUNCTION,
    CallSite,
    Definition,
    FactsBuilder,
    FileFacts,
    InheritanceRef,
    RenderSite,
)
from app.knowledge_graph.limits import DEFAULT_LIMITS, Limits


class UnsupportedLanguage(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _CapturedDefinition:
    """A definition node plus what the spec decided it means."""

    node: Node
    name: str
    kind: str
    qualified_name: str


@lru_cache(maxsize=16)
def _compiled_query(language: str) -> Query:
    """Queries are compiled once per process; compilation is not cheap."""
    spec = spec_for(language)
    if spec is None:
        raise UnsupportedLanguage(language)
    return Query(get_language(spec.grammar), spec.query_source())


def extract(source: SourceFile, limits: Limits = DEFAULT_LIMITS) -> FileFacts:
    """Extract facts from one already-read, already-validated source file."""
    spec = spec_for(source.language)
    if spec is None:
        raise UnsupportedLanguage(source.language)

    tree = get_parser(spec.grammar).parse(source.source)
    root = tree.root_node
    builder = FactsBuilder(
        path=source.relative_path,
        language=source.language,
        content_hash=source.content_hash,
        loc=source.loc,
        max_definitions=limits.max_definitions_per_file,
        max_calls=limits.max_calls_per_file,
    )
    # tree-sitter recovers from syntax errors rather than failing, so partial
    # facts from a file with errors are still worth keeping — flagged as such.
    builder.has_syntax_errors = root.has_error

    matches = QueryCursor(_compiled_query(source.language)).matches(root)
    definitions = _collect_definitions(matches, spec, source.source)
    _emit_definitions(builder, definitions, spec, source.source, limits)
    _emit_references(builder, matches, definitions, spec, source.source)
    _promote_components(builder, definitions)
    _emit_file_routes(builder, spec, source.relative_path)

    return builder.build()


def _collect_definitions(
    matches: list, spec: LanguageSpec, source: bytes,
) -> dict[int, _CapturedDefinition]:
    """Definition nodes keyed by node id, with qualified names resolved.

    Two passes are needed: a definition's qualified name depends on its
    ancestors, which may appear later in the match list.
    """
    raw: dict[int, tuple[Node, str, str]] = {}  # node id -> (node, capture, name)
    for _, captures in matches:
        capture = next((c for c in captures if c.startswith(DEFINITION_PREFIX)), None)
        if capture is None:
            continue
        node = captures[capture][0]
        name_nodes = captures.get(CAPTURE_NAME)
        if not name_nodes:
            continue
        raw[node.id] = (node, capture, node_text(name_nodes[0], source))

    resolved: dict[int, _CapturedDefinition] = {}
    for node_id, (node, capture, name) in raw.items():
        ancestors = _ancestor_definitions(node, raw)
        enclosing_kind = None
        if ancestors:
            parent_node, parent_capture, parent_name = ancestors[-1]
            enclosing_kind = spec.definition_kind(
                parent_capture, parent_node, parent_name, None,
            )
        kind = spec.definition_kind(capture, node, name, enclosing_kind)
        if kind is None:
            continue
        qualified_name = ".".join([*(n for _, _, n in ancestors), name])
        resolved[node_id] = _CapturedDefinition(node, name, kind, qualified_name)
    return resolved


def _ancestor_definitions(
    node: Node, raw: dict[int, tuple[Node, str, str]],
) -> list[tuple[Node, str, str]]:
    """Captured definitions enclosing `node`, outermost first."""
    chain: list[tuple[Node, str, str]] = []
    current = node.parent
    while current is not None:
        entry = raw.get(current.id)
        if entry is not None:
            chain.append(entry)
        current = current.parent
    chain.reverse()
    return chain


def _emit_definitions(
    builder: FactsBuilder,
    definitions: dict[int, _CapturedDefinition],
    spec: LanguageSpec,
    source: bytes,
    limits: Limits,
) -> None:
    for definition in definitions.values():
        node = definition.node
        parent = definition.qualified_name.rpartition(".")[0] or None
        builder.add_definition(
            Definition(
                name=definition.name,
                kind=definition.kind,
                qualified_name=definition.qualified_name,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=spec.signature(node, definition.name, source, limits),
                docstring=spec.docstring(node, source, limits),
                exported=spec.is_exported(node, definition.name),
                parent=parent,
            )
        )
        if definition.kind == KIND_CLASS:
            for base_name, relation in spec.base_types(node, source):
                builder.add_inheritance(
                    InheritanceRef(
                        child=definition.qualified_name,
                        parent_name=base_name,
                        kind=relation,
                        line=node.start_point[0] + 1,
                    )
                )


def _emit_references(
    builder: FactsBuilder,
    matches: list,
    definitions: dict[int, _CapturedDefinition],
    spec: LanguageSpec,
    source: bytes,
) -> None:
    """Imports, calls and JSX renders, each attributed to its enclosing definition."""
    for _, captures in matches:
        if CAPTURE_IMPORT in captures:
            for reference in spec.imports_from(captures[CAPTURE_IMPORT][0], source):
                builder.add_import(reference)
            continue

        call_node = (captures.get(CAPTURE_CALL) or captures.get(CAPTURE_CONSTRUCTION) or [None])[0]
        if call_node is not None and CAPTURE_CALLEE in captures:
            callee_node = captures[CAPTURE_CALLEE][0]
            callee, receiver = spec.call_target(callee_node, source)
            hidden_import = spec.import_from_call(callee, call_node, source)
            if hidden_import is not None:
                builder.add_import(hidden_import)
                continue
            route = spec.route_from_call(callee, receiver, call_node, source)
            if route is not None:
                builder.add_route(
                    replace(route, handler=route.handler or _decorated_name(call_node, definitions))
                )
            else:
                request = spec.http_call_from_call(callee, receiver, call_node, source)
                if request is not None:
                    builder.add_http_call(
                        replace(request, caller=_enclosing_name(call_node, definitions))
                    )

            arguments = call_node.child_by_field_name("arguments")
            builder.add_call(
                CallSite(
                    callee=callee,
                    line=call_node.start_point[0] + 1,
                    receiver=receiver,
                    enclosing=_enclosing_name(call_node, definitions),
                    argument_count=arguments.named_child_count if arguments else 0,
                    is_construction=CAPTURE_CONSTRUCTION in captures,
                )
            )
            continue

        if CAPTURE_COMPONENT in captures:
            component_node = captures[CAPTURE_COMPONENT][0]
            component = node_text(component_node, source)
            # Lowercase names are HTML tags (<div>), not components.
            if component[:1].isupper():
                builder.add_render(
                    RenderSite(
                        component=component,
                        line=component_node.start_point[0] + 1,
                        enclosing=_enclosing_name(component_node, definitions),
                    )
                )


def _decorated_name(call_node: Node, definitions: dict[int, _CapturedDefinition]) -> str | None:
    """The function a decorator is attached to.

    A decorator is a *sibling* of the function it decorates, not an ancestor,
    so the usual scope walk finds nothing; the handler has to be looked up
    through the shared parent.
    """
    decorator = call_node.parent
    decorated = decorator.parent if decorator is not None else None
    if decorated is None or decorated.type != "decorated_definition":
        return _enclosing_name(call_node, definitions)

    for child in decorated.named_children:
        definition = definitions.get(child.id)
        if definition is not None:
            return definition.qualified_name
    return None


def _emit_file_routes(builder: FactsBuilder, spec: LanguageSpec, relative_path: str) -> None:
    """Routes a framework derives from where the file lives (Next.js)."""
    for route in spec.file_routes(relative_path, builder.definitions):
        builder.add_route(route)


def _enclosing_name(node: Node, definitions: dict[int, _CapturedDefinition]) -> str | None:
    current = node.parent
    while current is not None:
        definition = definitions.get(current.id)
        if definition is not None:
            return definition.qualified_name
        current = current.parent
    return None


def _promote_components(builder: FactsBuilder, definitions: dict[int, _CapturedDefinition]) -> None:
    """Re-label functions that render JSX as components.

    A React component is a function that returns markup and is named in
    PascalCase. Both signals are required: `useThing()` may build JSX without
    being a component, and a PascalCase helper may render nothing.
    """
    rendering = {render.enclosing for render in builder.renders if render.enclosing}
    if not rendering:
        return
    builder.definitions[:] = [
        Definition(**{**_as_kwargs(definition), "kind": KIND_COMPONENT})
        if (
            definition.kind == KIND_FUNCTION
            and definition.qualified_name in rendering
            and definition.name[:1].isupper()
        )
        else definition
        for definition in builder.definitions
    ]


def _as_kwargs(definition: Definition) -> dict:
    return {field: getattr(definition, field) for field in Definition.__dataclass_fields__}
