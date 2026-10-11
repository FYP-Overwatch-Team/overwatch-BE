import secrets

from fastapi import BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import workspace_member
from app.core.config import get_settings
from app.core.cookies import cookie_samesite, cookie_secure
from app.core.exceptions import AppError
from app.core.responses import api_router
from app.integrations.jira_client import get_jira_client
from app.jira import service
from app.jira.schemas import AuthorizationUrl, ConnectedProject, ConnectProjectRequest, JiraProject
from app.workers import jobs
from app.workspaces.service import require_member

router = api_router(prefix="/jira", tags=["jira"])

STATE_COOKIE = "overwatch_jira_state"
COOKIE_PATH = "/jira"


@router.post("/login")
async def jira_login(response: Response, member: dict = Depends(workspace_member("admin"))) -> AuthorizationUrl:
    # Atlassian's callback carries no bearer token, so the user and workspace are bound
    # to a short-lived httpOnly state cookie before the browser leaves the frontend
    state = f"{member['user_id']}.{member['workspace_id']}.{secrets.token_urlsafe(24)}"
    response.set_cookie(
        STATE_COOKIE, state, httponly=True, secure=cookie_secure(), samesite=cookie_samesite(),
        path=COOKIE_PATH, max_age=600,
    )
    return AuthorizationUrl(authorization_url=get_jira_client().authorize_url(state))


@router.get("/callback")
async def jira_callback(
    request: Request, state: str = "", code: str | None = None, error: str | None = None,
) -> RedirectResponse:
    """Always lands back on the onboarding Jira step with ?jira=connected|denied|error."""
    expected_state = request.cookies.get(STATE_COOKIE)
    user_id, _, rest = state.partition(".")
    workspace_id = rest.partition(".")[0]
    if not expected_state or not secrets.compare_digest(state, expected_state):
        outcome = "error"
    elif error or not code:
        outcome = "denied"
    else:
        try:
            # they may have lost the admin role during the round trip
            await require_member(workspace_id, user_id, "admin")
            await service.complete_oauth(workspace_id, code)
            outcome = "connected"
        except AppError:
            outcome = "error"

    query = f"jira={outcome}" + (f"&w={workspace_id}" if outcome != "error" else "")
    response = RedirectResponse(f"{get_settings().frontend_origin}/onboarding/jira?{query}")
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return response


@router.delete("/connection")
async def disconnect_jira(member: dict = Depends(workspace_member("admin"))) -> None:
    await service.disconnect(member["workspace_id"])


@router.get("/projects")
async def list_projects(member: dict = Depends(workspace_member())) -> list[JiraProject]:
    return await service.list_projects(member["workspace_id"])


@router.post("/projects/connect", status_code=201)
async def connect_project(
    body: ConnectProjectRequest, background_tasks: BackgroundTasks, member: dict = Depends(workspace_member("admin")),
) -> ConnectedProject:
    project = await service.connect_project(member["workspace_id"], body.project_key)
    background_tasks.add_task(jobs.run_ticket_sync, member["workspace_id"], body.project_key)
    return project
