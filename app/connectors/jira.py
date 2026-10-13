"""Jira Cloud connector: issues mirrored into `work_items`, and board changes written back."""

import hashlib
import re
from datetime import datetime

import httpx
import structlog

from app.connectors.oauth import OAuthConfig, http
from app.connectors.ports import AuthBackend, Page
from app.core.constants import BoardKind, Provider, SprintState, StatusCategory
from app.core.exceptions import ConflictError, ExternalServiceError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors.jira")

API_BASE = "https://api.atlassian.com/ex/jira"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
WRITE_SCOPE = "write:jira-work"
# Jira Software (boards, their columns, sprints and the backlog) only accepts granular scopes
AGILE_SCOPES = (
    "read:board-scope:jira-software",
    "write:board-scope:jira-software",
    "read:board-scope.admin:jira-software",
    "read:sprint:jira-software",
    "write:sprint:jira-software",
    "read:issue:jira-software",
)
AGILE_SCOPE = "read:board-scope.admin:jira-software"

# Classic platform scopes plus Jira Software's; offline_access is what returns a (rotating) refresh token.
OAUTH = OAuthConfig(
    provider=Provider.JIRA,
    authorize_url="https://auth.atlassian.com/authorize",
    token_url="https://auth.atlassian.com/oauth/token",
    scopes=("read:jira-work", WRITE_SCOPE, "read:jira-user", *AGILE_SCOPES, "offline_access"),
    client_id_setting="jira_client_id",
    client_secret_setting="jira_client_secret",
    redirect_path="/jira/callback",
    extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
)

PAGE_SIZE = 100
PROJECTS_PAGE_SIZE = 50
FIELDS = (
    "summary,description,status,assignee,priority,issuetype,created,updated,statuscategorychangedate,"
    "reporter,labels,parent,duedate,subtasks,issuelinks"
)
# the estimate field's name in team-managed and company-managed projects; its id differs per site
STORY_POINT_NAMES = {"story point estimate", "story points"}
# how Jira marks its sprint field, whose id (customfield_NNNNN) differs per site
SPRINT_FIELD_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"
SPRINT_PAGE_SIZE = 50
COMMENTS_PAGE_SIZE = 100
# the Agile API takes at most this many issues per sprint or backlog move
ISSUES_PER_MOVE = 50
SPRINT_STATES = {"future": SprintState.FUTURE, "active": SprintState.ACTIVE, "closed": SprintState.CLOSED}
# Jira's own statusCategory keys
CATEGORIES = {"new": StatusCategory.TODO, "indeterminate": StatusCategory.IN_PROGRESS, "done": StatusCategory.DONE}
DONE_NAMES = {"done", "closed", "resolved"}
TODO_NAMES = {"to do", "todo", "open", "backlog"}
# block nodes whose children are themselves blocks, so they sit on separate lines
ADF_BLOCKS = {"doc", "bulletList", "orderedList", "listItem", "blockquote", "panel", "table", "tableRow", "tableCell", "tableHeader", "expand"}


def status_category(status: dict) -> StatusCategory:
    key = (status.get("statusCategory") or {}).get("key")
    if key in CATEGORIES:
        return CATEGORIES[key]
    name = (status.get("name") or "").lower()
    if name in DONE_NAMES:
        return StatusCategory.DONE
    return StatusCategory.TODO if name in TODO_NAMES else StatusCategory.IN_PROGRESS


def adf_to_text(node: dict | None) -> str | None:
    if not node:
        return None
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return "\n"
    parts = [adf_to_text(child) or "" for child in node.get("content", [])]
    return ("\n" if node.get("type") in ADF_BLOCKS else "").join(parts)


def adf_to_markdown(node: dict | None) -> str | None:
    """Jira's rich text as Markdown, for showing a description or comment as written.

    Covers what Jira's editor produces day to day; media need Jira's auth, so they become a note.
    """
    if not node:
        return None
    return _md_blocks(node.get("content", [])).strip() or None


def _md_blocks(nodes: list[dict], indent: str = "", sep: str = "\n\n") -> str:
    return sep.join(block for block in (_md_block(n, indent) for n in nodes) if block)


