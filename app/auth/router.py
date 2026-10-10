import secrets

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import get_current_user, get_current_user_id
from app.auth import service
from app.core.config import get_settings
from app.core.cookies import cookie_samesite, cookie_secure
from app.core.exceptions import UnauthorizedError
from app.integrations.github_client import get_github_client

router = APIRouter(tags=["auth"])

REFRESH_COOKIE = "overwatch_refresh"
STATE_COOKIE = "overwatch_oauth_state"
COOKIE_PATH = "/auth"


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, httponly=True, secure=cookie_secure(), samesite=cookie_samesite(),
        path=COOKIE_PATH, max_age=max_age,
    )


@router.get("/auth/github/login")
async def github_login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(get_github_client().authorize_url(state))
    _set_cookie(response, STATE_COOKIE, state, max_age=600)
    return response


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str, state: str = "") -> RedirectResponse:
    expected_state = request.cookies.get(STATE_COOKIE)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise UnauthorizedError("OAuth state mismatch", error_code="oauth_state_mismatch")

    client = get_github_client()
    github_token = await client.exchange_code(code)
    profile = await client.get_profile(github_token)
    access_jwt, raw_refresh = await service.sign_in_with_github(profile, github_token)

    response = RedirectResponse(f"{get_settings().frontend_origin}/auth/complete#access_token={access_jwt}")
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    _set_cookie(response, REFRESH_COOKIE, raw_refresh, max_age=int(service.REFRESH_TOKEN_TTL.total_seconds()))
    return response


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response) -> dict:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if not raw_refresh:
        raise UnauthorizedError("missing refresh token", error_code="missing_refresh_token")
    access_jwt, new_raw = await service.rotate(raw_refresh)
    _set_cookie(response, REFRESH_COOKIE, new_raw, max_age=int(service.REFRESH_TOKEN_TTL.total_seconds()))
    return {"access_token": access_jwt, "token_type": "bearer"}


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> dict:
    if raw_refresh := request.cookies.get(REFRESH_COOKIE):
        await service.logout(raw_refresh)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    return {"status": "logged_out"}


@router.post("/auth/logout-all")
async def logout_all(response: Response, user_id: str = Depends(get_current_user_id)) -> dict:
    await service.logout_everywhere(user_id)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    return {"status": "logged_out"}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> dict:
    return {
        "id": user["_id"],
        "github_login": user["github_login"],
        "name": user.get("name"),
        "avatar_url": user.get("avatar_url"),
    }
