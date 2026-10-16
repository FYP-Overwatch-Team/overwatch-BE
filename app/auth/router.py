import json
import secrets
from datetime import timedelta

from fastapi import BackgroundTasks, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import client_ip, get_current_user, get_current_user_id, same_origin
from app.auth import native, service, usernames
from app.auth.schemas import (
    AccessToken,
    AddEmailRequest,
    AddEmailResult,
    AuthorizationUrl,
    ChallengeIssued,
    ConfirmPassword,
    ForgotPasswordRequest,
    InviteSession,
    InviteSignupRequest,
    LinkGithubRequest,
    LoginRequest,
    Me,
    ResendAccepted,
    ResendRequest,
    ResetPasswordRequest,
    SetUsername,
    SignupRequest,
    UsernameAvailability,
    VerifyEmailRequest,
)
from app.core import security
from app.core.config import get_settings
from app.core.cookies import cookie_samesite, cookie_secure
from app.core.exceptions import AppError, UnauthorizedError
from app.core.rate_limit import Limit, take
from app.core.responses import api_router
from app.integrations.github_app import get_github_app

router = api_router(tags=["auth"])

REFRESH_COOKIE = "overwatch_refresh"
STATE_COOKIE = "overwatch_oauth_state"
COOKIE_PATH = "/auth"
REFRESH_MAX_AGE = int(service.REFRESH_TOKEN_TTL.total_seconds())
STATE_MAX_AGE = 600
# where a "connect GitHub" round trip lands afterwards
LINK_RETURN = {"onboarding": "/onboarding", "settings": "/settings/integrations", "profile": "/settings/profile"}
USERNAME_CHECKS_PER_IP = Limit("username:ip", 60, timedelta(minutes=1))


async def _start_session(response: Response, user_id: str) -> str:
    _set_cookie(response, REFRESH_COOKIE, await service.create_session(user_id), max_age=REFRESH_MAX_AGE)
    return security.issue_access_token(user_id)


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name, value, httponly=True, secure=cookie_secure(), samesite=cookie_samesite(),
        path=COOKIE_PATH, max_age=max_age,
    )


def _to_frontend(path: str) -> RedirectResponse:
    response = RedirectResponse(f"{get_settings().frontend_origin}{path}")
    response.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
    return response


def _state_cookie(response: Response, state: dict) -> None:
    # encrypted, so a link round trip can't be pointed at another user's account
    _set_cookie(response, STATE_COOKIE, security.encrypt_secret(json.dumps(state)), max_age=STATE_MAX_AGE)


def _read_state(request: Request) -> dict:
    try:
        return json.loads(security.decrypt_secret(request.cookies.get(STATE_COOKIE, "")))
    except (security.TokenError, ValueError):
        return {}


def _authorize(state: dict) -> tuple[str, dict]:
    state_value = secrets.token_urlsafe(24)
    verifier, challenge = security.pkce_pair()
    return get_github_app().authorize_url(state_value, challenge), {**state, "state": state_value, "verifier": verifier}


@router.get("/auth/github/login")
async def github_login() -> RedirectResponse:
    url, state = _authorize({})
    response = RedirectResponse(url)
    _state_cookie(response, state)
    return response


@router.post("/auth/github/link", dependencies=[Depends(same_origin)])
async def github_link(body: LinkGithubRequest, response: Response, user_id: str = Depends(get_current_user_id)) -> AuthorizationUrl:
    """Start connecting GitHub to the signed-in account; the browser goes to the returned URL."""
    url, state = _authorize({"link_user": user_id, "return_to": body.return_to})
    _state_cookie(response, state)
    return AuthorizationUrl(authorization_url=url)


@router.get("/auth/github/callback")
async def github_callback(
    request: Request, state: str = "", code: str | None = None, error: str | None = None,
) -> RedirectResponse:
    """Always lands the browser back on the frontend: /auth/complete or /sign-in?error=..., or the link's page."""
    saved = _read_state(request)
    expected_state = saved.get("state", "")
    if not expected_state or not secrets.compare_digest(state, expected_state):
        return _to_frontend("/sign-in?error=oauth_state_mismatch")
    link_user = saved.get("link_user")
    back = LINK_RETURN.get(saved.get("return_to", ""), LINK_RETURN["settings"])
    if error or not code:
        return _to_frontend(f"{back}?github=denied" if link_user else "/sign-in?error=oauth_cancelled")

    github = get_github_app()
    try:
        tokens = await github.exchange_code(code, saved.get("verifier", ""))
        profile = await github.get_user(tokens["access_token"])
    except AppError:
        return _to_frontend(f"{back}?github=error" if link_user else "/sign-in?error=github_unavailable")

    if link_user:
        # linking never touches the session cookie
        try:
            merge = await service.link_github(link_user, profile, tokens)
        except AppError:
            return _to_frontend(f"{back}?github=in_use")
        # a merge is confirmed where sign-in methods are managed, wherever the round trip started
        return _to_frontend(f"{LINK_RETURN['profile']}?github=merge" if merge else f"{back}?github=linked")

    # no token in the URL: the frontend calls /auth/refresh with the cookie set here
    try:
        raw_refresh = await service.sign_in_with_github(profile, tokens)
    except AppError as exc:
        return _to_frontend(f"/sign-in?error={exc.error_code}")
    response = _to_frontend("/auth/complete")
    _set_cookie(response, REFRESH_COOKIE, raw_refresh, max_age=REFRESH_MAX_AGE)
    return response


