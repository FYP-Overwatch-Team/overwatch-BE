"""What the model can look up while it answers: the graph and the code, read-only.

Each lookup is laid out by the question's `ContextBuilder`, so what it shows is
numbered with the rest of the context and checked like it: a reference the
model invents, or code it wasn't shown, is still dropped from the answer.

The arguments come from the model, so they are untrusted: a reference must be
one the context gave out, a path must be a file the graph indexed, and every
size is capped. A bad call gets a short error back, never an exception.
"""

import asyncio
import json
from dataclasses import dataclass, field

from app.integrations.llm import ToolCall
from app.knowledge_graph.build.ids import file_id
from app.knowledge_graph.build.model import EdgeType, NodeLabel
from app.knowledge_graph.ports import KnowledgeGraphStore
from app.query.context import ContextBuilder, clean, search_nodes
from app.query.source import find_in_code, read_snippet

SEARCH_GRAPH = "search_graph"
RELATED = "related"
READ_CODE = "read_code"
GREP_CODE = "grep_code"
OUT, IN = "out", "in"

MAX_QUERY_CHARS = 100
MAX_GREP_CHARS = 64
MIN_GREP_CHARS = 3
#: What one lookup may return: search hits, relations, lines of code, places found.
MAX_SEARCH_RESULTS = 10
MAX_RELATED = 20
MAX_READ_LINES = 150
MAX_GREP_HITS = 15
LOOKUP_TIMEOUT_SECONDS = 10
#: What a repeated lookup gets instead of the same result again.
ALREADY_SHOWN = "already shown as {source} ({path}, lines {start}–{end}); cite it from there, or read on from line {next}"
ALREADY_LOOKED_UP = "already looked up above; use that result"
NO_CODE = "error: no code is available for this repository"
READ_ON_FROM = "lines {start}–{end} are already shown as {source}; reading on from line {next}:"
#: Shown to the person asking while a lookup runs.
MAX_STATUS_CHARS = 60
#: The edges `related` follows: what code does, what it's made of, and for a folder what it
#: holds and depends on.
RELATED_EDGES = (
    EdgeType.CALLS, EdgeType.RENDERS, EdgeType.REQUESTS, EdgeType.HANDLES, EdgeType.EXTENDS,
    EdgeType.IMPLEMENTS, EdgeType.IMPORTS, EdgeType.DEFINES, EdgeType.HAS_MEMBER, EdgeType.CONTAINS,
    EdgeType.DEPENDS_ON,
)

_REF = {"type": "string", "description": "A reference from the context or an earlier lookup, like n4."}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": SEARCH_GRAPH,
            "description": "Find folders, files and functions by name; part of a name works ('signup' finds SignupForm).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": MAX_QUERY_CHARS}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": RELATED,
            "description": "What a node uses (out: calls, renders, imports, requests) or what uses it (in).",
            "parameters": {
                "type": "object",
                "properties": {"ref": _REF, "direction": {"type": "string", "enum": [OUT, IN]}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": READ_CODE,
            "description": (
                f"Read a node's code by its ref, or lines of an indexed file by path (at most {MAX_READ_LINES} lines)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": _REF,
                    "path": {"type": "string", "maxLength": 500},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": GREP_CODE,
            "description": "Find where some exact text appears in the code, case-insensitive, as path:line.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": MIN_GREP_CHARS, "maxLength": MAX_GREP_CHARS}},
                "required": ["query"],
            },
        },
    },
]


