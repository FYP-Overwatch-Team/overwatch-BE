import secrets

from fastapi import BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import workspace_member
from app.connectors import registry, service
from app.connectors.schemas import (
    AddTargetRequest,
    Assignee,
    AuthorizationUrl,
    AuthorizeRequest,
    Connection,
    SyncTarget,
    Target,
)
from app.core.config import get_settings
from app.core.constants import Role
from app.core.cookies import cookie_samesite, cookie_secure
from app.core.exceptions import AppError
from app.core.responses import api_router
from app.workers import jobs

router = api_router(prefix="/connections", tags=["connections"])

LANDING = {"onboarding": "/onboarding/{provider}", "settings": "/settings/integrations"}


def _state_cookie(credential: str) -> str:
    return f"overwatch_{credential}_state"


def set_state_cookie(response: Response, provider: str, state: str) -> None:
    # the provider's callback carries no bearer token, so the user and workspace are bound
    # to a short-lived httpOnly state cookie, scoped to the callback path
    connector = registry.get(provider)
    response.set_cookie(
        _state_cookie(connector.credential), state, httponly=True, secure=cookie_secure(), samesite=cookie_samesite(),
        path=connector.oauth.redirect_path, max_age=600,
    )


async def oauth_callback(credential: str, request: Request, state: str, code: str | None, error: str | None) -> RedirectResponse:
    """Lands back where the flow started with ?{provider}=connected|denied|error.

    One callback per OAuth app; the state says which of its products was being connected.
    """
    expected_state = request.cookies.get(_state_cookie(credential))
    user_id, workspace_id, return_to, provider, _ = (state.split(".", 4) + [""] * 5)[:5]
    products = [p for p, c in registry.CONNECTORS.items() if c.credential == credential]
    if provider not in products:
        provider, expected_state = products[0], None
    if not expected_state or not secrets.compare_digest(state, expected_state):
        outcome = "error"
    elif error or not code:
        outcome = "denied"
    else:
        try:
            await service.complete_authorization(provider, workspace_id, user_id, code)
            outcome = "connected"
        except AppError:
            outcome = "error"

    landing = LANDING.get(return_to, LANDING["onboarding"]).format(provider=provider)
    query = f"{provider}={outcome}" + (f"&w={workspace_id}" if outcome != "error" else "")
    response = RedirectResponse(f"{get_settings().frontend_origin}{landing}?{query}")
    response.delete_cookie(_state_cookie(credential), path=registry.get(provider).oauth.redirect_path)
    return response


@router.get("")
async def list_connections(member: dict = Depends(workspace_member())) -> list[Connection]:
    return await service.catalog(member["workspace_id"])


@router.post("/{provider}/authorize")
async def authorize(
    provider: str, response: Response, body: AuthorizeRequest = AuthorizeRequest(),
    member: dict = Depends(workspace_member(Role.ADMIN)),
) -> AuthorizationUrl:
    url, state = await service.start_authorization(member, provider, body.return_to)
    set_state_cookie(response, provider, state)
    return AuthorizationUrl(authorization_url=url)


@router.delete("/{provider}")
async def disconnect(provider: str, member: dict = Depends(workspace_member(Role.ADMIN))) -> None:
    await service.disconnect(member["workspace_id"], provider)


@router.get("/{provider}/targets/available")
async def available_targets(provider: str, member: dict = Depends(workspace_member())) -> list[Target]:
    return await service.list_available_targets(member["workspace_id"], provider)


@router.get("/{provider}/targets/{key}/assignees")
async def assignees(provider: str, key: str, member: dict = Depends(workspace_member())) -> list[Assignee]:
    return await service.list_assignees(member["workspace_id"], provider, key)


@router.post("/{provider}/targets", status_code=201)
async def add_target(
    provider: str, body: AddTargetRequest, background_tasks: BackgroundTasks,
    member: dict = Depends(workspace_member(Role.ADMIN)),
) -> SyncTarget:
    target = await service.add_target(member["workspace_id"], provider, body.key, body.name)
    background_tasks.add_task(jobs.run_connector_sync, member["workspace_id"], provider, body.key)
    return target


@router.delete("/{provider}/targets/{key}")
async def remove_target(provider: str, key: str, member: dict = Depends(workspace_member(Role.ADMIN))) -> None:
    await service.remove_target(member["workspace_id"], provider, key)


@router.post("/{provider}/sync", status_code=202)
async def sync_now(provider: str, background_tasks: BackgroundTasks, member: dict = Depends(workspace_member(Role.ADMIN))) -> None:
    for key in await service.request_full_sync(member["workspace_id"], provider):
        background_tasks.add_task(jobs.run_connector_sync, member["workspace_id"], provider, key)
