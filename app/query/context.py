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
import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.knowledge_graph.build.ids import module_id, module_of, repository_id
from app.knowledge_graph.build.model import EdgeType, NodeLabel
from app.knowledge_graph.ports import KnowledgeGraphStore
from app.query.source import Snippet, find_in_code, language_of, prompt_line, read_snippet

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
SEARCH_PER_WORD = 10
#: Words from one question searched for, joined phrases ("sign up" -> `signup`) first.
MAX_KEYWORDS = 8
#: Source shown: for the nodes in focus first, then the best matches.
MAX_SNIPPETS = 10
MAX_SNIPPET_MATCHES = 4
MAX_SOURCE_LINES = 400
MAX_SOURCE_CHARS = 40_000
#: Following what the best matches lead to, so code one or more hops away is shown too:
#: out along calls, renders and requests, and back from a route to the code serving it.
#: Logic and rendering are followed separately, so a component rendering many widgets still
#: has its calls followed.
FLOW_EDGES_OUT = (EdgeType.CALLS, EdgeType.REQUESTS, EdgeType.EXTENDS, EdgeType.IMPLEMENTS)
FLOW_EDGES_IN = (EdgeType.HANDLES,)
FLOW_RENDER_EDGES = (EdgeType.RENDERS,)
FLOW_DEPTH = 3
#: Matches followed from; a matching file is followed from the functions it defines.
MAX_FLOW_SEEDS = 4
MAX_LIFTED_SYMBOLS = 6
#: Per hop: nodes followed from, and edges taken from each; nodes reached in all.
MAX_FLOW_FRONTIER = 12
MAX_FLOW_FANOUT = 6
MAX_FLOW_NODES = 16
#: Code shown for what the flow reaches, nearest first, each cut to a readable length; at
#: the same distance logic (calls, requests, handlers) before what's rendered.
MAX_FLOW_SOURCES = 4
FLOW_SOURCE_PRIORITY = {EdgeType.CALLS: 0, EdgeType.REQUESTS: 0, EdgeType.HANDLES: 0, EdgeType.RENDERS: 1}
MAX_FLOW_SNIPPET_LINES = 60
#: Files listed so the reader knows what exists beyond what's shown: near the matches first, then the most used.
MAX_MAP_FILES = 40
MAX_MAP_SYMBOLS = 5
#: Lines the model's own lookups may add to a question's context, beyond its budget: room for
#: about ten full reads, most of it cached from round to round.
MAX_LOOKUP_LINES = 1_600
#: Minimum length for a word from the question to be worth searching for.
MIN_KEYWORD_CHARS = 3
#: Code found by searching file contents: how many files, and the lines either side of where the words cluster.
MAX_FOUND_FILES = 3
FOUND_CONTEXT_LINES = 12

_KEYWORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Verbs that code names as one word with the particle after them: "sign up" is `signup`.
_PHRASAL_VERBS = frozenset({
    "sign", "log", "check", "set", "look", "roll", "back", "opt", "clean", "start",
    "shut", "tear", "pick", "drop", "break", "follow", "pop", "sync", "spin", "wake",
})
_PARTICLES = frozenset({"up", "in", "out", "on", "off", "down", "back", "over"})
_WORD_BREAKS = frozenset("_-./ ")
_STOPWORDS = frozenset({
    "the", "and", "for", "what", "which", "where", "how", "does", "did", "are",
    "was", "were", "this", "that", "with", "from", "into", "when", "why", "who",
    "can", "you", "show", "tell", "give", "list", "find", "all", "any", "use",
    "used", "uses", "using", "call", "calls", "called", "depend", "depends",
    "file", "files", "code", "function", "functions", "module", "modules",
    "service", "services", "flow", "request", "there", "about", "between",
    "explain", "detail", "details", "point", "exact", "exactly", "line", "lines",
    "handle", "handles", "handled", "work", "works", "happen", "happens", "please",
    "walk", "through", "step", "steps", "goes", "going", "get", "gets", "does", "doing",
})
_FILE_NAME_RE = re.compile(r"[\w.-]*\w\.[A-Za-z]\w*")
# Repo text can't close the fence or forge a reference: brackets become look-alikes.
_ESCAPES = str.maketrans({"<": "‹", ">": "›", "[": "(", "]": ")"})


@dataclass(frozen=True, slots=True)
class Relation:
    """An edge as the model sees it: `[source] --type--> [target]`, by reference."""

    source: str
    type: str
    target: str