@dataclass(slots=True)
class Lookups:
    """The tools, run against one question's context."""

    builder: ContextBuilder
    store: KnowledgeGraphStore
    repo: str
    commit: str | None
    #: lookups made so far (tool, what was asked), and file ranges being read right now
    asked: set[tuple[str, str]] = field(default_factory=set)
    reading: list[tuple[str, int, int]] = field(default_factory=list)
    #: lookups that asked for something already shown, for the logs
    repeats: int = 0

    def status(self, call: ToolCall) -> str:
        """What the person asking sees while it runs, built from checked arguments only."""
        args = _arguments(call) or {}
        node = self._name(args.get("ref"))
        text = {
            SEARCH_GRAPH: f"Searching for “{clean(args.get('query', ''), MAX_STATUS_CHARS)}”",
            RELATED: f"Finding what uses {node}" if args.get("direction") == IN else f"Following what {node} uses",
            READ_CODE: f"Reading {node if args.get('ref') else clean(args.get('path', 'a file'), MAX_STATUS_CHARS)}",
            GREP_CODE: f"Searching the code for “{clean(args.get('query', ''), MAX_STATUS_CHARS)}”",
        }.get(call.name, "Looking further")
        return text[:MAX_STATUS_CHARS * 2]

    async def run(self, call: ToolCall) -> str:
        """The lookup's result as text for the model, or a short error."""
        args = _arguments(call)
        if args is None:
            return "error: the arguments must be a JSON object"
        handler = {
            SEARCH_GRAPH: self._search, RELATED: self._related, READ_CODE: self._read, GREP_CODE: self._grep,
        }.get(call.name)
        if handler is None:
            return f"error: there is no tool called {clean(call.name)}"
        # checked and recorded with nothing awaited in between, so calls run together can't both pass
        if call.name != READ_CODE:
            key = (call.name, _asked(call.name, args))
            if key in self.asked:
                self.repeats += 1
                return ALREADY_LOOKED_UP
            self.asked.add(key)
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT_SECONDS):
                return await handler(args)
        except TimeoutError:
            return "error: the lookup took too long"

    async def _search(self, args: dict) -> str:
        query = str(args.get("query") or "")[:MAX_QUERY_CHARS]
        rows = (await search_nodes(self.store, self.repo, query))[:MAX_SEARCH_RESULTS]
        return self.builder.look_up(lambda: self.builder.add_matches(rows, []))

    async def _related(self, args: dict) -> str:
        node_id = self.builder.node_of(_bare(args.get("ref")))
        if node_id is None:
            return "error: unknown ref; use one from the context or an earlier lookup"
        direction = IN if args.get("direction") == IN else OUT
        around = await self.store.neighbours(
            self.repo, node_id, direction=direction, edge_types=RELATED_EDGES, depth=1, limit=MAX_RELATED,
        )
        ref = self.builder.refs[node_id]
        heading = f"{'What uses' if direction == IN else 'What'} [{ref}] {self.builder.names[ref]}{'' if direction == IN else ' uses'}"
        return self.builder.look_up(lambda: self.builder.add_around(around, MAX_RELATED, heading))

    async def _read(self, args: dict) -> str:
        if args.get("ref"):
            node_id = self.builder.node_of(_bare(args.get("ref")))
            if node_id is None:
                return "error: unknown ref; use one from the context or an earlier lookup"
        else:
            node_id = file_id(self.repo, str(args.get("path") or "").strip("/"))
        if self.commit is None:
            return NO_CODE
        detail = await self.store.node_detail(self.repo, node_id)
        if detail is None:
            return "error: that file isn't in the repository's index"
        properties, labels = detail["properties"], detail["labels"]
        path = properties.get("file_path") or properties.get("path")
        if not path or not ({NodeLabel.FILE, NodeLabel.SYMBOL} & set(labels)):
            return "error: that has no code of its own; read one of the files or functions in it"
        start = _line(args.get("start_line")) or properties.get("start_line") or 1
        end = _line(args.get("end_line")) or properties.get("end_line") or start + MAX_READ_LINES - 1
        end = min(max(end, start), start + MAX_READ_LINES - 1)

        # only what isn't shown yet is read; the rest gets a pointer to where it already is
        shown = sorted([*self.builder.shown_code(path), *(("", a, b) for p, a, b in self.reading if p == path)], key=lambda block: block[1])
        asked_from, skipped = start, None
        for source, first, last in shown:
            if first <= start <= last:
                skipped = (source, first, last)
                start = last + 1
        if start > end:
            self.repeats += 1
            source, first, last = skipped
            if not source:
                return ALREADY_LOOKED_UP
            return ALREADY_SHOWN.format(source=source, path=path, start=first, end=last, next=last + 1)
        end = min([end, *(first - 1 for _, first, _ in shown if first > start)])

        self.reading.append((path, start, end))
        try:
            snippet = await read_snippet(self.repo, self.commit, path, start, end, max_lines=MAX_READ_LINES)
        finally:
            self.reading.remove((path, start, end))
        if snippet is None:
            return "error: the file couldn't be read, or it ends before that line"
        name = properties.get("name") or path
        text = self.builder.look_up(lambda: self.builder.add_sources([((node_id, name, labels, None), snippet)]))
        if skipped and skipped[0] and start > asked_from:
            return READ_ON_FROM.format(source=skipped[0], start=skipped[1], end=skipped[2], next=start) + "\n" + text
        return text

    async def _grep(self, args: dict) -> str:
        text = str(args.get("query") or "")[:MAX_GREP_CHARS]
        if self.commit is None:
            return NO_CODE
        if len(text.strip()) < MIN_GREP_CHARS:
            return "error: give at least three characters to search for"
        hits = await find_in_code(self.repo, self.commit, [text])
        places = list(dict.fromkeys((path, line) for path, line, _ in hits))[:MAX_GREP_HITS]
        if not places:
            return "(nothing found)"
        return self.builder.look_up(lambda: self.builder.add_found(f"Code containing “{clean(text)}”", places))

    def _name(self, ref: object) -> str:
        found = _bare(ref)
        return self.builder.names.get(found, "it") if self.builder.node_of(found) else "it"


def _asked(tool: str, args: dict) -> str:
    """What a lookup asked for, the same however it's spelled: a ref and direction, or the words searched."""
    if tool == RELATED:
        return f"{_bare(args.get('ref'))}:{IN if args.get('direction') == IN else OUT}"
    return " ".join(str(args.get("query") or "").lower().split())


def _arguments(call: ToolCall) -> dict | None:
    try:
        args = json.loads(call.arguments or "{}")
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def _bare(ref: object) -> str:
    """`[n4]` or `n4` -> `n4`."""
    return str(ref or "").strip().strip("[]")


def _line(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
