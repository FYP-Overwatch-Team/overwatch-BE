"""Recognising HTTP routes and outbound requests in source code.

Shared by the language specs, because the shapes are nearly identical across
frameworks: a call whose receiver looks like a router and whose arguments
include a handler defines a route; the same call shape *without* a handler is
a client request.

Only literal paths are read. A computed URL (`fetch(url)`, f-strings,
template literals with variables) is skipped rather than guessed — an edge
drawn from a path we cannot actually read would be exactly the kind of
invented structure this product promises not to produce.
"""

from tree_sitter import Node

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

#: Receivers that name an HTTP client rather than a server router. Used only
#: to break ties; the structural test (does an argument look like a handler?)
#: comes first.
CLIENT_RECEIVERS = frozenset({
    "axios", "api", "http", "client", "httpx", "requests", "session", "fetcher",
})

_FUNCTION_ARGUMENT_TYPES = frozenset({
    "arrow_function", "function_expression", "function", "lambda", "identifier",
    "member_expression", "attribute",
})
_STRING_TYPES = frozenset({"string", "template_string", "concatenated_string"})


def string_argument(call_node: Node, source: bytes, index: int = 0) -> str | None:
    """The literal value of one argument, or None if it is not a literal.

    Template strings with interpolation are rejected: half a path is not a
    path we can match on.
    """
    arguments = call_node.child_by_field_name("arguments")
    if arguments is None:
        return None
    named = [child for child in arguments.named_children if child.type != "comment"]
    if index >= len(named):
        return None

    argument = named[index]
    if argument.type not in _STRING_TYPES:
        return None
    if any(child.type in ("template_substitution", "interpolation") for child in argument.named_children):
        return None

    text = source[argument.start_byte:argument.end_byte].decode("utf-8", errors="replace").strip()
    if len(text) >= 2 and text[0] in "\"'`" and text[-1] == text[0]:
        return text[1:-1]
    return None


def has_handler_argument(call_node: Node) -> bool:
    """Whether any argument after the first looks like a request handler."""
    arguments = call_node.child_by_field_name("arguments")
    if arguments is None:
        return False
    named = [child for child in arguments.named_children if child.type != "comment"]
    return any(argument.type in _FUNCTION_ARGUMENT_TYPES for argument in named[1:])


def method_from_callee(callee: str) -> str | None:
    """`app.get` -> GET. None when the call is not an HTTP verb."""
    method = callee.rpartition(".")[2].lower()
    return method.upper() if method in HTTP_METHODS else None


def looks_like_client(receiver: str | None) -> bool:
    if not receiver:
        return False
    root = receiver.partition(".")[0].lower()
    return root in CLIENT_RECEIVERS


def is_routable_path(path: str) -> bool:
    """Paths we can match on: absolute, or absolute behind a host."""
    return path.startswith("/") or path.startswith(("http://", "https://"))