@dataclass(frozen=True, slots=True)
class ShownSource:
    """A block of code the model was shown (`Source s3 …`), and exactly which lines survived the budget."""

    path: str
    start_line: int
    end_line: int
    #: as written in the file, one per line from `start_line`
    lines: tuple[str, ...]
    #: the node it's the code of; None for a range an earlier answer cited
    node_id: str | None
    language: str | None


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
    #: Source id (`s3`) -> the code shown under it. A code citation must fall inside one.
    sources: dict[str, ShownSource] = field(default_factory=dict)
    #: The commit every source was read at.
    commit: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.nodes

    def supports(self, relation: Relation) -> bool:
        return relation in self.relations


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


def search_terms(question: str) -> tuple[list[str], frozenset[str]]:
    """Identifier-shaped words worth searching the graph for, trimmed to their stem ("dates" -> "date"),
    and which of them were joined from a phrase.

    A verb with its particle is also searched as the one word code names it with
    ("signs up" -> `signup`), ahead of the single words.
    """
    words = _WORD_RE.findall(question)
    phrases = [
        f"{_stem(first.lower())}{second.lower()}"
        for first, second in zip(words, words[1:], strict=False)
        if second.lower() in _PARTICLES and _stem(first.lower()) in _PHRASAL_VERBS
    ]
    seen: list[str] = list(dict.fromkeys(phrases))
    for match in _KEYWORD_RE.findall(question):
        word = match.strip("_")
        if len(word) < MIN_KEYWORD_CHARS or word.lower() in _STOPWORDS:
            continue
        word = _stem(word)
        if word not in seen:
            seen.append(word)
    return seen[:MAX_KEYWORDS], frozenset(phrases)


def _stem(word: str) -> str:
    """Drop a plural or verb ending; search matches substrings, so the stem still finds every form."""
    lower = word.lower()
    for ending in ("ing", "ed"):
        if lower.endswith(ending) and len(word) - len(ending) >= MIN_KEYWORD_CHARS + 1:
            stem = word[: -len(ending)]
            # "running" -> "run", "embedded" -> "embed"
            return stem[:-1] if len(stem) > MIN_KEYWORD_CHARS and stem[-1] == stem[-2] else stem
    if len(word) > 4 and lower.endswith("s") and not lower.endswith("ss"):
        return word[:-1]
    return word


def clean(text: object, limit: int = MAX_NAME_CHARS) -> str:
    """Repo text made safe to place inside the fence: one line, escaped, bounded."""
    return " ".join(str(text).split()).translate(_ESCAPES)[:limit]


