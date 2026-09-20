import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.cookies import cookie_samesite, cookie_secure
from app.api.deps import get_current_user_id
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.integrations.jira_client import get_jira_client
from app.services import jira_service
from app.workers import jobs

router = APIRouter(prefix="/jira", tags=["jira"])

STATE_COOKIE = "overwatch_jira_state"


class ConnectProjectRequest(BaseModel):
    project_key: str


@router.post("/login")
async def jira_login(
    response: Response,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    # Atlassian's callback carries no bearer token, so bind the user to a
    # short-lived, HttpOnly CSRF state cookie before leaving the frontend.
    state = f"{user_id}.{secrets.token_urlsafe(24)}"
    response.set_cookie(
        STATE_COOKIE, state, httponly=True,
        secure=cookie_secure(), samesite=cookie_samesite(), path="/jira", max_age=600,
    )
    return {"authorization_url": get_jira_client().authorize_url(state)}


@router.get("/callback")
async def jira_callback(
    request: Request,
    state: str = "",
    code: str | None = None,
    error: str | None = None,
):
    expected_state = request.cookies.get(STATE_COOKIE)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise UnauthorizedError("OAuth state mismatch", error_code="oauth_state_mismatch")

    frontend_url = f"{get_settings().frontend_origin}/connect"
    if error or not code:
        response = RedirectResponse(f"{frontend_url}?jira=denied")
    else:
        user_id = state.split(".", 1)[0]
        await jira_service.complete_oauth(user_id, code)
        response = RedirectResponse(f"{frontend_url}?jira=connected")

    response.delete_cookie(STATE_COOKIE, path="/jira")
    return response


@router.delete("/connection")
async def disconnect_jira(user_id: str = Depends(get_current_user_id)) -> dict:
    await jira_service.disconnect(user_id)
    return {"status": "disconnected"}


@router.get("/projects")
async def list_projects(user_id: str = Depends(get_current_user_id)) -> dict:
    return {"projects": await jira_service.list_projects(user_id)}


@router.post("/projects/connect", status_code=201)
async def connect_project(
    body: ConnectProjectRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    doc = await jira_service.connect_project(user_id, body.project_key)
    jobs.enqueue_ticket_sync(background_tasks, user_id, body.project_key)
    return {
        "project_key": doc["project_key"],
        "cloud_id": doc["cloud_id"],
        "sync_status": doc["sync_status"],
    }
