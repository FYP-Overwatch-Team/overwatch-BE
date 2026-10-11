"""Third-party OAuth tokens (GitHub user tokens, Jira), encrypted at rest.

Both providers issue short-lived access tokens with rotating refresh tokens,
so reads go through get_fresh_access_token, which refreshes near expiry.

A GitHub token is the user's identity; a Jira connection belongs to a workspace.
`owner_id` is a user or workspace id accordingly; the provider says which.
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog

from app.core import security
from app.core.exceptions import UnauthorizedError
from app.core.time import utcnow
from app.db import mongo

logger = structlog.get_logger("app.oauth_tokens")

# refresh a little early so a token never expires mid-request
REFRESH_BUFFER = timedelta(minutes=2)

Refresher = Callable[[str], Awaitable[dict]]

def _key(owner_id: str, provider: str) -> dict:
    return {"owner_id": owner_id, "provider": provider}


async def save(owner_id: str, provider: str, token_response: dict, **extra) -> None:
    """Store a provider's token response (and any extra fields, e.g. Jira's cloud_id)."""
    now = utcnow()
    fields = {
        "access_token_encrypted": security.encrypt_secret(token_response["access_token"]),
        "expires_at": _expiry(token_response.get("expires_in")),
        "status": "active",
        "updated_at": now,
        **extra,
    }
    if token_response.get("refresh_token"):
        fields["refresh_token_encrypted"] = security.encrypt_secret(token_response["refresh_token"])
        fields["refresh_expires_at"] = _expiry(token_response.get("refresh_token_expires_in"))
    await mongo.oauth_tokens().update_one(
        _key(owner_id, provider), {"$set": fields}, upsert=True,
    )


async def get(owner_id: str, provider: str) -> dict | None:
    return await mongo.oauth_tokens().find_one(_key(owner_id, provider))


async def set_status(owner_id: str, provider: str, status: str) -> None:
    await mongo.oauth_tokens().update_one(
        _key(owner_id, provider),
        {"$set": {"status": status, "updated_at": utcnow()}},
    )


async def get_fresh_access_token(owner_id: str, provider: str, refresh: Refresher) -> str:
    """The decrypted access token, refreshed first if it's about to expire."""
    doc = await get(owner_id, provider)
    if doc is None or doc.get("status") != "active":
        raise _needs_reauth(provider)

    expires_at = doc.get("expires_at")
    if expires_at is not None and expires_at - REFRESH_BUFFER <= utcnow():
        doc = await _refresh(owner_id, provider, doc, refresh)
    return security.decrypt_secret(doc["access_token_encrypted"])


async def _refresh(owner_id: str, provider: str, doc: dict, refresh: Refresher) -> dict:
    if not doc.get("refresh_token_encrypted"):
        await set_status(owner_id, provider, "needs_reauth")
        raise _needs_reauth(provider)
    try:
        token_response = await refresh(security.decrypt_secret(doc["refresh_token_encrypted"]))
    except UnauthorizedError:
        # refresh tokens rotate: if a concurrent request already refreshed, its token is the good one
        latest = await get(owner_id, provider)
        if latest and latest.get("refresh_token_encrypted") != doc["refresh_token_encrypted"]:
            return latest
        await set_status(owner_id, provider, "needs_reauth")
        raise
    await save(owner_id, provider, token_response)
    logger.info("oauth_token_refreshed", provider=provider, owner_id=owner_id)
    return await get(owner_id, provider)


def _expiry(seconds: int | None):
    return None if seconds is None else utcnow() + timedelta(seconds=seconds)


def _needs_reauth(provider: str) -> UnauthorizedError:
    return UnauthorizedError(f"{provider} connection needs re-authorization", error_code=f"{provider}_needs_reauth")