async def open_ask_context(
    store: KnowledgeGraphStore,
    repo_full_name: str,
    question: str,
    *,
    selected_node_id: str | None = None,
    mentioned_node_ids: Sequence[str] = (),
    earlier_node_ids: Sequence[str] = (),
    open_folder_ids: Sequence[str] = (),
    earlier_ranges: Sequence[tuple[str, int, int]] = (),
    commit: str | None = None,
) -> "ContextBuilder":
    """What is in focus, the symbols the question names, what folders in focus hold, then the modules.

    Returned unbuilt, so what the model looks up later is numbered and checked with it.

    `mentioned_node_ids` are what the question @-mentions, `earlier_node_ids` what the
    previous answer cited (so "what calls it?" has something to go on), `earlier_ranges`
    the code it cited as `(path, start, end)`, and `open_folder_ids` what's open on the
    canvas. Code is read at `commit`, the one the graph was built from; without it no
    code is shown. Independent lookups run together, so a question costs two rounds of
    graph queries, not one per lookup.
    """
    mentioned = list(dict.fromkeys(mentioned_node_ids))
    earlier = [node_id for node_id in dict.fromkeys(earlier_node_ids) if node_id not in mentioned][:MAX_EARLIER_NODES]

    terms, phrases = search_terms(question)
    selected, focused, matched, view, found = await asyncio.gather(
        _focus(store, repo_full_name, selected_node_id, depth=2, limit=MAX_NEIGHBOURS),
        asyncio.gather(*(
            _focus(store, repo_full_name, node_id, depth=1, limit=MAX_RELATIONS, relations=index < MAX_MENTION_RELATIONS)
            for index, node_id in enumerate(mentioned)
        ), *(_focus(store, repo_full_name, node_id, depth=1, limit=MAX_RELATIONS) for node_id in earlier)),
        search_nodes(store, repo_full_name, question),
        store.module_view(repo_full_name),
        find_in_code(repo_full_name, commit, terms) if commit else _nothing(),
    )
    # folders match well by name but hold no code to follow or show
    with_code = [row for row in matched if not _holds_only(row.get("labels", []))]
    seeds = [
        *({"id": focus.detail["id"], "labels": focus.detail["labels"]} for focus in (selected, *focused) if focus),
        *with_code[:MAX_FLOW_SEEDS],
    ]
    holders = [
        *open_folder_ids[:MAX_OPEN_FOLDERS],
        *(focus.detail["id"] for focus in (selected, *focused) if focus and _holds(focus.detail["labels"])),
        *(row["id"] for row in matched if _holds(row.get("labels", []))),
    ]
    flow = await _follow_flow(store, repo_full_name, seeds)
    near = {
        posixpath.dirname(path)
        for row in [*seeds, *matched, *flow]
        if (path := row.get("file_path") or row.get("path")) and "/" in path
    }
    relations, contents, files = await asyncio.gather(
        asyncio.gather(*(
            store.neighbours(repo_full_name, row["id"], depth=1, limit=MAX_RELATIONS)
            for row in matched[:MAX_EXPANDED_SYMBOLS]
        )),
        _subtree(store, repo_full_name, list(dict.fromkeys(holders))[:MAX_FOLDERS]),
        store.repo_map(repo_full_name, near=sorted(near), limit=MAX_MAP_FILES),
    )
    # file reads, all at once: code cited just before, what's in focus, the best matches, then functions in open folders
    sources = _source_candidates(
        [selected, *focused], matched, contents, earlier_ranges, _found_ranges(found, len(terms), phrases), question,
        flow,
    ) if commit else []
    snippets = await asyncio.gather(*(
        read_snippet(repo_full_name, commit, path, start, end) for _, _, _, path, start, end, _ in sources
    ))

    builder = ContextBuilder(repo_full_name, commit)
    if selected:
        builder.add_focus("Selected on the canvas", selected, "Around the selection")
    for node_id, focus in zip([*mentioned, *earlier], focused):
        if focus and node_id in mentioned:
            builder.add_focus("Mentioned in the question", focus)
            builder.mentioned.append(node_id)
        elif focus:
            builder.add_focus("Mentioned earlier", focus)
    builder.add_matches(matched, relations)
    builder.add_flow(flow)
    builder.add_sources([((*source[:3], source[6]), snippet) for source, snippet in zip(sources, snippets) if snippet])
    builder.add_contents(contents)
    builder.add_repo_map(files)
    builder.add_module_overview(view)
    return builder


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


async def _nothing() -> list:
    return []


def _found_ranges(
    hits: list[tuple[str, int, str]], term_count: int, phrases: frozenset[str] = frozenset(),
) -> list[tuple[str, int, int]]:
    """The files that contain the most of the question's words, each as a window around where they cluster.

    A file needs two different words when the question has two, so one common word
    (`next`, `user`) can't pull in every file that happens to use it; a phrase
    (`signup`) is specific enough on its own.
    """
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, line, term in hits:
        by_file.setdefault(path, []).append((line, term))
    wanted = min(2, term_count)
    ranked = sorted(
        (
            path for path, found in by_file.items()
            if len({term for _, term in found}) >= wanted or any(term in phrases for _, term in found)
        ),
        key=lambda path: (
            not any(term in phrases for _, term in by_file[path]),
            -len({term for _, term in by_file[path]}), -len(by_file[path]), len(path),
        ),
    )
    ranges = []
    for path in ranked[:MAX_FOUND_FILES]:
        lines = [line for line, _ in by_file[path]]
        centre = max(lines, key=lambda at: sum(abs(other - at) <= FOUND_CONTEXT_LINES for other in lines))
        ranges.append((path, max(centre - FOUND_CONTEXT_LINES, 1), centre + FOUND_CONTEXT_LINES))
    return ranges


async def search_nodes(store: KnowledgeGraphStore, repo: str, question: str) -> list[dict]:
    """Nodes whose names appear in the question, best match first — the retrieval step; every word at once."""
    terms, phrases = search_terms(question)
    results = await asyncio.gather(*(store.search(repo, word, limit=SEARCH_PER_WORD) for word in terms))
    found: dict[str, dict] = {}
    for rows in results:
        for row in rows:
            found.setdefault(row["id"], row)
    return rank_matches(list(found.values()), terms, phrases)[:MAX_RETRIEVED_SYMBOLS]


