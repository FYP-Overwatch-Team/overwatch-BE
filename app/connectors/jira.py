"""Jira Cloud connector: issues mirrored into `work_items`, and board changes written back."""

from datetime import datetime

import httpx
import structlog

from app.connectors.oauth import OAuthConfig, http
from app.connectors.ports import AuthBackend, Page
from app.core.constants import Provider, StatusCategory
from app.core.exceptions import ConflictError, ExternalServiceError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors.jira")

API_BASE = "https://api.atlassian.com/ex/jira"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
WRITE_SCOPE = "write:jira-work"

# Classic 3LO scopes; offline_access is what returns a (rotating) refresh token.
OAUTH = OAuthConfig(
    provider=Provider.JIRA,
    authorize_url="https://auth.atlassian.com/authorize",
    token_url="https://auth.atlassian.com/oauth/token",
    scopes=("read:jira-work", WRITE_SCOPE, "read:jira-user", "offline_access"),
    client_id_setting="jira_client_id",
    client_secret_setting="jira_client_secret",
    redirect_path="/jira/callback",
    extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
)

PAGE_SIZE = 100
PROJECTS_PAGE_SIZE = 50
FIELDS = "summary,description,status,assignee,priority,issuetype,created,updated"
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


def text_to_adf(text: str | None) -> dict | None:
    if not text:
        return None
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else {"type": "paragraph"}
        for line in text.split("\n")
    ]
    return {"type": "doc", "version": 1, "content": paragraphs}


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


def _fields(fields: dict) -> dict:
    """Board fields to Jira fields; only the ones present are changed."""
    out: dict = {}
    if "title" in fields:
        out["summary"] = fields["title"]
    if "description" in fields:
        out["description"] = text_to_adf(fields["description"])
    if "assignee_id" in fields:
        out["assignee"] = {"accountId": fields["assignee_id"]}
    return out


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
    ) -> Page:
        # relative JQL dates avoid Jira reading absolute ones in the user's timezone
        jql = f'project = "{target_key}"' + (f" AND updated >= -{since_minutes}m" if since_minutes else "")
        params = {"jql": jql, "maxResults": PAGE_SIZE, "fields": FIELDS}
        if page_token:
            params["nextPageToken"] = page_token
        resp = _check(
            await auth.request(conn, "GET", _url(conn, "/rest/api/3/search/jql"), params=params),
            {200}, "jira_search_failed", "Jira ticket search failed",
        )
        body = resp.json()
        return Page(items=body.get("issues", []), next_token=body.get("nextPageToken"))

    def normalise(self, conn: dict, target_key: str, raw: dict) -> dict:
        fields = raw.get("fields", {})
        status = fields.get("status") or {}
        assignee = fields.get("assignee")
        return {
            "source": Provider.JIRA,
            "external_id": raw["id"],
            "key": raw["key"],
            "target_key": target_key,
            "title": fields.get("summary") or "",
            "description": adf_to_text(fields.get("description")),
            "status": status.get("name"),
            "status_category": status_category(status),
            "assignee": assignee and {"id": assignee.get("accountId"), "name": assignee.get("displayName")},
            "priority": (fields.get("priority") or {}).get("name"),
            "issue_type": (fields.get("issuetype") or {}).get("name"),
            "external_url": f"{conn['external_account'].get('url')}/browse/{raw['key']}",
            "source_created_at": fields.get("created"),
            "source_updated_at": fields.get("updated"),
            "rank": _epoch(fields.get("created")),
            "raw": raw,
        }

    async def fetch(self, auth: AuthBackend, conn: dict, external_id: str) -> dict:
        resp = _check(
            await auth.request(conn, "GET", _url(conn, f"/rest/api/3/issue/{external_id}"), params={"fields": FIELDS}),
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

    async def update(self, auth: AuthBackend, conn: dict, external_id: str, fields: dict) -> None:
        _check(
            await auth.request(conn, "PUT", _url(conn, f"/rest/api/3/issue/{external_id}"), json={"fields": _fields(fields)}),
            {204}, "jira_update_failed", "could not update the Jira issue",
        )

    async def move(self, auth: AuthBackend, conn: dict, external_id: str, category: StatusCategory) -> None:
        url = _url(conn, f"/rest/api/3/issue/{external_id}/transitions")
        transitions = _check(
            await auth.request(conn, "GET", url), {200}, "jira_transitions_unavailable", "could not read Jira transitions",
        ).json().get("transitions", [])
        transition = next((t for t in transitions if status_category(t.get("to") or {}) == category), None)
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
