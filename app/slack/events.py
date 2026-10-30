"""Slack's Events API: one endpoint that Slack calls for every subscribed event in every installed workspace.

The signature is checked over the raw body before anything is parsed. Slack wants an answer
within 3 seconds, so this only routes the event: messages from watched channels go to the
buffer (`app.slack.messages`), and nothing slow happens here. Slack redelivers an event it
thinks failed; every change below is idempotent, so a redelivery is harmless.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import structlog
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse

from app.connectors.repository import ACTIVE
from app.core.config import get_settings
from app.core.constants import ConnectionStatus, Provider
from app.core.exceptions import AppError, UnauthorizedError
from app.core.responses import api_router
from app.db import mongo, oauth_tokens
from app.slack import messages

router = api_router(prefix="/webhooks/slack", tags=["webhooks"])
logger = structlog.get_logger("app.slack")

# Slack's replay window: an older request is rejected even with a valid signature
MAX_REQUEST_AGE_SECONDS = 300
# who listens to a Slack workspace is looked up at most this often; a newly watched channel waits this long
LISTENERS_TTL_SECONDS = 60
# the app is gone from the Slack workspace, so its token is too
UNINSTALL_EVENTS = {"app_uninstalled", "tokens_revoked"}


@dataclass(frozen=True)
class Listener:
    """A Bearings workspace connected to a Slack workspace, and the channels it watches."""

    workspace_id: str
    channels: frozenset[str]


_listeners: dict[str, tuple[float, list[Listener]]] = {}


def forget_listeners() -> None:
    _listeners.clear()


def verify_signature(raw_body: bytes, timestamp: str | None, signature: str | None, now: float | None = None) -> bool:
    """Slack's v0 signature: HMAC-SHA256 of `v0:{timestamp}:{body}` with the app's signing secret."""
    secret = get_settings().slack_signing_secret
    if not secret or not timestamp or not signature:
        return False
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > MAX_REQUEST_AGE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, f"v0={expected}")


@router.post("/events")
async def slack_events(request: Request) -> Response:
    raw_body = await request.body()
    if not verify_signature(raw_body, request.headers.get("x-slack-request-timestamp"), request.headers.get("x-slack-signature")):
        logger.warning("slack_event_bad_signature")
        raise UnauthorizedError("invalid Slack signature", error_code="slack_bad_signature")

    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise AppError("unreadable Slack event", error_code="slack_bad_payload") from exc
    if payload.get("type") == "url_verification":
        return PlainTextResponse(str(payload.get("challenge", "")))
    if payload.get("type") == "event_callback":
        await _handle(payload.get("team_id") or "", payload.get("event") or {})
    return Response(status_code=200)


async def _handle(team_id: str, event: dict) -> None:
    if event.get("type") in UNINSTALL_EVENTS:
        if event["type"] == "app_uninstalled" or (event.get("tokens") or {}).get("bot"):
            await _uninstalled(team_id)
        return
    change = messages.change_from(event)
    channel = event.get("channel")
    if change is None or not channel:
        return
    for listener in await _listeners_of(team_id):
        if channel in listener.channels:
            messages.buffer.apply(listener.workspace_id, team_id, channel, change)


async def _listeners_of(team_id: str) -> list[Listener]:
    cached = _listeners.get(team_id)
    if cached and time.monotonic() - cached[0] < LISTENERS_TTL_SECONDS:
        return cached[1]
    found = await _read_listeners(team_id)
    _listeners[team_id] = (time.monotonic(), found)
    return found


async def _read_listeners(team_id: str) -> list[Listener]:
    workspace_ids = [
        conn["workspace_id"]
        async for conn in mongo.connections().find(
            {"provider": Provider.SLACK, "external_account.id": team_id, **ACTIVE}, {"workspace_id": 1},
        )
    ]
    if not workspace_ids:
        return []
    channels: dict[str, set[str]] = {workspace_id: set() for workspace_id in workspace_ids}
    async for target in mongo.sync_targets().find(
        {"provider": Provider.SLACK, "workspace_id": {"$in": workspace_ids}, **ACTIVE}, {"workspace_id": 1, "key": 1},
    ):
        channels[target["workspace_id"]].add(target["key"])
    return [Listener(workspace_id, frozenset(keys)) for workspace_id, keys in channels.items()]


async def _uninstalled(team_id: str) -> None:
    """The app was removed from the Slack workspace: ask for a reconnect, and drop its chat text."""
    for listener in await _read_listeners(team_id):
        await oauth_tokens.set_status(listener.workspace_id, Provider.SLACK, ConnectionStatus.NEEDS_REAUTH)
        await messages.delete_workspace(listener.workspace_id)
    _listeners.pop(team_id, None)
    logger.info("slack_app_uninstalled", team_id=team_id)