def _md_block(node: dict, indent: str) -> str:
    kind, attrs, content = node.get("type"), node.get("attrs") or {}, node.get("content", [])
    if kind == "paragraph":
        return indent + _md_inline(content)
    if kind == "heading":
        return f"{indent}{'#' * min(int(attrs.get('level', 1)), 6)} {_md_inline(content)}"
    if kind in ("bulletList", "orderedList"):
        lines = []
        for number, item in enumerate(content, start=int(attrs.get("order", 1))):
            marker = f"{number}. " if kind == "orderedList" else "- "
            # a list item's blocks stay together, so the list stays tight
            body = _md_blocks(item.get("content", []), indent + "  ", "\n").lstrip()
            lines.append(f"{indent}{marker}{body}")
        return "\n".join(lines)
    if kind == "codeBlock":
        code = "".join(child.get("text", "") for child in content)
        return f"{indent}```{attrs.get('language') or ''}\n{code}\n{indent}```"
    if kind in ("blockquote", "panel"):
        return "\n".join(f"{indent}> {line}" if line else f"{indent}>" for line in _md_blocks(content).split("\n"))
    if kind == "rule":
        return f"{indent}---"
    if kind == "table":
        rows = [[_md_inline_blocks(cell.get("content", [])) for cell in row.get("content", [])] for row in content]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = [f"| {' | '.join(rows[0])} |", f"|{'---|' * width}", *(f"| {' | '.join(row)} |" for row in rows[1:])]
        return "\n".join(indent + line for line in lines)
    if kind in ("mediaSingle", "mediaGroup", "media"):
        return f"{indent}_[attachment in Jira]_"
    if kind in ("expand", "nestedExpand"):
        title = attrs.get("title")
        return (f"{indent}**{title}**\n\n" if title else "") + _md_blocks(content, indent)
    # anything else: keep its text
    return indent + (_md_inline(content) if content else "")


def _md_inline_blocks(nodes: list[dict]) -> str:
    """A table cell's blocks on one line."""
    return " ".join(_md_inline(n.get("content", [])) for n in nodes).replace("|", "\\|")


def _md_inline(nodes: list[dict]) -> str:
    parts = []
    for node in nodes:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text":
            parts.append(_md_marks(node.get("text", ""), node.get("marks", [])))
        elif kind == "hardBreak":
            parts.append("  \n")
        elif kind == "mention":
            parts.append(attrs.get("text") or "@someone")
        elif kind == "emoji":
            parts.append(attrs.get("text") or attrs.get("shortName") or "")
        elif kind in ("inlineCard", "blockCard"):
            parts.append(attrs.get("url") or "")
        elif kind == "status":
            parts.append(f"`{attrs.get('text', '')}`")
        elif kind == "date":
            try:
                parts.append(datetime.fromtimestamp(int(attrs.get("timestamp")) / 1000).strftime("%Y-%m-%d"))
            except (TypeError, ValueError):
                pass
        else:
            parts.append(_md_inline(node.get("content", [])))
    return "".join(parts)


def _md_marks(text: str, marks: list[dict]) -> str:
    kinds = {mark.get("type"): mark.get("attrs") or {} for mark in marks}
    if "code" in kinds:
        return f"`{text}`"
    if "strong" in kinds:
        text = f"**{text}**"
    if "em" in kinds:
        text = f"_{text}_"
    if "strike" in kinds:
        text = f"~~{text}~~"
    if "link" in kinds and kinds["link"].get("href"):
        text = f"[{text}]({kinds['link']['href']})"
    return text


def markdown_to_adf(md: str | None) -> dict | None:
    """Markdown as Jira's rich text: the inverse of `adf_to_markdown`, so an edit keeps its formatting.

    Covers what `adf_to_markdown` writes and what an editor produces: headings, lists, code, quotes,
    rules, tables, and bold, italic, strikethrough, code and links inline. A single line break stays
    a line break, so plain text keeps its lines.
    """
    if not md or not md.strip():
        return None
    return {"type": "doc", "version": 1, "content": _adf_blocks(md.replace("\r\n", "\n").split("\n"))}


_FENCE_RE = re.compile(r"^\s*```\s*([\w+#.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_INLINE_RE = re.compile(
    r"`([^`]+)`"
    r"|\[([^\]]+)\]\(([^)\s]+)\)"
    r"|\*\*(.+?)\*\*"
    r"|~~(.+?)~~"
    r"|(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?![\w*])"
    r"|(?<![\w_])_(?![\s_])(.+?)(?<!\s)_(?![\w_])"
    r"|\\([\\`*_\[\]()#+\-.!|~>])"
)


