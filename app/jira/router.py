"""The original /jira routes, kept as aliases over the generic connector API.

/jira/callback stays for good: it's the redirect URI registered with Atlassian, for Jira and Confluence alike.
The rest go once the frontend uses /connections.
"""

from fastapi import BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import workspace_member
from app.connectors import service
from app.connectors.router import oauth_callback, set_state_cookie
from app.connectors.schemas import AuthorizationUrl, Target
from app.core.constants import CredentialGroup, Provider, Role
from app.core.responses import api_router
from app.jira.schemas import ConnectedProject, ConnectProjectRequest
from app.workers import jobs

router = api_router(prefix="/jira", tags=["jira"])


@router.post("/login")
async def jira_login(response: Response, member: dict = Depends(workspace_member(Role.ADMIN))) -> AuthorizationUrl:
    url, state = await service.start_authorization(member, Provider.JIRA, "onboarding")
    set_state_cookie(response, Provider.JIRA, state)
    return AuthorizationUrl(authorization_url=url)


@router.get("/callback")
async def jira_callback(
    request: Request, state: str = "", code: str | None = None, error: str | None = None,
) -> RedirectResponse:
    return await oauth_callback(CredentialGroup.ATLASSIAN, request, state, code, error)


@router.delete("/connection")
async def disconnect_jira(member: dict = Depends(workspace_member(Role.ADMIN))) -> None:
    await service.disconnect(member["workspace_id"], Provider.JIRA)


@router.get("/projects")
async def list_projects(member: dict = Depends(workspace_member())) -> list[Target]:
    return await service.list_available_targets(member["workspace_id"], Provider.JIRA)


@router.post("/projects/connect", status_code=201)
async def connect_project(
    body: ConnectProjectRequest, background_tasks: BackgroundTasks, member: dict = Depends(workspace_member(Role.ADMIN)),
) -> ConnectedProject:
    target = await service.add_target(member["workspace_id"], Provider.JIRA, body.project_key, None)
    conn = await service.require_connection(member["workspace_id"], Provider.JIRA)
    background_tasks.add_task(jobs.run_connector_sync, member["workspace_id"], Provider.JIRA, body.project_key)
    return ConnectedProject(project_key=target["key"], cloud_id=conn["external_account"]["id"], sync_status=target["sync_status"])
