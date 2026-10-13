"""Building the context an LLM is allowed to see.

Three rules shape this module:

1. **Never the whole graph.** A mid-sized repository has tens of thousands of
   symbols; a large one has hundreds of thousands. Context is *retrieved* —
   whatever is in focus, the symbols the question names and their relations,
   then the module overview.
2. **Bounded.** Line and character budgets are applied before the call, so a
   large repository cannot produce an unbounded prompt.
3. **Everything here is untrusted data.** Names, signatures and docstrings
   come from someone else's repository and may contain text shaped like
   instructions. It is escaped, fenced and labelled as data, and — the part
   that actually protects the user — every reference and relationship the
   model returns is checked against what this context showed it.

Each node gets a short reference (`[n4]`) unique within one context, so two
files that share a name stay distinguishable and the model cites nodes by
reference rather than by a name we would have to guess at.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.knowledge_graph.build.ids import module_id, module_of, repository_id
from app.knowledge_graph.build.model import NodeLabel
from app.knowledge_graph.ports import KnowledgeGraphStore
from app.query.source import Snippet, read_snippet

#: Hard ceilings on what one prompt may carry.
MAX_CONTEXT_LINES = 700
MAX_LINE_CHARS = 240
MAX_NAME_CHARS = 80
MAX_LABEL_CHARS = 200
MAX_DOCSTRING_CHARS = 120
MAX_RETRIEVED_SYMBOLS = 24
MAX_NEIGHBOURS = 40
#: How many retrieved symbols get their relations expanded, and how many
#: relations each may contribute. Bounded so a hub symbol cannot flood the
#: prompt.
MAX_EXPANDED_SYMBOLS = 5
MAX_RELATIONS = 12
#: Nodes an earlier answer cited, carried into a follow-up question.
MAX_EARLIER_NODES = 4
#: Every @-mentioned node is shown; the first few also get their relations.
MAX_MENTION_RELATIONS = 5
#: Folders (and files) whose contents are listed: open on the canvas, in focus or named.
MAX_FOLDERS = 12
MAX_OPEN_FOLDERS = 10
#: Nodes listed under those folders, shallowest first.
MAX_CONTENTS = 80
SEARCH_PER_WORD = 6
#: Source shown: for the nodes in focus first, then the best matches.
MAX_SNIPPETS = 8
MAX_SNIPPET_MATCHES = 4
MAX_SOURCE_LINES = 400
MAX_SOURCE_CHARS = 40_000
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
# Repo text can't close the fence or forge a reference: brackets become look-alikes.
_ESCAPES = str.maketrans({"<": "‹", ">": "›", "[": "(", "]": ")"})


@dataclass(frozen=True, slots=True)
class Relation:
    """An edge as the model sees it: `[source] --type--> [target]`, by reference."""

    source: str
    type: str
    target: str


@dataclass(frozen=True, slots=True)
class GroundingContext:
    """What the model was shown, and how to map its answer back to real nodes."""

    text: str
    #: Reference -> node id. Nothing outside this map is ever accepted back.
    nodes: dict[str, str] = field(default_factory=dict)
    #: Reference -> name as it appears in the prompt (escaped).
    names: dict[str, str] = field(default_factory=dict)
    #: Reference -> name as the repository has it, for showing a citation or mention.
    labels: dict[str, str] = field(default_factory=dict)
    #: Reference -> the module node that owns it; the dashboard renders modules.
    modules: dict[str, str] = field(default_factory=dict)
    #: Every relationship that made it into the text.
    relations: frozenset[Relation] = frozenset()
    #: References of the @-mentioned nodes that exist, in the order mentioned.
    mentioned: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.nodes

    def supports(self, relation: Relation) -> bool:
        return relation in self.relations

    def resolve(self, refs: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
        """(node ids, owning module ids, dropped refs) for the model's references."""
        node_ids: list[str] = []
        module_ids: list[str] = []
        dropped: list[str] = []
        for ref in refs:
            node_id = self.nodes.get(ref)
            if node_id is None:
                dropped.append(ref)
                continue
            if node_id not in node_ids:
                node_ids.append(node_id)
            if self.modules[ref] not in module_ids:
                module_ids.append(self.modules[ref])
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
    module = module_of(path)
    # A top-level file belongs to no module; the repository owns it.
    if module is None:
        return repository_id(repo_full_name)
    return module_id(repo_full_name, module)


