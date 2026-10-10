"""Sessions: a short-lived access JWT plus a rotating refresh token.

Refresh tokens are grouped into families (one per sign-in). Presenting a token
that was already rotated revokes the whole family, the standard defence
against a stolen refresh token.
"""

import uuid
from datetime import timedelta
from typing import NoReturn

import structlog

from app.auth import repository
from app.core import security
from app.core.exceptions import UnauthorizedError
from app.core.time import utcnow
from app.db import oauth_tokens

logger = structlog.get_logger("app.auth")

REFRESH_TOKEN_TTL = timedelta(days=30)


async def sign_in_with_github(profile: dict, github_tokens: dict) -> tuple[str, str]:
    """Upsert the user, keep their GitHub user token, start a session. Returns (access_jwt, raw_refresh)."""
    user = await repository.upsert_github_user(profile)
    await oauth_tokens.save(user["_id"], "github", github_tokens)
    return await create_session(user["_id"])


async def create_session(user_id: str, family_id: str | None = None) -> tuple[str, str]:
    raw_refresh = security.generate_refresh_token()
    await repository.insert_refresh_token(
        security.hash_refresh_token(raw_refresh),
        user_id,
        family_id or uuid.uuid4().hex,
        utcnow() + REFRESH_TOKEN_TTL,
    )
    return security.issue_access_token(user_id), raw_refresh


async def rotate(raw_refresh: str) -> tuple[str, str]:
    """Swap a refresh token for a new one. Returns (access_jwt, new_raw_refresh)."""
    token_hash = security.hash_refresh_token(raw_refresh)
    new_raw = security.generate_refresh_token()
    claimed = await repository.claim_refresh_token(token_hash, security.hash_refresh_token(new_raw))
    if claimed is None:
        await _reject(token_hash)

    await repository.insert_refresh_token(
        security.hash_refresh_token(new_raw),
        claimed["user_id"],
        claimed["family_id"],
        utcnow() + REFRESH_TOKEN_TTL,
    )
    return security.issue_access_token(claimed["user_id"]), new_raw


async def _reject(token_hash: str) -> NoReturn:
    """Explain why a token couldn't be claimed, revoking its family on reuse."""
    doc = await repository.find_refresh_token(token_hash)
    if doc is None:
        raise UnauthorizedError("invalid refresh token", error_code="invalid_refresh_token")
    if doc["revoked"]:
        await repository.revoke_family(doc["family_id"])
        logger.warning("refresh_token_reuse_detected", family_id=doc["family_id"], user_id=doc["user_id"])
        raise UnauthorizedError("refresh token reuse detected", error_code="refresh_token_reused")
    raise UnauthorizedError("refresh token expired", error_code="refresh_token_expired")


async def logout(raw_refresh: str) -> None:
    doc = await repository.find_refresh_token(security.hash_refresh_token(raw_refresh))
    if doc:
        await repository.revoke_family(doc["family_id"])


async def logout_everywhere(user_id: str) -> None:
    await repository.revoke_user_sessions(user_id)
