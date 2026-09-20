"""Building the context an LLM is allowed to see.

Three rules shape this module:

1. **Never the whole graph.** A mid-sized repository has tens of thousands of
   symbols; a large one has hundreds of thousands. Context is *retrieved* —
   the modules, plus the symbols the question actually mentions, plus the
   neighbourhood of whatever the user has selected.
2. **Bounded.** Line and character budgets are applied before the call, so a
   large repository cannot produce an unbounded prompt.
3. **Everything here is untrusted data.** Names, signatures and docstrings
   come from someone else's repository and may contain text shaped like
   instructions. It is fenced and labelled as data, and — the part that
   actually protects the user — every id the model returns is checked against
   this context before it reaches the client.
"""

import re
from dataclasses import dataclass, field

from app.knowledge_graph.build.ids import module_id, module_of
from app.knowledge_graph.build.model import EdgeType
from app.knowledge_graph.ports import KnowledgeGraphStore

#: Hard ceilings on what one prompt may carry.
MAX_CONTEXT_LINES = 160
MAX_LINE_CHARS = 200
MAX_RETRIEVED_SYMBOLS = 24
MAX_NEIGHBOURS = 40
#: How many retrieved symbols get their relations expanded, and how many
#: relations each may contribute. Bounded so a hub symbol cannot flood the
#: prompt.
MAX_EXPANDED_SYMBOLS = 5
MAX_RELATIONS = 12
#: Minimum length for a word from the question to be worth searching for.
MIN_KEYWORD_CHARS = 3

_KEYWORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "what", "which", "where", "how", "does", "did", "are",
    "was", "were", "this", "that", "with", "from", "into", "when", "why", "who",
    "can", "you", "show", "tell", "give", "list", "find", "all", "any", "use",
    "used", "uses", "using", "call", "calls", "called", "depend", "depends",
    "file", "files", "code", "function", "functions", "module", "modules",
    "service", "services", "flow", "request", "there", "about", "between",
})


@dataclass(frozen=True, slots=True)
class GroundingContext:
    """What the model may see, and how to map its answer back to real nodes."""

    text: str
    #: Label shown to the model -> node id. Nothing outside this map is ever
    #: accepted back from the model.
    labels: dict[str, str] = field(default_factory=dict)
    #: Label -> the module node that owns it. Recorded while the context is
    #: built, where the kind of each node is known; the dashboard renders
    #: modules, so this is what highlights and flow steps report.
    modules: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.labels

    def resolve(self, names: list[str]) -> tuple[list[str], list[str], list[str]]:
        """Map the model's answer back to real nodes.

        Returns (node ids, owning module ids, dropped names). A name this
        context never contained cannot become an id — that check, not the
        prompt, is what keeps invented nodes out of the response.
        """
        node_ids: list[str] = []
        module_ids: list[str] = []
        dropped: list[str] = []

        for name in names:
            node_id = self.labels.get(name)
            if node_id is None:
                dropped.append(name)
                continue
            if node_id not in node_ids:
                node_ids.append(node_id)
            owner = self.modules.get(name, node_id)
            if owner not in module_ids:
                module_ids.append(owner)

        return node_ids, module_ids, dropped


def owning_module_id(node_id: str, repo_full_name: str) -> str:
    """The module that owns a *file or symbol* id, derived without a query.

    Only meaningful for ids that carry a path (`repo:dir/file.py#symbol`);
    module ids are recorded directly when the context is built.
    """
    _, _, remainder = node_id.partition(":")
    path = remainder.partition("#")[0]
    if not path or path.startswith("pkg:"):
        return node_id
    return module_id(repo_full_name, module_of(path))


def keywords(question: str) -> list[str]:
    """Identifier-shaped words worth searching the graph for."""
    seen: list[str] = []
    for match in _KEYWORD_RE.findall(question):
        word = match.strip("_")
        if len(word) < MIN_KEYWORD_CHARS or word.lower() in _STOPWORDS:
            continue
        if word not in seen:
            seen.append(word)
    return seen[:8]


async def build_ask_context(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    question: str,
    focused_node_id: str | None = None,
) -> GroundingContext:
    """Modules, the symbols the question names, and anything in focus."""
    lines: list[str] = []
    labels: dict[str, str] = {}
    modules: dict[str, str] = {}

    await _add_module_overview(store, repo_full_name, lines, labels, modules)

    if focused_node_id:
        await _add_focus(store, repo_full_name, focused_node_id, lines, labels, modules)

    await _add_retrieved_symbols(store, repo_full_name, question, lines, labels, modules)

    return GroundingContext(text=_render(lines), labels=labels, modules=modules)


async def build_flow_context(
    store: KnowledgeGraphStore, repo_full_name: str, question: str = "",
) -> GroundingContext:
    """Modules and their dependencies, plus the functions the question names.

    A walkthrough is animated module by module, but people describe flows in
    terms of functions ("show me the login flow"), so those are retrieved too
    and mapped back to the module that owns them.
    """
    lines: list[str] = []
    labels: dict[str, str] = {}
    modules: dict[str, str] = {}
    await _add_module_overview(store, repo_full_name, lines, labels, modules)
    if question:
        await _add_retrieved_symbols(store, repo_full_name, question, lines, labels, modules)
    return GroundingContext(text=_render(lines), labels=labels, modules=modules)


