"""The contract every language implementation fills in.

Adding a language means adding one module here plus its query fragments; the
extractor itself never grows a language-specific branch.

The split is deliberate: **queries** say *which* syntax nodes are interesting
(fast, evaluated in C), and a **spec** says what they *mean* (names,
signatures, imports). Anything needing scope or text lives in the spec,
because queries cannot express it.
"""

from pathlib import Path
from typing import Protocol

from tree_sitter import Node

from app.knowledge_graph.facts import HttpCall, ImportRef, RouteDef
from app.knowledge_graph.limits import Limits

QUERIES_DIR = Path(__file__).resolve().parent.parent / "queries"

# Capture names shared by every language's queries.
CAPTURE_NAME = "name"
CAPTURE_CALLEE = "callee"
CAPTURE_COMPONENT = "component"
CAPTURE_IMPORT = "import"
CAPTURE_CALL = "call"
CAPTURE_CONSTRUCTION = "construction"
CAPTURE_RENDER = "render"
DEFINITION_PREFIX = "definition."


def load_query_fragments(*names: str) -> str:
    """Read and concatenate query fragments, which are plain data files."""
    return "\n".join((QUERIES_DIR / name).read_text(encoding="utf-8") for name in names)


def node_text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LanguageSpec(Protocol):
    """What the extractor needs to know about one language."""

    #: tree-sitter grammar name, e.g. "python" or "tsx".
    grammar: str

    def query_source(self) -> str:
        """The composed query text for this language."""

    def definition_kind(self, capture: str, node: Node, inside_class: bool) -> str | None:
        """Final kind for a captured definition, or None to discard it."""

    def signature(self, node: Node, name: str, source: bytes, limits: Limits) -> str:
        """One-line signature, e.g. `get(id: string): Promise<User>`."""

    def docstring(self, node: Node, source: bytes, limits: Limits) -> str | None:
        """Documentation attached to a definition, if the language has any."""

    def is_exported(self, node: Node, name: str) -> bool:
        """Whether the definition is visible outside its file."""

    def imports_from(self, node: Node, source: bytes) -> list[ImportRef]:
        """Imports declared by an `@import` node (one statement may declare several)."""

    def call_target(self, callee: Node, source: bytes) -> tuple[str, str | None]:
        """(callee text, receiver) for a call; receiver is `db` in `db.query()`."""

    def base_types(self, class_node: Node, source: bytes) -> list[tuple[str, str]]:
        """(base name, "extends" | "implements") pairs for a class definition."""

    def import_from_call(self, callee: str, call_node: Node, source: bytes) -> ImportRef | None:
        """`require("x")` and `import("x")` are calls that are really imports."""

    def route_from_call(
        self, callee: str, receiver: str | None, call_node: Node, source: bytes,
    ) -> RouteDef | None:
        """An HTTP route declared by this call, if it declares one."""

    def http_call_from_call(
        self, callee: str, receiver: str | None, call_node: Node, source: bytes,
    ) -> HttpCall | None:
        """An outbound HTTP request made by this call, if it makes one."""

    def file_routes(self, relative_path: str, definitions: list) -> list[RouteDef]:
        """Routes implied by where a file lives, e.g. Next.js route handlers."""