def rank_matches(rows: list[dict], terms: Sequence[str], phrases: frozenset[str] = frozenset()) -> list[dict]:
    """Search hits ordered by how well their names fit the question.

    A word in the name counts more than one only in the path, a name that is or
    starts with the word more again, and a phrase ("signup") more than a single
    word, so `SignupForm` comes before `design` for "how does a user sign up".
    """
    def score(row: dict) -> float:
        written = str(row.get("name") or "")
        name = written.lower()
        path = str(row.get("path") or row.get("file_path") or "").lower()
        total = 0.0
        for term in (term.lower() for term in terms):
            weight = 2 if term in phrases else 1
            at = name.find(term)
            if at >= 0:
                # `SignupForm`, `sign_in`, `onSignUp` start a word with it; `design` only contains it
                starts_word = at == 0 or written[at].isupper() or written[at - 1] in _WORD_BREAKS
                total += weight * ((3 if starts_word else 1) + (2 if at == 0 else 0) + (2 if name == term else 0))
            elif term in path:
                total += weight
        return total

    return sorted(rows, key=lambda row: (-score(row), len(str(row.get("name") or row.get("path") or "")), row["id"]))


async def _follow_flow(store: KnowledgeGraphStore, repo: str, seeds: Sequence[dict]) -> list[dict]:
    """What the seeds lead to along the flow edges, up to `FLOW_DEPTH` hops: rows from `store.follow`.

    A matching file is opened up into the functions it defines first (hop 0), since
    calls and renders start at functions. Each hop follows only the most used edges
    of a bounded frontier, so a hub can't flood the context. Every row carries
    `hop` and `seed`, the position of the seed it was reached from, so what the
    best match leads to can come first.
    """
    seed_of = {row["id"]: index for index, row in reversed(list(enumerate(seeds)))}
    files = [row["id"] for row in seeds if NodeLabel.FILE in row.get("labels", [])]
    lifted = await store.follow(
        repo, files, out_types=(EdgeType.DEFINES,), per_node=MAX_LIFTED_SYMBOLS, limit=MAX_FLOW_FRONTIER,
    ) if files else []
    reached = []
    for row in lifted:
        seed_of.setdefault(row["id"], seed_of[row["origin"]])
        reached.append({**row, "hop": 0, "seed": seed_of[row["origin"]]})
    frontier = sorted(
        (node_id for node_id in seed_of if node_id not in files), key=lambda node_id: seed_of[node_id],
    )[:MAX_FLOW_FRONTIER]
    found = 0
    for hop in range(1, FLOW_DEPTH + 1):
        if not frontier:
            break
        logic, rendered = await asyncio.gather(*(
            store.follow(
                repo, frontier, out_types=out_types, in_types=in_types,
                per_node=MAX_FLOW_FANOUT, limit=MAX_FLOW_FRONTIER * MAX_FLOW_FANOUT,
            )
            for out_types, in_types in ((FLOW_EDGES_OUT, FLOW_EDGES_IN), (FLOW_RENDER_EDGES, ()))
        ))
        next_frontier = []
        for row in sorted([*logic, *rendered], key=lambda row: seed_of[row["origin"]]):
            new = row["id"] not in seed_of
            if new and found >= MAX_FLOW_NODES:
                continue
            if new:
                found += 1
                seed_of[row["id"]] = seed_of[row["origin"]]
                next_frontier.append(row["id"])
            reached.append({**row, "hop": hop, "seed": seed_of[row["origin"]]})
        frontier = next_frontier[:MAX_FLOW_FRONTIER]
    return reached


async def _subtree(store: KnowledgeGraphStore, repo: str, root_ids: list[str]) -> list[dict]:
    return await store.subtree(repo, root_ids, limit=MAX_CONTENTS) if root_ids else []


