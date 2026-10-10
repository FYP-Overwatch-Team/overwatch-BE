import secrets

from fastapi import BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import get_current_user_id
from app.core.config import get_settings
from app.core.cookies import cookie_samesite, cookie_secure
from app.core.exceptions import AppError
from app.core.responses import api_router
from app.integrations.jira_client import get_jira_client
from app.jira import service
from app.jira.schemas import AuthorizationUrl, ConnectedProject, ConnectProjectRequest, JiraProject
from app.workers import jobs

router = api_router(prefix="/jira", tags=["jira"])

STATE_COOKIE = "overwatch_jira_state"
COOKIE_PATH = "/jira"


@router.post("/login")
async def jira_login(response: Response, user_id: str = Depends(get_current_user_id)) -> AuthorizationUrl:
    # Atlassian's callback carries no bearer token, so the user is bound to a
    # short-lived httpOnly state cookie before the browser leaves the frontend
    state = f"{user_id}.{secrets.token_urlsafe(24)}"
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
    if not expected_state or not secrets.compare_digest(state, expected_state):
        outcome = "error"
    elif error or not code:
        outcome = "denied"
    else:
        try:
            await service.complete_oauth(state.split(".", 1)[0], code)
            outcome = "connected"
        except AppError:
            outcome = "error"

    response = RedirectResponse(f"{get_settings().frontend_origin}/onboarding/jira?jira={outcome}")
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return response


@router.delete("/connection")
async def disconnect_jira(user_id: str = Depends(get_current_user_id)) -> None:
    await service.disconnect(user_id)


@router.get("/projects")
async def list_projects(user_id: str = Depends(get_current_user_id)) -> list[JiraProject]:
    return await service.list_projects(user_id)


@router.post("/projects/connect", status_code=201)
async def connect_project(
    body: ConnectProjectRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user_id),
) -> ConnectedProject:
    project = await service.connect_project(user_id, body.project_key)
    background_tasks.add_task(jobs.run_ticket_sync, user_id, body.project_key)
    return project
