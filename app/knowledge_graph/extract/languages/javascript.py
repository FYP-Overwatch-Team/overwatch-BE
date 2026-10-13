"""What JavaScript, TypeScript and TSX syntax nodes mean.

One spec covers all three: the grammars differ (TSX has JSX, TypeScript does
not), but the meaning of a class or an import does not. The differences are
expressed as which query fragments are loaded, not as branches in the code.
"""

from tree_sitter import Node

from app.knowledge_graph.extract.languages.base import (
    load_query_fragments,
    node_text,
    truncate,
)
from app.knowledge_graph.extract.languages.http import (
    HTTP_METHODS,
    has_handler_argument,
    is_routable_path,
    looks_like_client,
    method_from_callee,
    string_argument,
)
from app.knowledge_graph.facts import (
    KIND_CLASS,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_TYPE,
    HttpCall,
    ImportRef,
    RouteDef,
)
from app.knowledge_graph.limits import Limits
from app.knowledge_graph.redaction import redact

_KIND_BY_CAPTURE = {
    "definition.class": KIND_CLASS,
    "definition.function": KIND_FUNCTION,
    "definition.method": KIND_METHOD,
    "definition.interface": KIND_INTERFACE,
    "definition.type": KIND_TYPE,
}

_FUNCTION_VALUE_TYPES = ("arrow_function", "function_expression")
#: How far up we look for an `export` wrapper around a declaration.
_EXPORT_LOOKUP_DEPTH = 3


class JavaScriptSpec:
    """Shared implementation; `grammar` and query fragments vary per dialect."""

    def __init__(self, grammar: str, fragments: tuple[str, ...]):
        self.grammar = grammar
        self._fragments = fragments

    def query_source(self) -> str:
        return load_query_fragments(*self._fragments)

    def definition_kind(
        self, capture: str, node: Node, name: str, enclosing_kind: str | None,
    ) -> str | None:
        return _KIND_BY_CAPTURE.get(capture)

    def signature(self, node: Node, name: str, source: bytes, limits: Limits) -> str:
        target = node
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in _FUNCTION_VALUE_TYPES:
                target = value

        parameters = node_text(target.child_by_field_name("parameters"), source)
        returns = node_text(target.child_by_field_name("return_type"), source)
        if node.type in ("class_declaration", "abstract_class_declaration", "interface_declaration"):
            heritage = node_text(_heritage(node), source)
            signature = f"{name} {heritage}".strip()
        else:
            signature = f"{name}{parameters}{returns}"
        return truncate(redact(signature), limits.max_signature_chars)

    def docstring(self, node: Node, source: bytes, limits: Limits) -> str | None:
        """The JSDoc block immediately above a declaration, if there is one."""
        candidate = node
        # For `export function x()` the comment sits above the export wrapper.
        for _ in range(_EXPORT_LOOKUP_DEPTH):
            previous = candidate.prev_named_sibling
            if previous is not None and previous.type == "comment":
                text = node_text(previous, source)
                if text.startswith("/**"):
                    return truncate(redact(_strip_jsdoc(text)), limits.max_docstring_chars) or None
                return None
            if candidate.parent is None:
                return None
            candidate = candidate.parent
        return None

    def is_exported(self, node: Node, name: str) -> bool:
        candidate: Node | None = node
        for _ in range(_EXPORT_LOOKUP_DEPTH):
            if candidate is None:
                return False
            if candidate.type == "export_statement":
                return True
            candidate = candidate.parent
        return False

    def imports_from(self, node: Node, source: bytes) -> list[ImportRef]:
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return []

        names: list[str] = []
        alias: str | None = None
        wildcard = False
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is not None:
            for child in clause.named_children:
                if child.type == "identifier":  # default import
                    names.append("default")
                    alias = node_text(child, source)
                elif child.type == "namespace_import":
                    wildcard = True
                    alias = node_text(child.named_children[-1], source) if child.named_child_count else None
                elif child.type == "named_imports":
                    for specifier in child.named_children:
                        if specifier.type == "import_specifier":
                            names.append(node_text(specifier.child_by_field_name("name"), source))

        return [
            ImportRef(
                specifier=_string_value(node_text(source_node, source)),
                names=tuple(names),
                alias=alias,
                line=node.start_point[0] + 1,
                is_wildcard=wildcard,
                is_type_only=node_text(node, source).lstrip().startswith("import type"),
            )
        ]

    def call_target(self, callee: Node, source: bytes) -> tuple[str, str | None]:
        text = node_text(callee, source)
        if callee.type == "member_expression":
            return text, node_text(callee.child_by_field_name("object"), source) or None
        return text, None

    def base_types(self, class_node: Node, source: bytes) -> list[tuple[str, str]]:
        heritage = _heritage(class_node)
        if heritage is None:
            return []

        bases: list[tuple[str, str]] = []
        for clause in heritage.named_children if heritage.named_child_count else [heritage]:
            kind = "implements" if "implements" in clause.type else "extends"
            for name in _identifier_names(clause, source):
                bases.append((name, kind))
        if not bases:
            # Plain JavaScript: `class A extends B` has no clause node.
            bases = [(name, "extends") for name in _identifier_names(heritage, source)]
        return bases

    def route_from_call(
        self, callee: str, receiver: str | None, call_node: Node, source: bytes,
    ) -> RouteDef | None:
        """Express-style `app.get("/x", handler)`.

        The handler argument is what separates a route from a client call:
        `axios.get("/x")` has none.
        """
        method = method_from_callee(callee)
        if method is None or not has_handler_argument(call_node):
            return None

        path = string_argument(call_node, source)
        if path is None or not is_routable_path(path):
            return None
        return RouteDef(
            method=method,
            path=path,
            framework="express",
            line=call_node.start_point[0] + 1,
        )

    def http_call_from_call(
        self, callee: str, receiver: str | None, call_node: Node, source: bytes,
    ) -> HttpCall | None:
        """`fetch("/api/x")`, `axios.get("/x")`, `api.post("/x", body)`."""
        if has_handler_argument(call_node):
            return None  # that is a route definition, not a request

        if callee == "fetch":
            path = string_argument(call_node, source)
            if path is None or not is_routable_path(path):
                return None
            # The verb lives in an options object we do not read; GET is the
            # default fetch uses when none is given.
            return HttpCall(method="GET", path=path, line=call_node.start_point[0] + 1)

        method = method_from_callee(callee)
        if method is None or not looks_like_client(receiver):
            return None
        path = string_argument(call_node, source)
        if path is None or not is_routable_path(path):
            return None
        return HttpCall(method=method, path=path, line=call_node.start_point[0] + 1)

    def file_routes(self, relative_path: str, definitions: list) -> list[RouteDef]:
        """Next.js App Router: a `route.ts` file serves its own directory."""
        name = relative_path.rpartition("/")[2]
        if name not in _ROUTE_FILE_NAMES:
            return []

        path = _next_route_path(relative_path)
        if path is None:
            return []
        return [
            RouteDef(
                method=definition.name,
                path=path,
                framework="next",
                line=definition.start_line,
                handler=definition.qualified_name,
            )
            for definition in definitions
            if definition.name.lower() in HTTP_METHODS and definition.parent is None
        ]

    def import_from_call(self, callee: str, call_node: Node, source: bytes) -> ImportRef | None:
        """`require("x")` and dynamic `import("x")` are imports written as calls."""
        if callee not in ("require", "import"):
            return None
        arguments = call_node.child_by_field_name("arguments")
        if arguments is None or arguments.named_child_count == 0:
            return None
        first = arguments.named_children[0]
        if first.type != "string":
            return None  # computed specifier: nothing static to resolve
        return ImportRef(
            specifier=_string_value(node_text(first, source)),
            line=call_node.start_point[0] + 1,
        )


