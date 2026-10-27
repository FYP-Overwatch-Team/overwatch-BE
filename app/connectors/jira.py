"""Jira Cloud connector: issues mirrored into `work_items`, and board changes written back."""

import hashlib
from datetime import datetime

import httpx
import structlog

from app.connectors import atlassian
from app.connectors.adf import adf_to_markdown as _adf_to_markdown, adf_to_text, markdown_to_adf
from app.connectors.ports import AuthBackend, Page
from app.core.constants import BoardKind, Provider, SprintState, StatusCategory
from app.core.exceptions import ConflictError, ExternalServiceError, ForbiddenError, NotFoundError
from app.core.time import utcnow

logger = structlog.get_logger("app.connectors.jira")

API_BASE = "https://api.atlassian.com/ex/jira"
WRITE_SCOPE = "write:jira-work"
# Jira Software (boards, their columns, sprints and the backlog) only accepts granular scopes
AGILE_SCOPES = (
    "read:board-scope:jira-software",
    "write:board-scope:jira-software",
    "read:board-scope.admin:jira-software",
    "read:sprint:jira-software",
    "write:sprint:jira-software",
    "read:issue:jira-software",
    # finding a project's board, and reading its columns, also need the project read
    "read:project:jira",
)
AGILE_SCOPE = "read:board-scope.admin:jira-software"

# reading a project's workflows (every status and the moves between them), for drag hints
WORKFLOW_SCOPE = "read:workflow:jira"
# classic platform scopes plus Jira Software's and the workflow read
SCOPES = ("read:jira-work", WRITE_SCOPE, "read:jira-user", *AGILE_SCOPES, WORKFLOW_SCOPE)

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
# the same categories as the workflows API names them
WORKFLOW_CATEGORIES = {"TODO": StatusCategory.TODO, "IN_PROGRESS": StatusCategory.IN_PROGRESS, "DONE": StatusCategory.DONE}
DONE_NAMES = {"done", "closed", "resolved"}
TODO_NAMES = {"to do", "todo", "open", "backlog"}


def adf_to_markdown(node: dict | None) -> str | None:
    return _adf_to_markdown(node, "attachment in Jira")


def status_category(status: dict) -> StatusCategory:
    key = (status.get("statusCategory") or {}).get("key")
    if key in CATEGORIES:
        return CATEGORIES[key]
    name = (status.get("name") or "").lower()
    if name in DONE_NAMES:
        return StatusCategory.DONE
    return StatusCategory.TODO if name in TODO_NAMES else StatusCategory.IN_PROGRESS


def column_id(name: str) -> str:
    """A tracker column's id: stable while its name is, so re-importing keeps items in place."""
    return "col_" + hashlib.sha1(name.strip().lower().encode()).hexdigest()[:10]


def has_agile(conn: dict) -> bool:
    return AGILE_SCOPE in conn.get("scopes", [])


def has_workflow(conn: dict) -> bool:
    return WORKFLOW_SCOPE in conn.get("scopes", [])


def _workflow(raw: dict, statuses: dict[str, dict]) -> dict:
    """One Jira workflow's moves: from any of `from` (None: any status, a global transition) to `to`."""
    by_reference = {ref: status["id"] for ref, status in statuses.items()}
    transitions = []
    for transition in raw.get("transitions") or []:
        kind, to = transition.get("type"), by_reference.get(transition.get("toStatusReference"))
        if kind == "INITIAL" or not to:
            continue  # creating an issue isn't a move
        sources = [by_reference[link["fromStatusReference"]] for link in transition.get("links") or [] if link.get("fromStatusReference") in by_reference]
        transitions.append({"id": str(transition.get("id")), "name": transition.get("name") or "", "from": None if kind == "GLOBAL" else sources, "to": to})
    return {"name": raw.get("name"), "transitions": transitions}


