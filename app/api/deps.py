from collections.abc import Awaitable, Callable

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.repository import get_user
from app.core.constants import Role
from app.core.exceptions import AppError, UnauthorizedError
from app.core.security import TokenError, verify_access_token
from app.github.repository import get_readable
from app.workspaces.service import require_member

_bearer = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str:
    """The caller's address, for rate limits; uvicorn takes it from the proxy's X-Forwarded-For."""
    return request.client.host if request.client else "unknown"


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None:
        raise UnauthorizedError("missing bearer token", error_code="missing_token")
    try:
        return verify_access_token(credentials.credentials)
    except TokenError:
        raise UnauthorizedError("invalid or expired access token", error_code="invalid_token")


async def get_current_user(user_id: str = Depends(get_current_user_id)) -> dict:
    user = await get_user(user_id)
    if user is None:
        raise UnauthorizedError("user not found", error_code="unknown_user")
    return user


def workspace_member(min_role: str = Role.MEMBER) -> Callable[..., Awaitable[dict]]:
    """The caller's membership of the workspace named by the X-Workspace-Id header."""

    async def dependency(
        x_workspace_id: str | None = Header(default=None), user_id: str = Depends(get_current_user_id),
    ) -> dict:
        if not x_workspace_id:
            raise AppError("X-Workspace-Id header is required", error_code="workspace_required")
        return await require_member(x_workspace_id, user_id, min_role)

    return dependency


def workspace_member_by_path(min_role: str = Role.MEMBER) -> Callable[..., Awaitable[dict]]:
    """Same check for /workspaces/{workspace_id}/... routes, where the workspace is the resource."""

    async def dependency(workspace_id: str, user_id: str = Depends(get_current_user_id)) -> dict:
        return await require_member(workspace_id, user_id, min_role)

    return dependency


async def require_connected_repo(repo_full_name: str, member: dict = Depends(workspace_member())) -> dict:
    """Authorise access to one repo's graph; every graph route depends on this.

    Node ids embed the repo name, so without one check in front of them a crafted
    id could read another tenant's subgraph.
    """
    return await get_readable(member["workspace_id"], repo_full_name)