def _starts_block(line: str) -> bool:
    stripped = line.lstrip()
    return bool(
        _FENCE_RE.match(line) or _HEADING_RE.match(line) or _RULE_RE.match(line) or _LIST_RE.match(line) or stripped.startswith(">")
    )


def _dedent(lines: list[str]) -> list[str]:
    depth = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return [line[depth:] for line in lines]


def _adf_blocks(lines: list[str]) -> list[dict]:
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif fence := _FENCE_RE.match(line):
            code, i = [], i + 1
            while i < len(lines) and lines[i].strip() != "```":
                code.append(lines[i])
                i += 1
            block: dict = {"type": "codeBlock"}
            if fence.group(1):
                block["attrs"] = {"language": fence.group(1)}
            if code:
                block["content"] = [{"type": "text", "text": "\n".join(code)}]
            blocks.append(block)
            i += 1
        elif heading := _HEADING_RE.match(line):
            blocks.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": _adf_inline(heading.group(2))})
            i += 1
        elif _RULE_RE.match(line):
            blocks.append({"type": "rule"})
            i += 1
        elif line.lstrip().startswith(">"):
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                rest = lines[i].lstrip()[1:]
                quoted.append(rest[1:] if rest.startswith(" ") else rest)
                i += 1
            blocks.append({"type": "blockquote", "content": _adf_blocks(quoted) or [{"type": "paragraph"}]})
        elif "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            rows = [line]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            blocks.append(_adf_table(rows))
        elif _LIST_RE.match(line):
            block, i = _adf_list(lines, i)
            blocks.append(block)
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                paragraph.append(lines[i])
                i += 1
            blocks.append({"type": "paragraph", "content": _adf_lines(paragraph)})
    return blocks


def _adf_list(lines: list[str], i: int) -> tuple[dict, int]:
    first = _LIST_RE.match(lines[i])
    base, ordered = len(first.group(1)), first.group(2)[0].isdigit()
    items: list[dict] = []
    while i < len(lines):
        item = _LIST_RE.match(lines[i])
        if not item or len(item.group(1)) != base or item.group(2)[0].isdigit() != ordered:
            break
        children, i = [], i + 1
        while i < len(lines):
            line = lines[i]
            if line.strip() and len(line) - len(line.lstrip()) <= base:
                break
            if not line.strip():
                # a blank line ends the list unless more of it follows
                following = next((later for later in lines[i + 1:] if later.strip()), "")
                if len(following) - len(following.lstrip()) <= base and not _LIST_RE.match(following):
                    break
            children.append(line)
            i += 1
        content = _adf_blocks([item.group(3), *_dedent(children)]) or [{"type": "paragraph"}]
        items.append({"type": "listItem", "content": content})
        while i < len(lines) and not lines[i].strip() and _LIST_RE.match(next((later for later in lines[i:] if later.strip()), "")):
            i += 1
    block: dict = {"type": "orderedList" if ordered else "bulletList", "content": items}
    start = int(re.match(r"\d+", first.group(2)).group()) if ordered else 1
    if start != 1:
        block["attrs"] = {"order": start}
    return block, i


def _adf_table(rows: list[str]) -> dict:
    def cells(row: str) -> list[str]:
        row = row.strip()
        row = row[1:] if row.startswith("|") else row
        row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
        return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(row)]

    def cell(kind: str, text: str) -> dict:
        return {"type": kind, "content": [{"type": "paragraph", "content": _adf_inline(text)} if text else {"type": "paragraph"}]}

    header, *body = [cells(row) for row in rows]
    return {
        "type": "table",
        "content": [
            {"type": "tableRow", "content": [cell("tableHeader", text) for text in header]},
            *({"type": "tableRow", "content": [cell("tableCell", text) for text in row]} for row in body),
        ],
    }


def _adf_lines(lines: list[str]) -> list[dict]:
    """A paragraph's lines, joined by hard breaks (Markdown's `  ` or `\\` line ends included)."""
    content: list[dict] = []
    for index, line in enumerate(lines):
        if index:
            content.append({"type": "hardBreak"})
        text = line.strip()
        content.extend(_adf_inline(text[:-1].rstrip() if text.endswith("\\") else text))
    return content


