from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import NotFoundError, UnauthorizedError
from app.core.repo_identity import validate_repo_full_name
from app.core.security import TokenError, verify_access_token
from app.db import mongo

_bearer = HTTPBearer(auto_error=False)


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
    user = await mongo.users().find_one({"_id": user_id})
    if user is None:
        raise UnauthorizedError("user not found", error_code="unknown_user")
    return user


async def require_connected_repo(
    repo_full_name: str, user_id: str = Depends(get_current_user_id),
) -> dict:
    """Authorise access to one repository's graph.

    Every graph route depends on this. Node ids embed the repository name, so
    without a single check in front of them a crafted id would be a way to
    read another tenant's subgraph. Returning the repository document also
    gives routes the parse status and graph version without a second query.
    """
    validate_repo_full_name(repo_full_name)
    repo = await mongo.repos().find_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
    )
    if repo is None:
        raise NotFoundError("repository is not connected", error_code="repo_not_connected")
    return repo
