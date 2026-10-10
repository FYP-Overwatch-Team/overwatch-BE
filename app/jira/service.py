from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError, UnauthorizedError
from app.db import oauth_tokens
from app.integrations.jira_client import get_jira_client
from app.jira import repository

PROJECTS_PAGE_SIZE = 50


async def access_token(user_id: str) -> tuple[str, str]:
    """(access_token, cloud_id), refreshed first if it's about to expire."""
    token = await oauth_tokens.get_fresh_access_token(user_id, "jira", get_jira_client().refresh_tokens)
    doc = await oauth_tokens.get(user_id, "jira")
    if not doc.get("cloud_id"):
        raise UnauthorizedError("Jira connection has no selected site", error_code="jira_needs_reauth")
    return token, doc["cloud_id"]


async def complete_oauth(user_id: str, code: str) -> None:
    client = get_jira_client()
    tokens = await client.exchange_code(code)
    sites = await client.get_accessible_resources(tokens["access_token"])
    if not sites:
        raise NotFoundError("no accessible Jira sites for this account", error_code="no_jira_sites")
    # one site per user for now: the first one the account can reach
    await oauth_tokens.save(user_id, "jira", tokens, cloud_id=sites[0]["id"])


async def list_projects(user_id: str) -> list[dict]:
    token, cloud_id = await access_token(user_id)
    projects: list[dict] = []
    while True:
        resp = await get_jira_client().api_get(
            token, cloud_id, "/rest/api/3/project/search", startAt=len(projects), maxResults=PROJECTS_PAGE_SIZE,
        )
        if resp.status_code != 200:
            raise ExternalServiceError("could not list Jira projects", error_code="jira_projects_unavailable")
        body = resp.json()
        page = body.get("values", [])
        projects.extend({"key": p["key"], "name": p.get("name"), "id": p.get("id")} for p in page)
        if body.get("isLast", True) or not page:
            return projects


async def connect_project(user_id: str, project_key: str) -> dict:
    if await repository.find_active(user_id, project_key):
        raise ConflictError("project already connected", error_code="project_already_connected")
    _, cloud_id = await access_token(user_id)
    return await repository.connect(user_id, project_key, cloud_id)


async def disconnect(user_id: str) -> None:
    await oauth_tokens.set_status(user_id, "jira", "disconnected")
    await repository.disconnect_all(user_id)
