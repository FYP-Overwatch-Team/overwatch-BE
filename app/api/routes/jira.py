import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.integrations.jira_client import get_jira_client
from app.services import jira_service
from app.workers import jobs

router = APIRouter(prefix="/jira", tags=["jira"])

STATE_COOKIE = "overwatch_jira_state"


class ConnectProjectRequest(BaseModel):
    cloud_id: str
    project_key: str


@router.get("/login")
async def jira_login(user_id: str = Depends(get_current_user_id)) -> RedirectResponse:
    # Encode user id into state alongside CSRF nonce: Atlassian's callback carries no auth.
    state = f"{user_id}.{secrets.token_urlsafe(24)}"
    response = RedirectResponse(get_jira_client().authorize_url(state))
    response.set_cookie(
        STATE_COOKIE, state, httponly=True,
        secure=get_settings().app_env != "dev", samesite="lax", path="/jira", max_age=600,
    )
    return response


@router.get("/callback")
async def jira_callback(request: Request, code: str, state: str = ""):
    expected_state = request.cookies.get(STATE_COOKIE)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise UnauthorizedError("OAuth state mismatch", error_code="oauth_state_mismatch")
    user_id = state.split(".", 1)[0]

    site = await jira_service.complete_oauth(user_id, code)

    response = RedirectResponse(f"{get_settings().frontend_origin}/onboarding?jira=connected")
    response.delete_cookie(STATE_COOKIE, path="/jira")
    return response


@router.get("/projects")
async def list_projects(user_id: str = Depends(get_current_user_id)) -> dict:
    return {"projects": await jira_service.list_projects(user_id)}


@router.post("/projects/connect", status_code=201)
async def connect_project(
    body: ConnectProjectRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    doc = await jira_service.connect_project(user_id, body.cloud_id, body.project_key)
    jobs.enqueue_ticket_sync(background_tasks, user_id, body.project_key)
    return {
        "project_key": doc["project_key"],
        "cloud_id": doc["cloud_id"],
        "sync_status": doc["sync_status"],
    }