def module_for(node_id: str, labels: Sequence[str], repo_full_name: str) -> str:
    """What the diagram can draw for a node: modules (and the repository) are themselves."""
    if NodeLabel.MODULE in labels or NodeLabel.REPOSITORY in labels:
        return node_id
    return owning_module_id(node_id, repo_full_name)


def keywords(question: str) -> list[str]:
    """Identifier-shaped words worth searching the graph for, plurals trimmed ("dates" -> "date")."""
    seen: list[str] = []
    for match in _KEYWORD_RE.findall(question):
        word = match.strip("_")
        if len(word) < MIN_KEYWORD_CHARS or word.lower() in _STOPWORDS:
            continue
        # search is a substring match, so the singular still finds the plural
        if len(word) > 4 and word.lower().endswith("s") and not word.lower().endswith("ss"):
            word = word[:-1]
        if word not in seen:
            seen.append(word)
    return seen[:8]


def clean(text: object, limit: int = MAX_NAME_CHARS) -> str:
    """Repo text made safe to place inside the fence: one line, escaped, bounded."""
    return " ".join(str(text).split()).translate(_ESCAPES)[:limit]


async def build_ask_context(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    question: str,
    *,
    selected_node_id: str | None = None,
    mentioned_node_ids: Sequence[str] = (),
    earlier_node_ids: Sequence[str] = (),
    open_folder_ids: Sequence[str] = (),
) -> GroundingContext:
    """What is in focus, the symbols the question names, what folders in focus hold, then the modules.

    `mentioned_node_ids` are what the question @-mentions, `earlier_node_ids` what the
    previous answer cited (so "what calls it?" has something to go on), and
    `open_folder_ids` what's open on the canvas. Independent lookups run together, so
    a question costs two rounds of graph queries, not one per lookup.
    """
    mentioned = list(dict.fromkeys(mentioned_node_ids))
    earlier = [node_id for node_id in dict.fromkeys(earlier_node_ids) if node_id not in mentioned][:MAX_EARLIER_NODES]

    selected, focused, matched, view = await asyncio.gather(
        _focus(store, repo_full_name, selected_node_id, depth=2, limit=MAX_NEIGHBOURS),
        asyncio.gather(*(
            _focus(store, repo_full_name, node_id, depth=1, limit=MAX_RELATIONS, relations=index < MAX_MENTION_RELATIONS)
            for index, node_id in enumerate(mentioned)
        ), *(_focus(store, repo_full_name, node_id, depth=1, limit=MAX_RELATIONS) for node_id in earlier)),
        _search(store, repo_full_name, question),
        store.module_view(repo_full_name),
    )
    holders = [
        *open_folder_ids[:MAX_OPEN_FOLDERS],
        *(focus.detail["id"] for focus in (selected, *focused) if focus and _holds(focus.detail["labels"])),
        *(row["id"] for row in matched if _holds(row.get("labels", []))),
    ]
    relations, contents = await asyncio.gather(
        asyncio.gather(*(
            store.neighbours(repo_full_name, row["id"], depth=1, limit=MAX_RELATIONS)
            for row in matched[:MAX_EXPANDED_SYMBOLS]
        )),
        _subtree(store, repo_full_name, list(dict.fromkeys(holders))[:MAX_FOLDERS]),
    )
    # local file reads, all at once: what's in focus, the best matches, then functions in open folders
    sources = _source_candidates([selected, *focused], matched, contents)
    snippets = await asyncio.gather(*(
        read_snippet(repo_full_name, path, start, end) for _, _, _, path, start, end in sources
    ))

    builder = _Builder(repo_full_name)
    if selected:
        builder.add_focus("Selected on the canvas", selected, "Around the selection")
    for node_id, focus in zip([*mentioned, *earlier], focused):
        if focus and node_id in mentioned:
            builder.add_focus("Mentioned in the question", focus)
            builder.mentioned.append(node_id)
        elif focus:
            builder.add_focus("Mentioned earlier", focus)
    builder.add_matches(matched, relations)
    builder.add_sources([(source[:3], snippet) for source, snippet in zip(sources, snippets) if snippet])
    builder.add_contents(contents)
    builder.add_module_overview(view)
    return builder.build()