def _source_candidates(
    focused: Sequence[_Focus | None], matched: list[dict], contents: list[dict],
    earlier_ranges: Sequence[tuple[str, int, int]] = (),
    found_ranges: Sequence[tuple[str, int, int]] = (),
    question: str = "",
    flow: Sequence[dict] = (),
) -> list[tuple]:
    """(id, name, labels, path, start, end, note) of the code worth showing, most telling first.

    Code cited just before, then what's in focus, files the question names, the
    best matches and what they lead to, code found by its contents, and only then
    other symbols whose names merely share a word with the question. `id` is None for a range that isn't one node's code; `note`
    says why it's shown.
    """
    candidates: dict[str, tuple] = {
        f"{path}:{start}-{end}": (None, None, (), path, start, end, "cited earlier")
        for path, start, end in earlier_ranges
    }
    named = set(_FILE_NAME_RE.findall(question))
    named_files = [
        row for row in matched
        if NodeLabel.FILE in row.get("labels", []) and any(
            (row.get("path") or "").endswith(name) or row.get("name") == name for name in named
        )
    ]
    focus_rows = [{"id": f.detail["id"], "labels": f.detail["labels"], **f.detail["properties"]} for f in focused if f]
    with_code = [row for row in matched if not _holds_only(row.get("labels", []))]
    _add_node_sources(candidates, [*focus_rows, *named_files, *with_code[:2]])
    # the seeds' own functions (hop 0) are in the matched files already
    followed = sorted(
        (row for row in flow if row["hop"] > 0 and NodeLabel.SYMBOL in row["labels"]),
        key=lambda row: (row["seed"], row["hop"], FLOW_SOURCE_PRIORITY.get(row["type"], 2), -(row["count"] or 1)),
    )
    _add_node_sources(candidates, list({row["id"]: row for row in followed}.values())[:MAX_FLOW_SOURCES], MAX_FLOW_SNIPPET_LINES)
    for path, start, end in found_ranges:
        candidates.setdefault(f"{path}:{start}-{end}", (None, None, (), path, start, end, "contains the question's words"))
    _add_node_sources(candidates, [
        *matched[:MAX_SNIPPET_MATCHES],
        # only functions here: a whole file from an open folder is mostly noise
        *(row for row in contents if NodeLabel.SYMBOL in row["labels"]),
    ])
    return list(candidates.values())[:MAX_SNIPPETS]


def _add_node_sources(candidates: dict[str, tuple], rows: Sequence[dict], max_lines: int | None = None) -> None:
    for row in rows:
        labels = row.get("labels", [])
        if NodeLabel.SYMBOL in labels and row.get("file_path") and row.get("start_line"):
            end = row.get("end_line")
            if max_lines and end:
                end = min(end, row["start_line"] + max_lines - 1)
            source = (row["file_path"], row["start_line"], end)
        elif NodeLabel.FILE in labels and row.get("path"):
            source = (row["path"], None, None)
        else:
            continue
        candidates.setdefault(row["id"], (row["id"], row.get("name") or row.get("path"), labels, *source, None))


def _holds_only(labels: Sequence[str]) -> bool:
    """A folder: it holds code but has none of its own."""
    return NodeLabel.MODULE in labels


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
    #: (source id, line number) for a line of code
    code: tuple[str, int] | None = None


