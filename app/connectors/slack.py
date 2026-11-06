"""Slack connector: the app's bot installed in a workspace, and the channels it listens to.

Slack answers most failures with HTTP 200 and `{"ok": false, "error": ...}`, so every call
reads `ok` itself: `NativeOAuth` only notices a 401, which Slack doesn't send for a revoked token.
Bot tokens don't expire (unless the app opts into rotation), so there's nothing to refresh.
"""

import httpx
import structlog

from app.connectors.oauth import OAuthConfig, http
from app.connectors.ports import AuthBackend
from app.core.constants import ConnectionStatus, Provider
from app.core.exceptions import ExternalServiceError, ForbiddenError, UnauthorizedError
from app.db import oauth_tokens
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors.slack")

API_BASE = "https://slack.com/api"

OAUTH = OAuthConfig(
    provider=Provider.SLACK,
    authorize_url="https://slack.com/oauth/v2/authorize",
    token_url=f"{API_BASE}/oauth.v2.access",
    scopes=(),
    client_id_setting="slack_client_id",
    client_secret_setting="slack_client_secret",
    # Slack only accepts an HTTPS redirect, so local runs need APP_BASE_URL on https
    redirect_path="/slack/callback",
    scope_separator=",",
    client_auth="basic_form",
)

# bot scopes: listing the public channels and the private ones the bot was invited to, reading their
# messages (delivered as events; the bot only hears channels it's a member of), posting the daily
# digest there, and reading the email of someone who clicks a digest button, to find them in Bearings
SCOPES = (
    "channels:read", "groups:read", "channels:history", "groups:history",
    "chat:write", "users:read", "users:read.email",
)

# the install or its token is gone: only reconnecting helps
REVOKED_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
CHANNELS_PAGE_SIZE = 200
NOT_MEMBER_HINT = "invite the app to it first (/invite in the channel)"
# 10,000 channels: more than any real workspace, and a stop for a cursor that never ends
MAX_CHANNEL_PAGES = 50


class SlackAdapter:
    async def resolve_account(self, access_token: str, known_site_id: str | None = None) -> dict:
        resp = await http.post(f"{API_BASE}/auth.test", headers={"Authorization": f"Bearer {access_token}"})
        log_call(logger, "slack_api_call", "POST", "/api/auth.test", resp)
        body = _read(resp, "auth.test")
        if not body.get("ok"):
            raise _failed("auth.test", body.get("error"))
        return {"id": body["team_id"], "name": body.get("team"), "url": body.get("url")}

    async def list_targets(self, auth: AuthBackend, conn: dict) -> list[dict]:
        channels: list[dict] = []
        cursor = ""
        for _ in range(MAX_CHANNEL_PAGES):
            body = await _call(auth, conn, "conversations.list", params={
                "types": "public_channel,private_channel",
                "exclude_archived": "true",
                "limit": CHANNELS_PAGE_SIZE,
                **({"cursor": cursor} if cursor else {}),
            })
            channels += [
                {
                    "key": channel["id"], "name": f"#{channel['name']}", "id": channel["id"],
                    # Slack only sends a channel's messages to an app that's in it
                    "hint": None if channel.get("is_member") else NOT_MEMBER_HINT,
                }
                for channel in body.get("channels", [])
            ]
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return sorted(channels, key=lambda channel: channel["name"])

    async def post_message(self, auth: AuthBackend, conn: dict, channel: str, text: str, blocks: list[dict]) -> str:
        """Post as the app's bot in a channel it's in; returns the message's ts. `text` is the notification fallback."""
        body = await _call(auth, conn, "chat.postMessage", json={
            "channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False,
        })
        return body["ts"]

    async def user_email(self, auth: AuthBackend, conn: dict, user_id: str) -> str | None:
        """A Slack member's confirmed email; None for bots, members without one, and emails Slack hasn't confirmed."""
        body = await _call(auth, conn, "users.info", params={"user": user_id})
        user = body.get("user") or {}
        email = (user.get("profile") or {}).get("email")
        if user.get("is_bot") or not email or user.get("is_email_confirmed") is False:
            return None
        return email.strip().lower()

    async def revoke(self, auth: AuthBackend, conn: dict) -> None:
        """Give the bot token back, so the app stops hearing the workspace's channels."""
        await _call(auth, conn, "auth.revoke", params={})


async def _call(auth: AuthBackend, conn: dict, method: str, *, params: dict | None = None, json: dict | None = None) -> dict:
    """One Web API call for a connection (a JSON POST when `json` is given); a revoked install marks it to reconnect."""
    if json is None:
        resp = await auth.request(conn, "GET", f"{API_BASE}/{method}", params=params)
    else:
        resp = await auth.request(conn, "POST", f"{API_BASE}/{method}", json=json, headers={"Content-Type": "application/json; charset=utf-8"})
    body = _read(resp, method)
    if body.get("ok"):
        return body
    error = body.get("error")
    # Slack answers failures with HTTP 200, so the request log shows 200: the error name is what to look at
    logger.warning("slack_api_error", method=method, error=error)
    if error in REVOKED_ERRORS:
        await oauth_tokens.set_status(conn["workspace_id"], OAUTH.provider, ConnectionStatus.NEEDS_REAUTH)
        raise UnauthorizedError("slack token is no longer valid", error_code="slack_needs_reauth")
    if error == "missing_scope":
        raise ForbiddenError("reconnect slack to allow this", error_code="slack_scope_missing")
    raise _failed(method, error)


def _read(resp: httpx.Response, method: str) -> dict:
    if resp.status_code != 200:
        raise _failed(method, f"http {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise _failed(method, "unreadable reply") from exc


def _failed(method: str, reason: str | None) -> ExternalServiceError:
    return ExternalServiceError(f"slack {method} failed: {reason or 'unknown error'}", error_code="slack_unavailable")
