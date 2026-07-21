import uuid
from datetime import datetime, timedelta, timezone

import structlog

from app.core import security
from app.core.exceptions import UnauthorizedError
from app.db import mongo

logger = structlog.get_logger("app.auth")

REFRESH_TOKEN_TTL = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def upsert_user_from_github(profile: dict, github_access_token: str) -> dict:
    """Create or update the user for a GitHub profile and store the encrypted GitHub token."""
    now = _now()
    github_id = profile["id"]
    existing = await mongo.users().find_one({"github_id": github_id})
    if existing:
        user_id = existing["_id"]
        await mongo.users().update_one(
            {"_id": user_id},
            {"$set": {
                "github_login": profile["login"],
                "name": profile.get("name"),
                "avatar_url": profile.get("avatar_url"),
                "updated_at": now,
            }},
        )
    else:
        user_id = uuid.uuid4().hex
        await mongo.users().insert_one({
            "_id": user_id,
            "github_id": github_id,
            "github_login": profile["login"],
            "name": profile.get("name"),
            "avatar_url": profile.get("avatar_url"),
            "created_at": now,
            "updated_at": now,
        })

    await mongo.oauth_tokens().update_one(
        {"user_id": user_id, "provider": "github"},
        {"$set": {
            "access_token_encrypted": security.encrypt_secret(github_access_token),
            "status": "active",
            "updated_at": now,
        }},
        upsert=True,
    )
    user = await mongo.users().find_one({"_id": user_id})
    return user


async def create_session(user_id: str) -> tuple[str, str]:
    """Start a new session family. Returns (access_jwt, raw_refresh_token)."""
    raw_refresh = security.generate_refresh_token()
    now = _now()
    await mongo.refresh_tokens().insert_one({
        "token_hash": security.hash_refresh_token(raw_refresh),
        "user_id": user_id,
        "family_id": uuid.uuid4().hex,
        "created_at": now,
        "expires_at": now + REFRESH_TOKEN_TTL,
        "revoked": False,
    })
    return security.issue_access_token(user_id), raw_refresh


async def rotate_refresh_token(raw_refresh: str) -> tuple[str, str]:
    """Validate + rotate a refresh token. Returns (access_jwt, new_raw_refresh_token).

    Reuse detection: presenting an already-rotated/revoked token revokes the whole
    session family — the standard mitigation for stolen refresh tokens.
    """
    doc = await mongo.refresh_tokens().find_one({"token_hash": security.hash_refresh_token(raw_refresh)})
    if doc is None:
        raise UnauthorizedError("invalid refresh token", error_code="invalid_refresh_token")

    if doc["revoked"]:
        await revoke_family(doc["family_id"])
        logger.warning("refresh_token_reuse_detected", family_id=doc["family_id"], user_id=doc["user_id"])
        raise UnauthorizedError("refresh token reuse detected", error_code="refresh_token_reused")

    expires_at = doc["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _now():
        raise UnauthorizedError("refresh token expired", error_code="refresh_token_expired")

    new_raw = security.generate_refresh_token()
    now = _now()
    await mongo.refresh_tokens().update_one(
        {"_id": doc["_id"]},
        {"$set": {"revoked": True, "replaced_by": security.hash_refresh_token(new_raw)}},
    )
    await mongo.refresh_tokens().insert_one({
        "token_hash": security.hash_refresh_token(new_raw),
        "user_id": doc["user_id"],
        "family_id": doc["family_id"],
        "created_at": now,
        "expires_at": now + REFRESH_TOKEN_TTL,
        "revoked": False,
    })
    return security.issue_access_token(doc["user_id"]), new_raw


async def revoke_family(family_id: str) -> None:
    await mongo.refresh_tokens().update_many({"family_id": family_id}, {"$set": {"revoked": True}})


async def logout(raw_refresh: str) -> None:
    doc = await mongo.refresh_tokens().find_one({"token_hash": security.hash_refresh_token(raw_refresh)})
    if doc:
        await revoke_family(doc["family_id"])
