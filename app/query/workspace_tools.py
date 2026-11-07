"""What the model can read from the workspace while it answers: tickets, docs, decisions and who's in it.

It can't change anything. It may draft a ticket, page or decision (`propose_*`), which is saved
with the answer as a proposal; only the person who asked can create it, after editing it if they
like (`chat.proposals`).

Tickets come from every connected tracker and from the app's own board, limited to what the
workspace can see (`work_items.service.visibility`); docs are the pages not archived. Their text
is written by people, so it's escaped and fenced as data. A record the model cites must be one
these lookups gave it as `[rN]`, and its link is built here. The workspace and the person asking
are the asker's, never an argument.
"""

import re
from dataclasses import dataclass, field

from app.boards import repository as boards
from app.connectors import registry
from app.connectors import repository as connectors
from app.core.constants import (
    NATIVE_SOURCE,
    ChatActionKind,
    ChatRecordKind,
    DecisionStatus,
    DocumentKind,
    StatusCategory,
)
from app.core.exceptions import NotFoundError
from app.core.links import decision_href, document_href, work_item_href
from app.db import mongo
from app.github import repository as repos
from app.query.context import clean, escape
from app.query.grounding import safe_markdown
from app.sprints import repository as sprints
from app.work_items import service as work_items
from app.workspaces import repository as workspaces
from app.workspaces.service import list_members

FIND_TICKETS = "find_tickets"
FIND_DOCS = "find_docs"
FIND_DECISIONS = "find_decisions"
OPEN_RECORD = "open_record"
WORKSPACE_INFO = "workspace_info"
PROPOSE_TICKET = "propose_ticket"
PROPOSE_DOC = "propose_doc"
PROPOSE_DECISION = "propose_decision"
CURRENT_SPRINT = "current"
#: any ticket not done
OPEN = "open"
MAX_QUERY_CHARS = 100
#: what one search returns
MAX_RESULTS = 8
MAX_MEMBERS = 30
MAX_TITLE_CHARS = 120
MAX_SNIPPET_CHARS = 160
#: a record is read in parts this long, so a long one is read whole over a few lookups
READ_CHARS = 20_000
#: what the app's own board and docs are called next to a tracker's name
APP_SOURCE = "the app"
#: drafts one answer may make, and how long their parts may be (what every kind accepts)
MAX_DRAFTS = 3
MAX_DRAFT_TITLE_CHARS = 200
MAX_DRAFT_BODY_CHARS = 20_000
MAX_STATEMENT_CHARS = 2_000
#: what a draft tool answers, so the model doesn't say it was created
DRAFTED = (
    "Drafted. It shows under your answer for them to edit and approve; nothing exists until they do, "
    "so say you drafted it, never that it was created, and give it no reference."
)

_QUERY = {"type": "string", "maxLength": MAX_QUERY_CHARS}


def _draft_tool(name: str, what: str, body: str, *, statement: bool = False) -> dict:
    """A tool that drafts `what` for the person asking to approve; `body` names its Markdown text."""
    properties = {
        "title": {"type": "string", "maxLength": MAX_DRAFT_TITLE_CHARS},
        **({"statement": {"type": "string", "maxLength": MAX_STATEMENT_CHARS}} if statement else {}),
        body: {"type": "string", "maxLength": MAX_DRAFT_BODY_CHARS},
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                f"Draft {what} for the person asking to review, only when they ask for one or clearly would want it. "
                f"Nothing is created until they approve it. Write the {body} in Markdown."
            ),
            "parameters": {"type": "object", "properties": properties, "required": ["title", "statement"] if statement else ["title"]},
        },
    }


