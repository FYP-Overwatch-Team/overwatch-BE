import secrets

from fastapi import Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import get_current_user, get_current_user_id
from app.auth import service
from app.auth.schemas import AccessToken, Me
from app.core import security
from app.core.config import get_settings
from app.core.cookies import cookie_samesite, cookie_secure
from app.core.exceptions import AppError, UnauthorizedError
from app.core.responses import api_router
from app.integrations.github_app import get_github_app

router = api_router(tags=["auth"])

REFRESH_COOKIE = "overwatch_refresh"
STATE_COOKIE = "overwatch_oauth_state"
COOKIE_PATH = "/auth"
REFRESH_MAX_AGE = int(service.REFRESH_TOKEN_TTL.total_seconds())


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, httponly=True, secure=cookie_secure(), samesite=cookie_samesite(),
        path=COOKIE_PATH, max_age=max_age,
    )


def _to_frontend(path: str) -> RedirectResponse:
    response = RedirectResponse(f"{get_settings().frontend_origin}{path}")
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return response


@router.get("/auth/github/login")
async def github_login() -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    verifier, challenge = security.pkce_pair()
    response = RedirectResponse(get_github_app().authorize_url(state, challenge))
    # both values are url-safe base64, so "." is a safe separator
    _set_cookie(response, STATE_COOKIE, f"{state}.{verifier}", max_age=600)
    return response


@router.get("/auth/github/callback")
async def github_callback(
    request: Request, state: str = "", code: str | None = None, error: str | None = None,
) -> RedirectResponse:
    """Always lands the browser back on the frontend: /auth/complete or /sign-in?error=..."""
    expected_state, _, verifier = request.cookies.get(STATE_COOKIE, "").partition(".")
    if not expected_state or not secrets.compare_digest(state, expected_state):
        return _to_frontend("/sign-in?error=oauth_state_mismatch")
    if error or not code:
        return _to_frontend("/sign-in?error=oauth_cancelled")

    github = get_github_app()
    try:
        tokens = await github.exchange_code(code, verifier)
        profile = await github.get_user(tokens["access_token"])
    except AppError:
        return _to_frontend("/sign-in?error=github_unavailable")

    # no token in the URL: the frontend calls /auth/refresh with the cookie set here
    raw_refresh = await service.sign_in_with_github(profile, tokens)
    response = _to_frontend("/auth/complete")
    _set_cookie(response, REFRESH_COOKIE, raw_refresh, max_age=REFRESH_MAX_AGE)
    return response


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response) -> AccessToken:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if not raw_refresh:
        raise UnauthorizedError("missing refresh token", error_code="missing_refresh_token")
    access_jwt, new_raw = await service.rotate(raw_refresh)
    _set_cookie(response, REFRESH_COOKIE, new_raw, max_age=REFRESH_MAX_AGE)
    return AccessToken(access_token=access_jwt)


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> None:
    if raw_refresh := request.cookies.get(REFRESH_COOKIE):
        await service.logout(raw_refresh)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)


@router.post("/auth/logout-all")
async def logout_all(response: Response, user_id: str = Depends(get_current_user_id)) -> None:
    await service.logout_everywhere(user_id)
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)


@router.get("/me")
async def me(user: dict = Depends(get_current_user)) -> Me:
    return Me(id=user["_id"], github_login=user["github_login"], name=user.get("name"), avatar_url=user.get("avatar_url"))