class ContextBuilder:
    """Lays out what was fetched; only what survives the budget counts as shown."""

    def __init__(self, repo_full_name: str, commit: str | None = None):
        self.repo = repo_full_name
        self.commit = commit
        self.lines: list[_Line] = []
        #: source id -> (snippet, node id); what survives the budget is worked out in `build`
        self.sources: dict[str, tuple[Snippet, str | None]] = {}
        self.refs: dict[str, str] = {}  # node id -> ref
        self.names: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.modules: dict[str, str] = {}
        self.mentioned: list[str] = []
        self._heading: str | None = None
        #: lines added by lookups after the context was sent, outside its budget
        self.looked_up: list[_Line] = []

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
        self.add_around(focus.around, MAX_NEIGHBOURS, around_heading or f"Relations of [{ref}] {self.names[ref]}")

    def add_around(self, around: dict, limit: int, heading: str) -> None:
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
                self.add_around(around, MAX_RELATIONS, f"Relations of [{ref}] {self.names[ref]}")

    def shown_code(self, path: str) -> list[tuple[str, int, int]]:
        """`(source, start, end)` of each block of a file's code shown so far, as it can be cited."""
        return [
            (source, shown.start_line, shown.end_line)
            for source, shown in self.build().sources.items()
            if shown.path == path
        ]

    def node_of(self, ref: str) -> str | None:
        """The node a reference stands for, if this context gave it out."""
        return next((node_id for node_id, given in self.refs.items() if given == ref), None)

    def add_found(self, heading: str, hits: Sequence[tuple[str, int]]) -> None:
        """Places in the code, as `path:line`."""
        self.heading(heading)
        for path, line in hits:
            self.lines.append(_Line(f"- {clean(path, MAX_LINE_CHARS)}:{line}"))

    def add_flow(self, rows: Sequence[dict]) -> None:
        """Where the best matches lead: the nodes reached, then the edges that reach them."""
        if not rows:
            return
        self.heading("Followed from the matches")
        for row in rows:
            if row["id"] not in self.refs:
                self.node(row["id"], row.get("name") or row.get("path"), _describe(row, row["labels"]), row["labels"])
        for row in rows:
            self.edge(row)

    def add_sources(self, snippets: list[tuple[tuple, Snippet]]) -> None:
        """Code numbered as in the file, each block with an id to cite it by, within its own budget."""
        lines_left, chars_left = MAX_SOURCE_LINES, MAX_SOURCE_CHARS
        for (node_id, name, labels, note), snippet in snippets:
            if lines_left <= 0 or chars_left <= 0:
                break
            source = f"s{len(self.sources) + 1}"
            self.sources[source] = (snippet, node_id)
            end = snippet.start_line + len(snippet.lines) + snippet.omitted - 1
            where = f"{clean(snippet.path, MAX_LINE_CHARS)}:{snippet.start_line}-{end}"
            if node_id is None:
                self.lines.append(_Line(f"Source {source} ({where}), {note}:"))
            else:
                ref = self.ref(node_id, name, labels)
                self.lines.append(_Line(f"Source {source} of [{ref}] {self.names[ref]} ({where}):", (ref,)))
            shown = 0
            for offset, code in enumerate(snippet.lines):
                number = snippet.start_line + offset
                text = f"  {number:>5} | {prompt_line(code)}"
                if shown >= lines_left or len(text) > chars_left:
                    break
                self.lines.append(_Line(text, code=(source, number)))
                shown += 1
                chars_left -= len(text)
            lines_left -= shown
            hidden = snippet.omitted + len(snippet.lines) - shown
            if hidden:
                # where to read on from, so a lookup asks for the rest rather than the same lines again
                self.lines.append(_Line(f"  … {hidden} more lines, from line {snippet.start_line + shown}"))

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

    def add_repo_map(self, files: list[dict]) -> None:
        """Files with the definitions most used from them, so the reader knows they exist."""
        if not files:
            return
        self.heading("Files in the repository")
        for row in files:
            ref = self.ref(row["id"], row["path"], [NodeLabel.FILE])
            symbols = ", ".join(clean(name) for name in row["key_symbols"][:MAX_MAP_SYMBOLS])
            where = clean(row["path"], MAX_LINE_CHARS)
            self.lines.append(_Line(f"- [{ref}] {where}" + (f": {symbols}" if symbols else ""), (ref,)))

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

    def look_up(self, fill: Callable[[], None]) -> str:
        """The lines `fill` adds, kept as part of what was shown and returned as text for the model.

        They continue the context's numbering, so a reference or code citation from
        a lookup is checked like any other; past `MAX_LOOKUP_LINES` nothing more is added.
        """
        start = len(self.lines)
        self._heading = None
        fill()
        added = self.lines[start:]
        del self.lines[start:]
        self._heading = None
        kept = added[:max(MAX_LOOKUP_LINES - len(self.looked_up), 0)]
        self.looked_up += kept
        rendered = [line.text[:MAX_LINE_CHARS] for line in kept]
        if len(added) > len(kept):
            rendered.append(f"… {len(added) - len(kept)} more lines omitted: there's no room to look up more, so answer with what's shown")
        return "\n".join(rendered) or "(nothing found)"

    @property
    def lookup_room(self) -> int:
        """Lines lookups may still add."""
        return max(MAX_LOOKUP_LINES - len(self.looked_up), 0)

    def build(self) -> GroundingContext:
        """Apply the budget, then fence the whole thing as data; what was looked up since counts as shown."""
        sent = self.lines[:MAX_CONTEXT_LINES]
        kept = [*sent, *self.looked_up]
        rendered = [line.text[:MAX_LINE_CHARS] for line in sent]
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
            sources=self._shown_sources(kept),
            commit=self.commit,
        )

    def _shown_sources(self, kept: list[_Line]) -> dict[str, ShownSource]:
        """Each block's lines that made it into the prompt: only those can be cited."""
        last: dict[str, int] = {}
        for line in kept:
            if line.code:
                last[line.code[0]] = line.code[1]
        shown = {}
        for source, end in last.items():
            snippet, node_id = self.sources[source]
            lines = snippet.lines[:end - snippet.start_line + 1]
            shown[source] = ShownSource(snippet.path, snippet.start_line, end, lines, node_id, language_of(snippet.path))
        return shown


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
    "ShownSource",
    "clean",
    "rank_matches",
    "search_terms",
    "module_for",
    "owning_module_id",
]