async def build_flow_context(
    store: KnowledgeGraphStore, repo_full_name: str, question: str = "",
) -> GroundingContext:
    """Modules and their dependencies, plus the functions the question names.

    A walkthrough is animated module by module, but people describe flows in
    terms of functions ("show me the login flow"), so those are retrieved too
    and mapped back to the module that owns them.
    """
    view, matched = await asyncio.gather(store.module_view(repo_full_name), _search(store, repo_full_name, question))
    relations = await asyncio.gather(*(
        store.neighbours(repo_full_name, row["id"], depth=1, limit=MAX_RELATIONS)
        for row in matched[:MAX_EXPANDED_SYMBOLS]
    ))
    builder = _Builder(repo_full_name)
    builder.add_module_overview(view)
    builder.add_matches(matched, relations)
    return builder.build()


@dataclass(frozen=True, slots=True)
class _Focus:
    detail: dict
    around: dict


async def _focus(
    store: KnowledgeGraphStore, repo: str, node_id: str | None, *, depth: int, limit: int, relations: bool = True,
) -> _Focus | None:
    if not node_id:
        return None
    detail, around = await asyncio.gather(
        store.node_detail(repo, node_id),
        store.neighbours(repo, node_id, depth=depth, limit=limit) if relations else _no_neighbours(),
    )
    return _Focus(detail, around) if detail else None


async def _no_neighbours() -> dict:
    return {"nodes": [], "edges": []}


async def _search(store: KnowledgeGraphStore, repo: str, question: str) -> list[dict]:
    """Nodes whose names appear in the question — the retrieval step; every word at once."""
    results = await asyncio.gather(*(store.search(repo, word, limit=SEARCH_PER_WORD) for word in keywords(question)))
    found: dict[str, dict] = {}
    for rows in results:
        for row in rows:
            found.setdefault(row["id"], row)
    return list(found.values())[:MAX_RETRIEVED_SYMBOLS]


async def _subtree(store: KnowledgeGraphStore, repo: str, root_ids: list[str]) -> list[dict]:
    return await store.subtree(repo, root_ids, limit=MAX_CONTENTS) if root_ids else []


def _source_candidates(
    focused: Sequence[_Focus | None], matched: list[dict], contents: list[dict],
) -> list[tuple]:
    """(id, name, labels, path, start, end) of the files and symbols worth showing code for."""
    candidates: dict[str, tuple] = {}
    rows = [
        *({"id": f.detail["id"], "labels": f.detail["labels"], **f.detail["properties"]} for f in focused if f),
        *matched[:MAX_SNIPPET_MATCHES],
        # only functions here: a whole file from an open folder is mostly noise
        *(row for row in contents if NodeLabel.SYMBOL in row["labels"]),
    ]
    for row in rows:
        labels = row.get("labels", [])
        if NodeLabel.SYMBOL in labels and row.get("file_path") and row.get("start_line"):
            source = (row["file_path"], row["start_line"], row.get("end_line"))
        elif NodeLabel.FILE in labels and row.get("path"):
            source = (row["path"], None, None)
        else:
            continue
        candidates.setdefault(row["id"], (row["id"], row.get("name") or row.get("path"), labels, *source))
    return list(candidates.values())[:MAX_SNIPPETS]


def _holds(labels: Sequence[str]) -> bool:
    """A folder or file: something whose contents answer "what's in it?"."""
    return NodeLabel.MODULE in labels or NodeLabel.FILE in labels