def _adf_inline(text: str, marks: tuple[dict, ...] = ()) -> list[dict]:
    nodes: list[dict] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            nodes.append(_adf_text(text[position:match.start()], marks))
        code, label, href, strong, strike, star_em, underscore_em, escaped = match.groups()
        if code is not None:
            # Jira's code mark only combines with a link
            nodes.append(_adf_text(code, (*(mark for mark in marks if mark["type"] == "link"), {"type": "code"})))
        elif label is not None:
            nodes.extend(_adf_inline(label, (*marks, {"type": "link", "attrs": {"href": href}})))
        elif strong is not None:
            nodes.extend(_adf_inline(strong, (*marks, {"type": "strong"})))
        elif strike is not None:
            nodes.extend(_adf_inline(strike, (*marks, {"type": "strike"})))
        elif (star_em or underscore_em) is not None:
            nodes.extend(_adf_inline(star_em or underscore_em, (*marks, {"type": "em"})))
        else:
            nodes.append(_adf_text(escaped, marks))
        position = match.end()
    if position < len(text):
        nodes.append(_adf_text(text[position:], marks))
    # escapes split a run of text; Jira rejects empty text nodes
    merged: list[dict] = []
    for node in nodes:
        if not node["text"]:
            continue
        if merged and merged[-1].get("marks") == node.get("marks"):
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + node["text"]}
        else:
            merged.append(node)
    return merged


def _adf_text(text: str, marks: tuple[dict, ...]) -> dict:
    node: dict = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def column_id(name: str) -> str:
    """A tracker column's id: stable while its name is, so re-importing keeps items in place."""
    return "col_" + hashlib.sha1(name.strip().lower().encode()).hexdigest()[:10]


def has_agile(conn: dict) -> bool:
    return AGILE_SCOPE in conn.get("scopes", [])


