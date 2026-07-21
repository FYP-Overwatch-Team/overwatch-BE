from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import UnauthorizedError
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