#: Next.js serves `app/api/users/route.ts` at `/api/users`, with one exported
#: function per HTTP method.
_ROUTE_FILE_NAMES = ("route.ts", "route.js", "route.tsx", "route.jsx")


def _heritage(class_node: Node) -> Node | None:
    for child in class_node.named_children:
        if child.type in ("class_heritage", "extends_type_clause", "extends_clause"):
            return child
    return None


def _next_route_path(relative_path: str) -> str | None:
    """`app/api/users/[id]/route.ts` -> `/api/users/[id]`.

    Route groups like `(auth)` are organisational and never appear in a URL.
    """
    segments = relative_path.split("/")[:-1]
    if "api" not in segments:
        return None
    segments = segments[segments.index("api"):]
    kept = [s for s in segments if not (s.startswith("(") and s.endswith(")"))]
    return "/" + "/".join(kept) if kept else None


def _identifier_names(node: Node, source: bytes) -> list[str]:
    """Identifier-ish names inside a heritage clause, ignoring type arguments."""
    names: list[str] = []
    for child in node.named_children if node.named_child_count else []:
        if child.type in ("identifier", "type_identifier", "member_expression", "nested_type_identifier"):
            names.append(node_text(child, source))
        elif child.type in ("generic_type", "expression"):
            names.extend(_identifier_names(child, source))
    if not names and node.type in ("identifier", "type_identifier"):
        names.append(node_text(node, source))
    return names


def _string_value(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in "\"'`" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _strip_jsdoc(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip().removeprefix("/**").removesuffix("*/").removeprefix("*").strip()
        if line.startswith("@"):  # @param/@returns tags add noise, not meaning
            break
        if line:
            lines.append(line)
    return " ".join(lines)
