"""What Python syntax nodes mean, for the generic extractor."""

from tree_sitter import Node

from app.knowledge_graph.extract.languages.base import (
    load_query_fragments,
    node_text,
    truncate,
)
from app.knowledge_graph.extract.languages.http import (
    has_handler_argument,
    is_routable_path,
    looks_like_client,
    method_from_callee,
    string_argument,
)
from app.knowledge_graph.facts import (
    KIND_CLASS,
    KIND_CONSTANT,
    KIND_FUNCTION,
    KIND_METHOD,
    HttpCall,
    ImportRef,
    RouteDef,
)
from app.knowledge_graph.limits import Limits
from app.knowledge_graph.redaction import redact

_STRING_QUOTES = ("\"\"\"", "'''", '"', "'")


class PythonSpec:
    grammar = "python"

    def query_source(self) -> str:
        return load_query_fragments("python.scm")

    def definition_kind(
        self, capture: str, node: Node, name: str, enclosing_kind: str | None,
    ) -> str | None:
        if capture == "definition.class":
            return KIND_CLASS
        if capture == "definition.function":
            return KIND_METHOD if enclosing_kind == KIND_CLASS else KIND_FUNCTION
        if capture == "definition.constant":
            # Only module-level SCREAMING_CASE names; every other assignment is
            # a local variable, which is noise in an architecture graph.
            if enclosing_kind is None and len(name) > 1 and name.isupper():
                return KIND_CONSTANT
        return None

    def signature(self, node: Node, name: str, source: bytes, limits: Limits) -> str:
        if node.type == "function_definition":
            parameters = node_text(node.child_by_field_name("parameters"), source)
            returns = node_text(node.child_by_field_name("return_type"), source)
            signature = f"{name}{parameters}" + (f" -> {returns}" if returns else "")
        elif node.type == "class_definition":
            bases = node_text(node.child_by_field_name("superclasses"), source)
            signature = f"{name}{bases}"
        else:
            signature = name
        return truncate(redact(signature), limits.max_signature_chars)

    def docstring(self, node: Node, source: bytes, limits: Limits) -> str | None:
        body = node.child_by_field_name("body")
        if body is None or body.named_child_count == 0:
            return None
        first = body.named_children[0]
        # Grammar versions differ: the docstring is either a bare `string` or
        # an `expression_statement` wrapping one.
        if first.type == "expression_statement":
            first = first.named_children[0] if first.named_child_count else first
        literal = first
        if literal.type != "string":
            return None
        return truncate(
            redact(_strip_quotes(node_text(literal, source))), limits.max_docstring_chars,
        ) or None

    def is_exported(self, node: Node, name: str) -> bool:
        # Python has no export keyword; the leading-underscore convention is
        # what readers and linters treat as private.
        return not name.startswith("_")

    def imports_from(self, node: Node, source: bytes) -> list[ImportRef]:
        line = node.start_point[0] + 1
        if node.type == "import_statement":
            return [
                ImportRef(
                    specifier=node_text(target, source),
                    alias=node_text(alias, source) or None,
                    line=line,
                )
                for target, alias in _plain_import_targets(node)
            ]

        if node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is None:
                return []
            names: list[str] = []
            wildcard = False
            for child in node.named_children:
                if child == module:
                    continue
                if child.type == "wildcard_import":
                    wildcard = True
                elif child.type == "dotted_name":
                    names.append(node_text(child, source))
                elif child.type == "aliased_import":
                    names.append(node_text(child.child_by_field_name("name"), source))
            return [
                ImportRef(
                    specifier=node_text(module, source),
                    names=tuple(names),
                    line=line,
                    is_wildcard=wildcard,
                )
            ]
        return []

    def call_target(self, callee: Node, source: bytes) -> tuple[str, str | None]:
        text = node_text(callee, source)
        if callee.type == "attribute":
            return text, node_text(callee.child_by_field_name("object"), source) or None
        return text, None

    def base_types(self, class_node: Node, source: bytes) -> list[tuple[str, str]]:
        superclasses = class_node.child_by_field_name("superclasses")
        if superclasses is None:
            return []
        return [
            (node_text(child, source), "extends")
            for child in superclasses.named_children
            if child.type in ("identifier", "attribute")
        ]

    def import_from_call(self, callee: str, call_node: Node, source: bytes) -> ImportRef | None:
        return None  # importlib.import_module is rare and rarely a literal

    def route_from_call(
        self, callee: str, receiver: str | None, call_node: Node, source: bytes,
    ) -> RouteDef | None:
        """FastAPI and Flask declare routes with decorators: `@router.get("/x")`."""
        if not _is_decorator(call_node):
            return None

        path = string_argument(call_node, source)
        if path is None or not is_routable_path(path):
            return None

        method = method_from_callee(callee)
        if method is None:
            # Flask's `@app.route("/x", methods=["POST"])`; GET when unstated.
            if callee.rpartition(".")[2] != "route":
                return None
            method = _flask_method(call_node, source)

        return RouteDef(
            method=method,
            path=path,
            framework="python",
            line=call_node.start_point[0] + 1,
            handler=None,  # filled in by the extractor, which knows the scope
        )

    def http_call_from_call(
        self, callee: str, receiver: str | None, call_node: Node, source: bytes,
    ) -> HttpCall | None:
        """`requests.get("https://…")`, `client.post("/x")` and friends."""
        method = method_from_callee(callee)
        if method is None or _is_decorator(call_node) or has_handler_argument(call_node):
            return None
        if not looks_like_client(receiver):
            return None

        path = string_argument(call_node, source)
        if path is None or not is_routable_path(path):
            return None
        return HttpCall(method=method, path=path, line=call_node.start_point[0] + 1)

    def file_routes(self, relative_path: str, definitions: list) -> list[RouteDef]:
        return []  # Python frameworks declare routes in code, not by filename


def _is_decorator(call_node: Node) -> bool:
    parent = call_node.parent
    return parent is not None and parent.type == "decorator"


def _flask_method(call_node: Node, source: bytes) -> str:
    """The first verb in `methods=[...]`, defaulting to GET as Flask does."""
    arguments = call_node.child_by_field_name("arguments")
    for argument in arguments.named_children if arguments else []:
        if argument.type != "keyword_argument":
            continue
        if node_text(argument.child_by_field_name("name"), source) != "methods":
            continue
        value = node_text(argument.child_by_field_name("value"), source)
        for verb in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            if verb in value.upper():
                return verb
    return "GET"


def _plain_import_targets(node: Node) -> list[tuple[Node, Node | None]]:
    targets: list[tuple[Node, Node | None]] = []
    for child in node.named_children:
        if child.type == "aliased_import":
            targets.append((child.child_by_field_name("name"), child.child_by_field_name("alias")))
        elif child.type == "dotted_name":
            targets.append((child, None))
    return targets


def _strip_quotes(text: str) -> str:
    text = text.strip()
    for prefix in ("r", "b", "f", "u", "rb", "br"):
        if text.lower().startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    for quote in _STRING_QUOTES:
        if text.startswith(quote) and text.endswith(quote) and len(text) >= 2 * len(quote):
            return text[len(quote): -len(quote)].strip()
    return text