WORKSPACE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": FIND_TICKETS,
            "description": (
                "Find tickets on the workspace's boards: every connected tracker (Jira, ...) and the app's own. "
                "Every word must appear in the key, title or description; leave query out to list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": _QUERY,
                    "sprint": {"type": "string", "enum": [CURRENT_SPRINT]},
                    "status": {"type": "string", "enum": [*StatusCategory, OPEN]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": FIND_DOCS,
            "description": "Find docs pages (the app's and Confluence's). Every word must appear in the title or text.",
            "parameters": {"type": "object", "properties": {"query": _QUERY}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": FIND_DECISIONS,
            "description": (
                "Find the team's decisions. Every word must appear in the title, statement, area or background; "
                "leave query out to list."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": _QUERY, "status": {"type": "string", "enum": list(DecisionStatus)}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": OPEN_RECORD,
            "description": (
                f"Read a ticket, page or decision in full by its ref, like r1. A long one comes in parts of {READ_CHARS} "
                "characters; read on with the offset it gives until you have all of it."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": WORKSPACE_INFO,
            "description": "The workspace: its name, you and your role, its members, repositories and connections.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    _draft_tool(PROPOSE_TICKET, "a ticket", "description"),
    _draft_tool(PROPOSE_DOC, "a docs page", "content"),
    _draft_tool(PROPOSE_DECISION, "a decision", "background", statement=True),
]
WORKSPACE_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in WORKSPACE_TOOLS)


@dataclass(slots=True)
class Workspace:
    """The asker's workspace, and what its lookups have handed out so far."""

    workspace_id: str
    user_id: str
    #: `r1` -> the record as an answer cites it; everything found so far
    records: dict[str, dict] = field(default_factory=dict)
    #: what the ticket searches saw, for the buttons under the answer
    searched: bool = False
    no_tracker: bool = False
    #: tickets, pages and decisions drafted for the person to approve, as `{kind, fields}`
    drafts: list[dict] = field(default_factory=list)

    @property
    def actions(self) -> list[ChatActionKind]:
        """Connect a tracker when none is, and create a ticket when no search found one."""
        found = any(record["kind"] == ChatRecordKind.WORK_ITEM for record in self.records.values())
        offers = {ChatActionKind.CONNECT_TRACKER: self.no_tracker, ChatActionKind.CREATE_TICKET: self.searched and not found}
        return [action for action, offered in offers.items() if offered]

    @property
    def handlers(self) -> dict:
        return {
            FIND_TICKETS: self.find_tickets, FIND_DOCS: self.find_docs, FIND_DECISIONS: self.find_decisions,
            OPEN_RECORD: self.open_record, WORKSPACE_INFO: self.workspace_info,
            PROPOSE_TICKET: self.propose_ticket, PROPOSE_DOC: self.propose_doc, PROPOSE_DECISION: self.propose_decision,
        }

    def status(self, tool: str, args: dict) -> str | None:
        """What the person asking sees while a lookup runs."""
        query = clean(args.get("query") or "", MAX_QUERY_CHARS)
        record = self.records.get(_bare(args.get("ref")))
        return {
            FIND_TICKETS: f"Searching tickets for “{query}”" if query else "Looking through the tickets",
            FIND_DOCS: f"Searching docs for “{query}”",
            FIND_DECISIONS: f"Searching decisions for “{query}”" if query else "Looking through the decisions",
            OPEN_RECORD: f"Reading {clean(record['key'] or record['title'])}" if record else "Reading",
            WORKSPACE_INFO: "Looking at the workspace",
            PROPOSE_TICKET: "Drafting a ticket",
            PROPOSE_DOC: "Drafting a page",
            PROPOSE_DECISION: "Drafting a decision",
        }.get(tool)

    async def find_tickets(self, args: dict) -> str:
        trackers = sorted({
            registry.get(target["provider"]).name
            for target in await connectors.list_targets(self.workspace_id)
            if registry.has_work_items(target["provider"])
        })
        self.searched, self.no_tracker = True, not trackers
        heading = (
            f"Trackers connected: {', '.join(trackers)}." if trackers
            else "No tracker is connected; only the app's own board was searched."
        )

        query = {
            "workspace_id": self.workspace_id, **await work_items.visibility(self.workspace_id),
            **_words(args, ("key", "title", "description_md")),
        }
        if args.get("sprint") == CURRENT_SPRINT:
            query["sprint_id"] = {"$in": await sprints.active_ids(self.workspace_id)}
        status = args.get("status")
        if status == OPEN:
            query["status_category"] = {"$ne": StatusCategory.DONE}
        elif status in StatusCategory:
            query["status_category"] = status
        items = await mongo.work_items().find(query).sort("updated_at", -1).limit(MAX_RESULTS).to_list(None)
        if not items:
            return _fenced(f"{heading}\nNo tickets matched.")

        rows = [heading]
        board_ids: dict[tuple[str, str | None], str | None] = {}
        for item in items:
            place = (item["source"], item.get("target_key"))
            if place not in board_ids:
                board = await boards.find(self.workspace_id, *place)
                board_ids[place] = board["_id"] if board else None
            status = item.get("status") or item["status_category"]
            ref = self._cite(
                ChatRecordKind.WORK_ITEM, item, work_item_href(board_ids[place], item["key"]),
                key=item["key"], source=item["source"], status=status,
            )
            row = f"- [{ref}] {clean(item['key'])} · {_source_name(item['source'])} · {clean(status)} · {clean(item['title'], MAX_TITLE_CHARS)}"
            if name := (item.get("assignee") or {}).get("name"):
                row += f" · assigned to {clean(name)}"
            rows.append(_with_snippet(row, item.get("description_md")))
        return _fenced("\n".join(rows))

    async def find_docs(self, args: dict) -> str:
        query = {
            "workspace_id": self.workspace_id, "kind": DocumentKind.PAGE, "archived_at": None,
            **_words(args, ("title", "content_md")),
        }
        pages = await mongo.documents().find(query).sort("updated_at", -1).limit(MAX_RESULTS).to_list(None)
        if not pages:
            return _fenced("No docs pages matched.")
        rows = []
        for page in pages:
            ref = self._cite(ChatRecordKind.DOCUMENT, page, document_href(page["_id"]), source=page["source"])
            row = f"- [{ref}] {clean(page['title'], MAX_TITLE_CHARS)} · {_source_name(page['source'])}"
            rows.append(_with_snippet(row, page.get("content_md")))
        return _fenced("\n".join(rows))

    async def find_decisions(self, args: dict) -> str:
        query = {"workspace_id": self.workspace_id, **_words(args, ("title", "statement", "area", "context_md"))}
        if args.get("status") in DecisionStatus:
            query["status"] = args["status"]
        decisions = await mongo.decisions().find(query).sort("updated_at", -1).limit(MAX_RESULTS).to_list(None)
        if not decisions:
            return _fenced("No decisions matched.")
        rows = []
        for decision in decisions:
            ref = self._cite(ChatRecordKind.DECISION, decision, decision_href(decision["_id"]), status=decision["status"])
            row = f"- [{ref}] {clean(decision['title'], MAX_TITLE_CHARS)} · {decision['status']}"
            if decision.get("area"):
                row += f" · {clean(decision['area'])}"
            rows.append(_with_snippet(row, decision["statement"]))
        return _fenced("\n".join(rows))

    async def open_record(self, args: dict) -> str:
        record = self.records.get(_bare(args.get("ref")))
        if record is None:
            return "error: unknown ref; use one a lookup gave, like r1"
        read = {
            ChatRecordKind.WORK_ITEM: self._ticket_text,
            ChatRecordKind.DOCUMENT: self._page_text,
            ChatRecordKind.DECISION: self._decision_text,
        }[record["kind"]]
        found = await read(record["id"])
        if found is None:
            return "error: it has been deleted or archived, or its tracker disconnected"
        text = escape(found)
        offset = args.get("offset") if isinstance(args.get("offset"), int) else 0
        if offset < 0 or (offset and offset >= len(text)):
            return f"error: the offset must be below {len(text)}"
        part = text[offset:offset + READ_CHARS]
        rest = len(text) - offset - len(part)
        more = f"\n{rest} more characters; read on with offset {offset + len(part)}" if rest else ""
        return _fenced(part) + more

    async def workspace_info(self, args: dict) -> str:
        workspace = await workspaces.get(self.workspace_id)
        members = await list_members(self.workspace_id)
        you = next((member for member in members if member["user_id"] == self.user_id), None)
        lines = [f"Workspace: {clean(workspace['name'] if workspace else '')}"]
        if you:
            lines.append(f"You: {_person(you)} ({you['role']})")
        lines.append(f"Members ({len(members)}):")
        lines += [f"- {_person(member)} · {member['role']}" for member in members[:MAX_MEMBERS]]
        connected = await repos.list_for_workspace(self.workspace_id)
        lines.append("Repositories:" if connected else "Repositories: none connected")
        lines += [f"- {clean(repo['repo_full_name'])} · indexing {repo['parse_status']}" for repo in connected]
        connections = [
            (registry.CONNECTORS[c["provider"]], c["status"])
            for c in await connectors.list_connections(self.workspace_id) if c["provider"] in registry.CONNECTORS
        ]
        lines.append("Connections:" if connections else "Connections: none (trackers and docs sources)")
        lines += [f"- {connector.name} ({connector.category}) · {status}" for connector, status in connections]
        return _fenced("\n".join(lines))

    async def propose_ticket(self, args: dict) -> str:
        return self._draft(ChatRecordKind.WORK_ITEM, args, args.get("description"))

    async def propose_doc(self, args: dict) -> str:
        return self._draft(ChatRecordKind.DOCUMENT, args, args.get("content"))

    async def propose_decision(self, args: dict) -> str:
        return self._draft(ChatRecordKind.DECISION, args, args.get("background"), args.get("statement"))

    def _draft(self, kind: ChatRecordKind, args: dict, body: object, statement: object = None) -> str:
        """Keep a draft for the person to approve; its fields are checked again when they do."""
        if len(self.drafts) >= MAX_DRAFTS:
            return f"error: at most {MAX_DRAFTS} drafts per answer"
        title = " ".join(str(args.get("title") or "").split())[:MAX_DRAFT_TITLE_CHARS]
        # once approved it's a record shown with clickable links, so it's cleaned like an answer
        statement = safe_markdown(str(statement or "").strip()[:MAX_STATEMENT_CHARS])
        if not title or (kind == ChatRecordKind.DECISION and not statement):
            return "error: give it a title" + (" and a statement" if kind == ChatRecordKind.DECISION else "")
        fields = {"title": title, "body": safe_markdown(str(body or "").strip()[:MAX_DRAFT_BODY_CHARS]), "statement": statement}
        self.drafts.append({"kind": kind, "fields": fields})
        return DRAFTED

    async def _ticket_text(self, item_id: str) -> str | None:
        try:
            item = await work_items.get_visible(self.workspace_id, item_id)
        except NotFoundError:
            return None

        def keys(linked: list[dict]) -> str:
            return ", ".join(f"{one.get('key')} {one.get('title') or ''}".strip() for one in linked)

        return _text(f"{item['key']}: {item['title']}", {
            "Source": _source_name(item["source"]),
            "Status": item.get("status") or item["status_category"],
            "Type": item.get("issue_type"),
            "Priority": item.get("priority"),
            "Assignee": (item.get("assignee") or {}).get("name"),
            "Reporter": (item.get("reporter") or {}).get("name"),
            "Labels": ", ".join(item.get("labels") or []),
            "Due": item.get("due_date"),
            "Story points": item.get("story_points"),
            "Parent": keys([item["parent"]]) if item.get("parent") else None,
            "Subtasks": keys(item.get("subtasks") or []),
            "Links": keys(item.get("links") or []),
            "Updated": item["updated_at"].date().isoformat(),
        }, item.get("description_md") or "(no description)")

    async def _page_text(self, page_id: str) -> str | None:
        page = await mongo.documents().find_one(
            {"_id": page_id, "workspace_id": self.workspace_id, "kind": DocumentKind.PAGE, "archived_at": None},
        )
        if page is None:
            return None
        fields = {"Source": _source_name(page["source"]), "Updated": page["updated_at"].date().isoformat()}
        return _text(page["title"], fields, page.get("content_md") or "(empty page)")

    async def _decision_text(self, decision_id: str) -> str | None:
        decision = await mongo.decisions().find_one({"_id": decision_id, "workspace_id": self.workspace_id})
        if decision is None:
            return None
        body = decision.get("context_md") or "(no background)"
        if evidence := decision.get("evidence"):
            body += "\n\nEvidence:\n" + "\n".join(
                f"- {e['stance']} \"{e['quote']}\" from {e['title']}" + (f" ({e['note']})" if e.get("note") else "")
                for e in evidence
            )
        return _text(decision["title"], {
            "Status": decision["status"],
            "Area": decision.get("area"),
            "Review by": decision.get("review_by"),
            "Statement": decision["statement"],
            "Updated": decision["updated_at"].date().isoformat(),
        }, body)

    def _cite(
        self, kind: ChatRecordKind, doc: dict, href: str, *, key: str | None = None, source: str | None = None,
        status: str | None = None,
    ) -> str:
        """The record's ref, the same one each time it's found."""
        ref = next((ref for ref, record in self.records.items() if record["id"] == doc["_id"]), None)
        if ref is None:
            ref = f"r{len(self.records) + 1}"
            self.records[ref] = {
                "ref": ref, "kind": kind, "id": doc["_id"], "key": key, "title": doc["title"], "source": source,
                "status": status, "href": href,
            }
        return ref


def _words(args: dict, fields: tuple[str, ...]) -> dict:
    """Every word of the query in one of `fields`, case-insensitive; nothing when there's no query."""
    words = str(args.get("query") or "")[:MAX_QUERY_CHARS].split()
    if not words:
        return {}
    return {"$and": [
        {"$or": [{name: {"$regex": re.escape(word), "$options": "i"}} for name in fields]} for word in words
    ]}


def _with_snippet(row: str, text: str | None) -> str:
    return f"{row}\n  {clean(text, MAX_SNIPPET_CHARS)}" if text else row


def _text(heading: str, fields: dict, body: str) -> str:
    """A record for reading: its heading, the fields it has, then its whole text."""
    lines = [heading, *(f"{name}: {value}" for name, value in fields.items() if value)]
    return "\n".join([*lines, "", body])


def _person(member: dict) -> str:
    return clean(member.get("name") or member.get("username") or member.get("github_login") or "Someone")


def _source_name(source: str) -> str:
    return APP_SOURCE if source == NATIVE_SOURCE else registry.get(source).name


def _fenced(text: str) -> str:
    """Workspace text is data, never instructions; it's escaped, so it can't close this itself."""
    return f"<workspace_data>\n{text}\n</workspace_data>"


def _bare(ref: object) -> str:
    """`[r1]` or `r1` -> `r1`."""
    return str(ref or "").strip().strip("[]")