def _reachable(transitions: list[dict]) -> list[dict]:
    """The statuses an issue's available transitions lead to: [{status_id, status, status_category, transition_id, transition_name}]."""
    found = []
    for transition in transitions:
        to = transition.get("to") or {}
        found.append({
            "status_id": str(to["id"]) if to.get("id") is not None else None, "status": to.get("name"), "status_category": status_category(to),
            "transition_id": str(transition["id"]), "transition_name": transition.get("name") or "",
        })
    return found


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
    if "issue_type_id" in fields:
        out["issuetype"] = {"id": fields["issue_type_id"]}
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
    async def resolve_account(self, access_token: str, known_site_id: str | None = None) -> dict:
        return await atlassian.pick_site(access_token, known_site_id, "jira", "no_jira_sites")

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
            "issue_type_id": (fields.get("issuetype") or {}).get("id"),
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
        # the type asked for when the project has it, else Task, else its first
        wanted = (fields.get("issue_type") or "").lower()
        issue_type = next((t for t in types if (t.get("name") or "").lower() == wanted), None)
        issue_type = issue_type or next((t for t in types if t.get("name") == "Task"), types[0])
        fields = {k: v for k, v in fields.items() if k != "issue_type"}
        body = {"fields": {"project": {"key": target_key}, "issuetype": {"id": issue_type["id"]}, **_fields(fields)}}
        resp = _check(
            await auth.request(conn, "POST", _url(conn, "/rest/api/3/issue"), json=body),
            {201}, "jira_create_failed", "could not create the Jira issue",
        )
        return resp.json()["id"]

    async def allowed_types(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """The issue types this issue can be changed to in place: [{id, name}] (others need Jira's "Move")."""
        resp = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/api/3/issue/{external_id}/editmeta")),
            {200}, "jira_issue_unavailable", "could not read the Jira issue",
        )
        allowed = ((resp.json().get("fields") or {}).get("issuetype") or {}).get("allowedValues") or []
        return [{"id": str(t["id"]), "name": t.get("name")} for t in allowed]

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

    async def allowed_statuses(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """Where the issue can move now, as Jira decides for the connected account (conditions included)."""
        url = _url(conn, f"/rest/api/3/issue/{external_id}/transitions")
        return _reachable(_check(
            await auth.request(conn, "GET", url), {200}, "jira_transitions_unavailable", "could not read Jira transitions",
        ).json().get("transitions", []))

    async def workflow(self, auth: AuthBackend, conn: dict, target_key: str) -> dict | None:
        """The project's workflows, one per issue type: every move between its statuses, with its name.

        None when the grant can't read workflows (or Jira won't show them to this account); hints then
        come from asking Jira per issue.
        """
        if not has_workflow(conn):
            return None
        project = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/api/3/project/{target_key}")),
            {200}, "jira_project_unavailable", "could not read the Jira project",
        ).json()
        statuses: dict[str, dict] = {}  # by status reference
        workflows: dict[str, dict] = {}
        by_issue_type: dict[str, str] = {}
        for issue_type in project.get("issueTypes") or []:
            resp = await auth.request(conn, "POST", _url(conn, "/rest/api/3/workflows"), json={
                "projectAndIssueTypes": [{"projectId": str(project["id"]), "issueTypeId": str(issue_type["id"])}],
            })
            if resp.status_code in (401, 403):
                return None
            body = _check(resp, {200}, "jira_workflows_unavailable", "could not read the Jira workflows").json()
            for status in body.get("statuses") or []:
                statuses[status["statusReference"]] = {
                    "id": str(status["id"]), "name": status.get("name") or "",
                    "category": WORKFLOW_CATEGORIES.get(status.get("statusCategory"), StatusCategory.TODO),
                }
            for raw in body.get("workflows") or []:
                workflows.setdefault(str(raw["id"]), raw)
                by_issue_type[str(issue_type["id"])] = str(raw["id"])
        return {
            "statuses": {status["id"]: {"name": status["name"], "category": status["category"]} for status in statuses.values()},
            "workflows": {wf_id: _workflow(raw, statuses) for wf_id, raw in workflows.items()},
            "by_issue_type": by_issue_type,
        }

    async def move(self, auth: AuthBackend, conn: dict, external_id: str, column: dict, status_id: str | None = None) -> None:
        """Take the transition into the column: into `status_id` when given (one of its statuses), else its first reachable one."""
        reachable = await self.allowed_statuses(auth, conn, external_id)
        if column["status_ids"]:
            by_status = {r["status_id"]: r for r in reachable}
            wanted = [status_id] if status_id in column["status_ids"] else column["status_ids"]
            target = next((by_status[s] for s in wanted if s in by_status), None)
        else:
            target = next((r for r in reachable if r["status_category"] == column["status_category"]), None)
        if target is None:
            allowed = ", ".join(dict.fromkeys(r["status"] for r in reachable if r["status"])) or "nowhere"
            raise ConflictError(
                f"Jira's workflow doesn't allow that move. From here it allows: {allowed}.", error_code="transition_not_allowed",
            )
        _check(
            await auth.request(conn, "POST", _url(conn, f"/rest/api/3/issue/{external_id}/transitions"), json={"transition": {"id": target["transition_id"]}}),
            {204}, "jira_transition_failed", "could not move the Jira issue",
        )

    async def delete(self, auth: AuthBackend, conn: dict, external_id: str) -> None:
        _check(
            await auth.request(conn, "DELETE", _url(conn, f"/rest/api/3/issue/{external_id}")),
            {204}, "jira_delete_failed", "could not delete the Jira issue",
        )
