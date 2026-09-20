import secrets

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.api.cookies import cookie_samesite, cookie_secure
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.integrations.github_client import get_github_client
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "overwatch_refresh"
STATE_COOKIE = "overwatch_oauth_state"


def _set_refresh_cookie(response: Response, raw_refresh: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw_refresh,
        httponly=True,
        secure=cookie_secure(),
        samesite=cookie_samesite(),
        path="/auth",
        max_age=int(auth_service.REFRESH_TOKEN_TTL.total_seconds()),
    )


@router.get("/github/login")
async def github_login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(get_github_client().authorize_url(state))
    response.set_cookie(
        STATE_COOKIE, state, httponly=True, secure=cookie_secure(),
        samesite=cookie_samesite(), path="/auth", max_age=600,
    )
    return response


@router.get("/github/callback")
async def github_callback(request: Request, code: str, state: str = ""):
    expected_state = request.cookies.get(STATE_COOKIE)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise UnauthorizedError("OAuth state mismatch", error_code="oauth_state_mismatch")

    client = get_github_client()
    github_token = await client.exchange_code(code)
    profile = await client.get_profile(github_token)
    user = await auth_service.upsert_user_from_github(profile, github_token)
    access_jwt, raw_refresh = await auth_service.create_session(user["_id"])

    settings = get_settings()
    response = RedirectResponse(f"{settings.frontend_origin}/auth/complete#access_token={access_jwt}")
    response.delete_cookie(STATE_COOKIE, path="/auth")
    _set_refresh_cookie(response, raw_refresh)
    return response


@router.post("/refresh")
async def refresh(request: Request, response: Response) -> dict:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if not raw_refresh:
        raise UnauthorizedError("missing refresh token", error_code="missing_refresh_token")
    access_jwt, new_raw = await auth_service.rotate_refresh_token(raw_refresh)
    _set_refresh_cookie(response, new_raw)
    return {"access_token": access_jwt, "token_type": "bearer"}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if raw_refresh:
        await auth_service.logout(raw_refresh)
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return {"status": "logged_out"}