@router.post("/auth/signup", status_code=202, dependencies=[Depends(same_origin)])
async def signup(body: SignupRequest, background: BackgroundTasks, ip: str = Depends(client_ip)) -> ChallengeIssued:
    """The same answer whether or not the email has an account: a challenge to enter the emailed code into."""
    return await native.signup(background, ip, body.name, body.username, body.email, body.password)


@router.post("/auth/signup/invite", dependencies=[Depends(same_origin)])
async def signup_with_invite(body: InviteSignupRequest, response: Response, ip: str = Depends(client_ip)) -> InviteSession:
    user, workspace = await native.signup_with_invite(ip, body.token, body.name, body.username, body.password)
    access = await _start_session(response, user["_id"])
    return InviteSession(access_token=access, workspace=workspace)


@router.post("/auth/login", dependencies=[Depends(same_origin)])
async def login(body: LoginRequest, response: Response, background: BackgroundTasks, ip: str = Depends(client_ip)) -> AccessToken:
    user = await native.login(background, ip, body.email, body.password)
    return AccessToken(access_token=await _start_session(response, user["_id"]))


@router.post("/auth/email/verify", dependencies=[Depends(same_origin)])
async def verify_email(body: VerifyEmailRequest, response: Response, ip: str = Depends(client_ip)) -> AccessToken:
    """A right code verifies the email and signs the user in, so they never sign in again after signing up."""
    user = await native.verify_email(ip, body.challenge_id, body.code)
    return AccessToken(access_token=await _start_session(response, user["_id"]))


@router.post("/auth/otp/resend", status_code=202)
async def resend_code(body: ResendRequest, background: BackgroundTasks, ip: str = Depends(client_ip)) -> ResendAccepted:
    return await native.resend(background, ip, body.challenge_id)


@router.post("/auth/password/forgot", status_code=202)
async def forgot_password(body: ForgotPasswordRequest, background: BackgroundTasks, ip: str = Depends(client_ip)) -> ChallengeIssued:
    return await native.forgot_password(background, ip, body.email)


@router.post("/auth/password/reset", dependencies=[Depends(same_origin)])
async def reset_password(body: ResetPasswordRequest, ip: str = Depends(client_ip)) -> None:
    """Doesn't sign in: every session ends, and the user signs in with the new password."""
    await native.reset_password(ip, body.challenge_id, body.code, body.password)


@router.get("/auth/username-availability")
async def username_availability(
    response: Response, username: str = Query(max_length=usernames.USERNAME_MAX_LENGTH * 2), ip: str = Depends(client_ip),
) -> UsernameAvailability:
    await take(USERNAME_CHECKS_PER_IP, ip)
    response.headers["Cache-Control"] = "no-store"
    normalized = usernames.normalize(username)
    reason = await usernames.availability(normalized)
    return UsernameAvailability(username=normalized, available=reason is None, reason=reason)


@router.post("/auth/refresh", dependencies=[Depends(same_origin)])
async def refresh(request: Request, response: Response) -> AccessToken:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if not raw_refresh:
        raise UnauthorizedError("missing refresh token", error_code="missing_refresh_token")
    access_jwt, new_raw = await service.rotate(raw_refresh)
    _set_cookie(response, REFRESH_COOKIE, new_raw, max_age=REFRESH_MAX_AGE)
    return AccessToken(access_token=access_jwt)


@router.post("/auth/logout", dependencies=[Depends(same_origin)])
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
    return Me.of(user)


@router.put("/me/username")
async def set_username(body: SetUsername, user: dict = Depends(get_current_user)) -> Me:
    return Me.of(await native.set_username(user, body.username))


@router.post("/me/email", status_code=202, dependencies=[Depends(same_origin)])
async def add_email(
    body: AddEmailRequest, background: BackgroundTasks, ip: str = Depends(client_ip), user: dict = Depends(get_current_user),
) -> ChallengeIssued:
    """Start adding an email and password to the account: a code goes to the address, whoever holds it."""
    return await native.start_add_email(background, ip, user, body.email, body.password)


@router.post("/me/email/verify", dependencies=[Depends(same_origin)])
async def verify_added_email(
    body: VerifyEmailRequest, ip: str = Depends(client_ip), user: dict = Depends(get_current_user),
) -> AddEmailResult:
    outcome, result = await native.finish_add_email(ip, user, body.challenge_id, body.code)
    if outcome == "merge_required":
        return AddEmailResult(outcome=outcome, merge=result)
    return AddEmailResult(outcome=outcome, me=Me.of(result))


@router.delete("/me/github", dependencies=[Depends(same_origin)])
async def unlink_github(body: ConfirmPassword, user: dict = Depends(get_current_user)) -> Me:
    return Me.of(await native.unlink_github(user, body.password))


@router.delete("/me/email", dependencies=[Depends(same_origin)])
async def unlink_email(body: ConfirmPassword, user: dict = Depends(get_current_user)) -> Me:
    return Me.of(await native.unlink_email(user, body.password))