#: The containment edge a parent of each kind has to its children.
_CONTAINMENT_BY_PARENT = {NodeLabel.MODULE: "contains", NodeLabel.FILE: "defines", NodeLabel.SYMBOL: "has_member"}


@dataclass(slots=True)
class _Line:
    text: str
    refs: tuple[str, ...] = ()
    relation: Relation | None = None


class _Builder:
    """Lays out what was fetched; only what survives the budget counts as shown."""

    def __init__(self, repo_full_name: str):
        self.repo = repo_full_name
        self.lines: list[_Line] = []
        self.refs: dict[str, str] = {}  # node id -> ref
        self.names: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.modules: dict[str, str] = {}
        self.mentioned: list[str] = []
        self._heading: str | None = None

    def ref(self, node_id: str, name: object, labels: Sequence[str] = ()) -> str:
        if node_id not in self.refs:
            ref = f"n{len(self.refs) + 1}"
            self.refs[node_id] = ref
            self.names[ref] = clean(name or node_id)
            # `[slug]` stays `[slug]` here: only the prompt needs escaping
            self.labels[ref] = " ".join(str(name or node_id).split())[:MAX_LABEL_CHARS]
            self.modules[ref] = module_for(node_id, labels, self.repo)
        return self.refs[node_id]

    def heading(self, text: str) -> None:
        # consecutive nodes under one heading (several mentions, say) share it
        if self._heading != text:
            self.lines.append(_Line(f"{text}:"))
        self._heading = text

    def node(self, node_id: str, name: object, details: str, labels: Sequence[str] = ()) -> None:
        ref = self.ref(node_id, name, labels)
        self.lines.append(_Line(f"- [{ref}] {self.names[ref]} {details}", (ref,)))

    def edge(self, edge: dict, suffix: str = "") -> None:
        source, target = self.refs.get(edge["source"]), self.refs.get(edge["target"])
        if source is None or target is None:
            return
        relation = Relation(source, edge["type"].lower(), target)
        self.lines.append(_Line(
            f"- [{source}] {self.names[source]} --{relation.type}--> [{target}] {self.names[target]}{suffix}",
            (source, target),
            relation,
        ))

    def add_focus(self, heading: str, focus: _Focus, around_heading: str | None = None) -> None:
        """A node and one or two hops around it: what it uses and what uses it."""
        detail = focus.detail
        properties = detail["properties"]
        if detail["id"] not in self.refs:
            self.heading(heading)
            self.node(detail["id"], properties.get("name") or properties.get("path"), _describe(properties, detail["labels"]), detail["labels"])
        ref = self.refs[detail["id"]]
        self._add_around(focus.around, MAX_NEIGHBOURS, around_heading or f"Relations of [{ref}] {self.names[ref]}")

    def _add_around(self, around: dict, limit: int, heading: str) -> None:
        for neighbour in around["nodes"][:limit]:
            self.ref(neighbour["id"], neighbour.get("name"), neighbour.get("labels", ()))
        if around["edges"]:
            self.heading(heading)
            for edge in around["edges"][:limit]:
                self.edge(edge)

    def add_matches(self, matched: list[dict], relations: Sequence[dict]) -> None:
        if not matched:
            return
        self.heading("Matching definitions")
        for row in matched:
            labels = row.get("labels", [])
            self.node(row["id"], row.get("name") or row.get("path"), _describe(row, labels), labels)
        # "What does X call?" needs X's neighbours, not just X.
        for row, around in zip(matched, relations):
            if around["edges"]:
                ref = self.refs[row["id"]]
                self._add_around(around, MAX_RELATIONS, f"Relations of [{ref}] {self.names[ref]}")

    def add_sources(self, snippets: list[tuple[tuple, Snippet]]) -> None:
        """Code for the nodes in focus, numbered as in the file, within its own budget."""
        lines_left, chars_left = MAX_SOURCE_LINES, MAX_SOURCE_CHARS
        for (node_id, name, labels), snippet in snippets:
            if lines_left <= 0 or chars_left <= 0:
                break
            ref = self.ref(node_id, name, labels)
            end = snippet.start_line + len(snippet.lines) + snippet.omitted - 1
            self.lines.append(_Line(f"Source of [{ref}] {self.names[ref]} ({clean(snippet.path, MAX_LINE_CHARS)}:{snippet.start_line}-{end}):", (ref,)))
            shown = 0
            for offset, code in enumerate(snippet.lines):
                text = f"  {snippet.start_line + offset:>5} | {code}"
                if shown >= lines_left or len(text) > chars_left:
                    break
                self.lines.append(_Line(text))
                shown += 1
                chars_left -= len(text)
            lines_left -= shown
            hidden = snippet.omitted + len(snippet.lines) - shown
            if hidden:
                self.lines.append(_Line(f"  … {hidden} more lines"))

    def add_contents(self, rows: list[dict]) -> None:
        """What folders and files in focus hold, as containment edges, shallowest first."""
        if not rows:
            return
        self.heading("Contents")
        labels_of = {row["id"]: row["labels"] for row in rows}
        for row in rows:
            name, labels = row.get("name") or row.get("path"), row["labels"]
            kind = clean(row.get("kind") or ", ".join(labels).lower())
            if row["depth"] == 0:
                if row["id"] not in self.refs:
                    self.node(row["id"], name, f"({kind})", labels)
                continue
            parent_labels = labels_of.get(row["parent"], ())
            edge_type = next((t for label, t in _CONTAINMENT_BY_PARENT.items() if label in parent_labels), "contains")
            self.ref(row["id"], name, labels)
            self.edge({"source": row["parent"], "target": row["id"], "type": edge_type}, f" ({kind})")

    def add_module_overview(self, view: dict) -> None:
        if not view["nodes"]:
            return
        self.heading("Modules")
        for node in view["nodes"]:
            self.node(node["id"], node["name"], f"({node['kind']}, {node.get('file_count', 0)} files)", [NodeLabel.MODULE])
        if view["edges"]:
            self.heading("Dependencies between modules")
            for edge in view["edges"]:
                self.edge(edge, f" (weight {edge.get('weight', 1)})")

    def build(self) -> GroundingContext:
        """Apply the budget, then fence the whole thing as data."""
        kept = self.lines[:MAX_CONTEXT_LINES]
        rendered = [line.text[:MAX_LINE_CHARS] for line in kept]
        if len(self.lines) > MAX_CONTEXT_LINES:
            rendered.append(f"… {len(self.lines) - MAX_CONTEXT_LINES} more lines omitted")
        body = "\n".join(rendered) or "(no graph available)"

        shown = {ref for line in kept for ref in line.refs}
        nodes = {ref: node_id for node_id, ref in self.refs.items() if ref in shown}
        return GroundingContext(
            text=f"<graph_context>\n{body}\n</graph_context>",
            nodes=nodes,
            names={ref: self.names[ref] for ref in nodes},
            labels={ref: self.labels[ref] for ref in nodes},
            modules={ref: self.modules[ref] for ref in nodes},
            relations=frozenset(line.relation for line in kept if line.relation),
            mentioned=tuple(self.refs[node_id] for node_id in self.mentioned if self.refs[node_id] in nodes),
        )


def _describe(properties: dict, labels: Sequence[str]) -> str:
    """`(function) in api/x.py — sig(a) — "first docstring line"` for a file or symbol."""
    kind = properties.get("kind") or ", ".join(labels).lower() or "file"
    parts = [f"({clean(kind)})"]
    location = properties.get("file_path") or properties.get("path")
    if location:
        parts.append(f"in {clean(location, MAX_LINE_CHARS)}")
    if properties.get("signature"):
        parts.append(f"— {clean(properties['signature'], MAX_LINE_CHARS)}")
    docstring = (properties.get("docstring") or "").strip().splitlines()
    if docstring:
        parts.append(f'— "{clean(docstring[0], MAX_DOCSTRING_CHARS)}"')
    return " ".join(parts)


__all__ = [
    "GroundingContext",
    "Relation",
    "build_ask_context",
    "build_flow_context",
    "clean",
    "keywords",
    "module_for",
    "owning_module_id",
]