def _datetime(timestamp: str | None) -> datetime | None:
    """Jira's timestamps, with or without milliseconds, `Z` or `+0000`."""
    if not timestamp:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(timestamp.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
    return None


def _jira_time(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000%z") if value else None


def sprint_fields(raw: dict) -> dict:
    """A Jira sprint, as an issue's sprint field or the Agile API has it, in `sprints` terms."""
    return {
        "external_id": str(raw["id"]),
        "name": raw.get("name") or f"Sprint {raw['id']}",
        "state": SPRINT_STATES.get(raw.get("state"), SprintState.FUTURE),
        "goal": raw.get("goal") or None,
        "start_at": _datetime(raw.get("startDate")),
        "end_at": _datetime(raw.get("endDate")),
        "completed_at": _datetime(raw.get("completeDate")),
    }


def current_sprint(entries: list[dict] | None) -> dict | None:
    """The sprint an issue is in now: an active one, else the next future one; None means the backlog."""
    open_ = [e for e in entries or [] if e.get("state") in ("active", "future")]
    open_.sort(key=lambda e: (e.get("state") != "active", e.get("startDate") or "", e.get("id") or 0))
    return sprint_fields(open_[0]) if open_ else None


def _epoch(timestamp: str | None) -> float:
    try:
        return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except (TypeError, ValueError):
        return utcnow().timestamp()


def _url(conn: dict, path: str) -> str:
    return f"{API_BASE}/{conn['external_account']['id']}{path}"


def _check(resp: httpx.Response, ok: set[int], error_code: str, message: str) -> httpx.Response:
    if resp.status_code in ok:
        return resp
    try:
        body = resp.json()
        detail = body.get("message") or "; ".join([*body.get("errorMessages", []), *body.get("errors", {}).values()])
    except (ValueError, AttributeError, TypeError):
        detail = None
    logger.error(error_code, status=resp.status_code, detail=detail)
    message = f"{message}: {detail}" if detail else message
    if resp.status_code == 403:
        raise ForbiddenError(message, error_code="jira_forbidden")
    if resp.status_code == 404:
        raise NotFoundError(message, error_code="jira_item_not_found")
    raise ExternalServiceError(message, error_code=error_code)


def _fields(fields: dict, board: dict | None = None) -> dict:
    """Board fields to Jira fields; only the ones present are changed."""
    out: dict = {}
    if "priority" in fields:
        out["priority"] = {"name": fields["priority"]} if fields["priority"] else None
    if "labels" in fields:
        out["labels"] = fields["labels"]
    if "due_date" in fields:
        out["duedate"] = fields["due_date"]
    story_points = ((board or {}).get("fields") or {}).get("story_points")
    if "story_points" in fields:
        if not story_points:
            raise ConflictError("this Jira project has no story points field", error_code="jira_no_story_points")
        out[story_points] = fields["story_points"]
    if "title" in fields:
        out["summary"] = fields["title"]
    if "description" in fields:
        out["description"] = markdown_to_adf(fields["description"])
    if "assignee_id" in fields:
        out["assignee"] = {"accountId": fields["assignee_id"]}
    return out


def _fields_for(board: dict | None) -> str:
    """The issue fields to read: the standard ones, plus the board's sprint and estimate fields once known."""
    known = (board or {}).get("fields") or {}
    return ",".join([FIELDS, *(known[name] for name in ("sprint", "story_points") if known.get(name))])


def _comment(raw: dict) -> dict:
    author = raw.get("author") or {}
    return {
        "external_id": str(raw["id"]),
        "author": {"id": author.get("accountId"), "name": author.get("displayName")},
        "body": adf_to_markdown(raw.get("body")) or "",
        "created_at": _datetime(raw.get("created")),
        "updated_at": _datetime(raw.get("updated")),
    }


def _linked(issue: dict) -> dict:
    """A subtask or linked issue, as much of it as Jira embeds."""
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    return {"key": issue.get("key"), "title": fields.get("summary"), "status": status.get("name"), "status_category": status_category(status)}


def _link(link: dict) -> dict | None:
    """One issue link from this issue's side: "blocks OVR-2", "is blocked by OVR-3"."""
    kind = link.get("type") or {}
    if link.get("outwardIssue"):
        return {"type": kind.get("outward") or kind.get("name"), **_linked(link["outwardIssue"])}
    if link.get("inwardIssue"):
        return {"type": kind.get("inward") or kind.get("name"), **_linked(link["inwardIssue"])}
    return None


def _sprint_body(fields: dict) -> dict:
    names = {"name": "name", "goal": "goal", "state": "state"}
    body = {jira: fields[key] for key, jira in names.items() if key in fields}
    if "start_at" in fields:
        body["startDate"] = _jira_time(fields["start_at"])
    if "end_at" in fields:
        body["endDate"] = _jira_time(fields["end_at"])
    return body


class JiraAdapter:
    async def resolve_account(self, access_token: str) -> dict:
        resp = await http.get(ACCESSIBLE_RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        log_call(logger, "jira_api_call", "GET", "/oauth/token/accessible-resources", resp)
        if resp.status_code != 200:
            raise ExternalServiceError("failed to fetch accessible Atlassian resources")
        sites = resp.json()
        if not sites:
            raise NotFoundError("no accessible Jira sites for this account", error_code="no_jira_sites")
        # one site per workspace: the first one the account can reach
        return {"id": sites[0]["id"], "name": sites[0].get("name"), "url": sites[0].get("url")}

    async def list_targets(self, auth: AuthBackend, conn: dict) -> list[dict]:
        projects: list[dict] = []
        while True:
            resp = await auth.request(
                conn, "GET", _url(conn, "/rest/api/3/project/search"),
                params={"startAt": len(projects), "maxResults": PROJECTS_PAGE_SIZE},
            )
            if resp.status_code != 200:
                raise ExternalServiceError("could not list Jira projects", error_code="jira_projects_unavailable")
            body = resp.json()
            page = body.get("values", [])
            projects.extend({"key": p["key"], "name": p.get("name"), "id": p.get("id")} for p in page)
            if body.get("isLast", True) or not page:
                return projects

    async def list_assignees(self, auth: AuthBackend, conn: dict, target_key: str) -> list[dict]:
        resp = _check(
            await auth.request(conn, "GET", _url(conn, "/rest/api/3/user/assignable/search"), params={"project": target_key, "maxResults": 100}),
            {200}, "jira_assignees_unavailable", "could not list Jira assignees",
        )
        return [
            {"id": u["accountId"], "name": u.get("displayName"), "avatar_url": (u.get("avatarUrls") or {}).get("24x24")}
            for u in resp.json()
        ]

    async def fetch_page(
        self, auth: AuthBackend, conn: dict, target_key: str, *, since_minutes: int | None, page_token: str | None,
        board: dict | None = None,
    ) -> Page:
        # relative JQL dates avoid Jira reading absolute ones in the user's timezone
        jql = f'project = "{target_key}"' + (f" AND updated >= -{since_minutes}m" if since_minutes else "")
        params = {"jql": jql, "maxResults": PAGE_SIZE, "fields": _fields_for(board)}
        if page_token:
            params["nextPageToken"] = page_token
        resp = _check(
            await auth.request(conn, "GET", _url(conn, "/rest/api/3/search/jql"), params=params),
            {200}, "jira_search_failed", "Jira ticket search failed",
        )
        body = resp.json()
        return Page(items=body.get("issues", []), next_token=body.get("nextPageToken"))

    def normalise(self, conn: dict, target_key: str, raw: dict, board: dict | None = None) -> dict:
        fields = raw.get("fields", {})
        board_fields = (board or {}).get("fields") or {}
        sprint_field, points_field = board_fields.get("sprint"), board_fields.get("story_points")
        reporter, parent = fields.get("reporter"), fields.get("parent")
        status = fields.get("status") or {}
        assignee = fields.get("assignee")
        return {
            "source": Provider.JIRA,
            "external_id": raw["id"],
            "key": raw["key"],
            "target_key": target_key,
            "title": fields.get("summary") or "",
            "description": adf_to_text(fields.get("description")),
            "description_md": adf_to_markdown(fields.get("description")),
            "reporter": reporter and {"id": reporter.get("accountId"), "name": reporter.get("displayName")},
            "labels": fields.get("labels") or [],
            "parent": parent and {"key": parent.get("key"), "title": (parent.get("fields") or {}).get("summary")},
            "due_date": fields.get("duedate"),
            "story_points": fields.get(points_field) if points_field else None,
            "subtasks": [_linked(sub) for sub in fields.get("subtasks") or []],
            "links": [link for link in map(_link, fields.get("issuelinks") or []) if link],
            "status": status.get("name"),
            "status_id": status.get("id"),
            "status_category": status_category(status),
            "assignee": assignee and {"id": assignee.get("accountId"), "name": assignee.get("displayName")},
            "priority": (fields.get("priority") or {}).get("name"),
            "issue_type": (fields.get("issuetype") or {}).get("name"),
            "external_url": f"{conn['external_account'].get('url')}/browse/{raw['key']}",
            "source_created_at": fields.get("created"),
            "source_updated_at": fields.get("updated"),
            "status_changed_at": _datetime(fields.get("statuscategorychangedate")),
            # only known when the board's sprint field is; None then means the backlog
            **({"tracker_sprint": current_sprint(fields.get(sprint_field))} if sprint_field else {}),
            "rank": _epoch(fields.get("created")),
            "raw": raw,
        }

    async def fetch(self, auth: AuthBackend, conn: dict, external_id: str, board: dict | None = None) -> dict:
        resp = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/api/3/issue/{external_id}"), params={"fields": _fields_for(board)}),
            {200}, "jira_issue_unavailable", "could not read the Jira issue",
        )
        return resp.json()

    async def create(self, auth: AuthBackend, conn: dict, target_key: str, fields: dict) -> str:
        project = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/api/3/project/{target_key}")),
            {200}, "jira_project_unavailable", "could not read the Jira project",
        ).json()
        types = [t for t in project.get("issueTypes", []) if not t.get("subtask")]
        if not types:
            raise ConflictError("the Jira project has no issue type to create", error_code="jira_no_issue_type")
        issue_type = next((t for t in types if t.get("name") == "Task"), types[0])
        body = {"fields": {"project": {"key": target_key}, "issuetype": {"id": issue_type["id"]}, **_fields(fields)}}
        resp = _check(
            await auth.request(conn, "POST", _url(conn, "/rest/api/3/issue"), json=body),
            {201}, "jira_create_failed", "could not create the Jira issue",
        )
        return resp.json()["id"]

    async def update(self, auth: AuthBackend, conn: dict, external_id: str, fields: dict, board: dict | None = None) -> None:
        _check(
            await auth.request(conn, "PUT", _url(conn, f"/rest/api/3/issue/{external_id}"), json={"fields": _fields(fields, board)}),
            {204}, "jira_update_failed", "could not update the Jira issue",
        )

    async def board_columns(self, auth: AuthBackend, conn: dict, target_key: str) -> list[dict]:
        """The project's board columns when Jira Software access was granted, else one column per status."""
        statuses = await self._project_statuses(auth, conn, target_key)
        category_of = {status["id"]: status["status_category"] for status in statuses}
        board = await self._board(auth, conn, target_key) if has_agile(conn) else None
        if board is None:
            return [
                {"id": column_id(s["name"]), "name": s["name"], "status_category": s["status_category"], "status_ids": [s["id"]]}
                for s in statuses
            ]
        config = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/agile/1.0/board/{board['id']}/configuration")),
            {200}, "jira_board_unavailable", "could not read the Jira board",
        ).json()
        columns = []
        for column in (config.get("columnConfig") or {}).get("columns", []):
            ids = [str(status["id"]) for status in column.get("statuses", []) if str(status.get("id")) in category_of]
            if ids:  # a column holding no status of this project never shows anything
                columns.append({
                    "id": column_id(column["name"]), "name": column["name"],
                    "status_category": category_of[ids[0]], "status_ids": ids,
                })
        return columns

    async def list_priorities(self, auth: AuthBackend, conn: dict) -> list[str]:
        resp = _check(
            await auth.request(conn, "GET", _url(conn, "/rest/api/3/priority")), {200}, "jira_priorities_unavailable", "could not read Jira's priorities",
        )
        return [priority["name"] for priority in resp.json() if priority.get("name")]

    async def list_comments(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """The issue's comments, oldest first, with their rich text as Markdown."""
        comments: list[dict] = []
        while True:
            body = _check(
                await auth.request(
                    conn, "GET", _url(conn, f"/rest/api/3/issue/{external_id}/comment"),
                    params={"orderBy": "created", "startAt": len(comments), "maxResults": COMMENTS_PAGE_SIZE},
                ),
                {200}, "jira_comments_unavailable", "could not read the Jira issue's comments",
            ).json()
            page = body.get("comments", [])
            comments.extend(map(_comment, page))
            if not page or len(comments) >= body.get("total", 0):
                return comments

    async def add_comment(self, auth: AuthBackend, conn: dict, external_id: str, text: str) -> dict:
        resp = _check(
            await auth.request(conn, "POST", _url(conn, f"/rest/api/3/issue/{external_id}/comment"), json={"body": markdown_to_adf(text)}),
            {201}, "jira_comment_failed", "could not add the Jira comment",
        )
        return _comment(resp.json())

    async def update_comment(self, auth: AuthBackend, conn: dict, external_id: str, comment_id: str, text: str) -> dict:
        resp = _check(
            await auth.request(conn, "PUT", _url(conn, f"/rest/api/3/issue/{external_id}/comment/{comment_id}"), json={"body": markdown_to_adf(text)}),
            {200}, "jira_comment_failed", "could not change the Jira comment",
        )
        return _comment(resp.json())

    async def delete_comment(self, auth: AuthBackend, conn: dict, external_id: str, comment_id: str) -> None:
        _check(
            await auth.request(conn, "DELETE", _url(conn, f"/rest/api/3/issue/{external_id}/comment/{comment_id}")),
            {204}, "jira_comment_failed", "could not delete the Jira comment",
        )

    async def board_details(self, auth: AuthBackend, conn: dict, target_key: str) -> dict:
        """The board's Jira id and kind (with Jira Software access), and the site's sprint field."""
        fields = _check(
            await auth.request(conn, "GET", _url(conn, "/rest/api/3/field")), {200}, "jira_fields_unavailable", "could not read Jira's fields",
        ).json()
        sprint = next((f["id"] for f in fields if (f.get("schema") or {}).get("custom") == SPRINT_FIELD_TYPE), None)
        story_points = next((f["id"] for f in fields if (f.get("name") or "").lower() in STORY_POINT_NAMES), None)
        board = await self._board(auth, conn, target_key) if has_agile(conn) else None
        kind = None if board is None else BoardKind.SCRUM if board.get("type") == "scrum" else BoardKind.KANBAN
        return {"external_id": board and str(board["id"]), "kind": kind, "fields": {"sprint": sprint, "story_points": story_points}}

    async def list_sprints(self, auth: AuthBackend, conn: dict, board_external_id: str) -> list[dict]:
        """The board's active and future sprints."""
        sprints: list[dict] = []
        while True:
            resp = _check(
                await auth.request(
                    conn, "GET", _url(conn, f"/rest/agile/1.0/board/{board_external_id}/sprint"),
                    params={"state": "active,future", "startAt": len(sprints), "maxResults": SPRINT_PAGE_SIZE},
                ),
                {200}, "jira_sprints_unavailable", "could not read the Jira board's sprints",
            ).json()
            sprints.extend(sprint_fields(raw) for raw in resp.get("values", []))
            if resp.get("isLast", True) or not resp.get("values"):
                return sprints

    async def set_sprint(self, auth: AuthBackend, conn: dict, external_ids: list[str], sprint_external_id: str | None) -> None:
        """Move issues into a sprint, or to the backlog when it's None."""
        path = f"/rest/agile/1.0/sprint/{sprint_external_id}/issue" if sprint_external_id else "/rest/agile/1.0/backlog/issue"
        for start in range(0, len(external_ids), ISSUES_PER_MOVE):
            _check(
                await auth.request(conn, "POST", _url(conn, path), json={"issues": external_ids[start:start + ISSUES_PER_MOVE]}),
                {204}, "jira_sprint_move_failed", "could not move the Jira issues",
            )

    async def create_sprint(self, auth: AuthBackend, conn: dict, board_external_id: str, fields: dict) -> dict:
        body = {"originBoardId": int(board_external_id), **_sprint_body(fields)}
        resp = _check(
            await auth.request(conn, "POST", _url(conn, "/rest/agile/1.0/sprint"), json=body),
            {201}, "jira_sprint_create_failed", "could not create the Jira sprint",
        )
        return sprint_fields(resp.json())

    async def update_sprint(self, auth: AuthBackend, conn: dict, sprint_external_id: str, fields: dict) -> dict:
        """Change any of {name, goal, start_at, end_at, state}; a partial update, so only those are sent."""
        resp = _check(
            await auth.request(conn, "POST", _url(conn, f"/rest/agile/1.0/sprint/{sprint_external_id}"), json=_sprint_body(fields)),
            {200}, "jira_sprint_update_failed", "could not update the Jira sprint",
        )
        return sprint_fields(resp.json())

    async def _project_statuses(self, auth: AuthBackend, conn: dict, target_key: str) -> list[dict]:
        """Every status the project's issue types use, once each, to do -> in progress -> done."""
        per_type = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/api/3/project/{target_key}/statuses")),
            {200}, "jira_statuses_unavailable", "could not read the Jira project's statuses",
        ).json()
        found: dict[str, dict] = {}
        for issue_type in per_type:
            for status in issue_type.get("statuses", []):
                found.setdefault(str(status["id"]), {"id": str(status["id"]), "name": status.get("name") or "", "status_category": status_category(status)})
        order = list(StatusCategory)
        return sorted(found.values(), key=lambda status: order.index(status["status_category"]))

    async def _board(self, auth: AuthBackend, conn: dict, target_key: str) -> dict | None:
        """The project's board: its first scrum board, else its first board."""
        resp = _check(
            await auth.request(conn, "GET", _url(conn, "/rest/agile/1.0/board"), params={"projectKeyOrId": target_key}),
            {200}, "jira_boards_unavailable", "could not list the Jira project's boards",
        )
        boards = resp.json().get("values", [])
        return next((b for b in boards if b.get("type") == "scrum"), boards[0] if boards else None)

    async def move(self, auth: AuthBackend, conn: dict, external_id: str, column: dict) -> None:
        url = _url(conn, f"/rest/api/3/issue/{external_id}/transitions")
        transitions = _check(
            await auth.request(conn, "GET", url), {200}, "jira_transitions_unavailable", "could not read Jira transitions",
        ).json().get("transitions", [])
        if column["status_ids"]:
            # the column's statuses in their order: the first one a transition reaches
            reachable = {str((t.get("to") or {}).get("id")): t for t in transitions}
            transition = next((reachable[s] for s in column["status_ids"] if s in reachable), None)
        else:
            transition = next((t for t in transitions if status_category(t.get("to") or {}) == column["status_category"]), None)
        if transition is None:
            raise ConflictError(
                "the Jira workflow has no transition into that column from the issue's status",
                error_code="jira_transition_unavailable",
            )
        _check(
            await auth.request(conn, "POST", url, json={"transition": {"id": transition["id"]}}),
            {204}, "jira_transition_failed", "could not move the Jira issue",
        )

    async def delete(self, auth: AuthBackend, conn: dict, external_id: str) -> None:
        _check(
            await auth.request(conn, "DELETE", _url(conn, f"/rest/api/3/issue/{external_id}")),
            {204}, "jira_delete_failed", "could not delete the Jira issue",
        )