# --------------------------------------------------------------------------


async def _add_module_overview(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    lines: list[str],
    labels: dict[str, str],
    modules: dict[str, str],
) -> None:
    view = await store.module_view(repo_full_name)
    if not view["nodes"]:
        return

    lines.append("Modules:")
    for node in view["nodes"]:
        labels[node["name"]] = node["id"]
        modules[node["name"]] = node["id"]  # a module owns itself
        lines.append(
            f"- {node['name']} ({node['kind']}, {node.get('file_count', 0)} files)"
        )

    by_id = {node["id"]: node["name"] for node in view["nodes"]}
    if view["edges"]:
        lines.append("Dependencies:")
        lines.extend(
            f"- {by_id.get(edge['source'], edge['source'])} -> "
            f"{by_id.get(edge['target'], edge['target'])}"
            f" ({edge['type'].lower()}, weight {edge.get('weight', 1)})"
            for edge in view["edges"]
        )


async def _add_focus(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    node_id: str,
    lines: list[str],
    labels: dict[str, str],
    modules: dict[str, str],
) -> None:
    detail = await store.node_detail(repo_full_name, node_id)
    if detail is None:
        return

    properties = detail["properties"]
    name = properties.get("name") or properties.get("path") or detail["id"]
    labels[name] = detail["id"]
    modules[name] = owning_module_id(detail["id"], repo_full_name)
    lines.append(f"Selected: {name} ({', '.join(detail['labels'])})")

    neighbours = await store.neighbours(
        repo_full_name, node_id, depth=2, limit=MAX_NEIGHBOURS,
    )
    for neighbour in neighbours["nodes"][:MAX_NEIGHBOURS]:
        label = neighbour.get("name") or neighbour["id"]
        labels.setdefault(label, neighbour["id"])
        modules.setdefault(label, owning_module_id(neighbour["id"], repo_full_name))
    if neighbours["edges"]:
        lines.append("Around the selection:")
        by_id = {node["id"]: node.get("name") or node["id"] for node in neighbours["nodes"]}
        by_id[detail["id"]] = name
        lines.extend(
            f"- {by_id.get(edge['source'], edge['source'])} "
            f"--{edge['type'].lower()}--> {by_id.get(edge['target'], edge['target'])}"
            for edge in neighbours["edges"][:MAX_NEIGHBOURS]
        )


async def _add_retrieved_symbols(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    question: str,
    lines: list[str],
    labels: dict[str, str],
    modules: dict[str, str],
) -> None:
    """Symbols whose names appear in the question — the retrieval step."""
    found: dict[str, dict] = {}
    for word in keywords(question):
        for row in await store.search(repo_full_name, word, limit=6):
            found.setdefault(row["id"], row)
        if len(found) >= MAX_RETRIEVED_SYMBOLS:
            break

    if not found:
        return

    lines.append("Matching definitions:")
    matched = list(found.values())[:MAX_RETRIEVED_SYMBOLS]
    for row in matched:
        label = row.get("name") or row["id"]
        labels.setdefault(label, row["id"])
        modules.setdefault(label, owning_module_id(row["id"], repo_full_name))
        location = row.get("file_path") or row.get("path") or ""
        signature = row.get("signature") or ""
        detail = f" — {signature}" if signature else ""
        lines.append(f"- {label} ({row.get('kind') or 'file'}) in {location}{detail}")

    # "What does X call?" needs X's neighbours, not just X: a match is only
    # useful if what it connects to is in context too.
    for row in matched[:MAX_EXPANDED_SYMBOLS]:
        await _add_relations_of(store, repo_full_name, row, lines, labels, modules)


async def _add_relations_of(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    row: dict,
    lines: list[str],
    labels: dict[str, str],
    modules: dict[str, str],
) -> None:
    """One hop around a retrieved symbol: what it uses and what uses it."""
    around = await store.neighbours(repo_full_name, row["id"], depth=1, limit=MAX_RELATIONS)
    if not around["edges"]:
        return

    names = {node["id"]: node.get("name") or node["id"] for node in around["nodes"]}
    names[row["id"]] = row.get("name") or row["id"]
    for node in around["nodes"]:
        label = node.get("name") or node["id"]
        labels.setdefault(label, node["id"])
        modules.setdefault(label, owning_module_id(node["id"], repo_full_name))

    lines.append(f"Relations of {names[row['id']]}:")
    lines.extend(
        f"- {names.get(edge['source'], edge['source'])} "
        f"--{edge['type'].lower()}--> {names.get(edge['target'], edge['target'])}"
        for edge in around["edges"][:MAX_RELATIONS]
    )


def _render(lines: list[str]) -> str:
    """Apply the budget, then fence the whole thing as data."""
    trimmed = [line[:MAX_LINE_CHARS] for line in lines[:MAX_CONTEXT_LINES]]
    if len(lines) > MAX_CONTEXT_LINES:
        trimmed.append(f"… {len(lines) - MAX_CONTEXT_LINES} more lines omitted")
    body = "\n".join(trimmed) or "(no graph available)"
    return f"<graph_context>\n{body}\n</graph_context>"


__all__ = [
    "GroundingContext",
    "build_ask_context",
    "build_flow_context",
    "keywords",
    "owning_module_id",
]
