"""Users and refresh-token sessions in Mongo."""

import uuid
from datetime import datetime

from pymongo import ReturnDocument

from app.core.time import utcnow
from app.db import mongo


async def upsert_github_user(profile: dict) -> dict:
    """Create or refresh the user for a GitHub profile in one atomic write."""
    now = utcnow()
    return await mongo.users().find_one_and_update(
        {"github_id": profile["id"]},
        {
            "$set": {
                "github_login": profile["login"],
                "name": profile.get("name"),
                "avatar_url": profile.get("avatar_url"),
                "updated_at": now,
            },
            "$setOnInsert": {"_id": uuid.uuid4().hex, "created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def get_user(user_id: str) -> dict | None:
    return await mongo.users().find_one({"_id": user_id})


async def find_user_by_github_id(github_id: int) -> dict | None:
    return await mongo.users().find_one({"github_id": github_id})


async def insert_refresh_token(token_hash: str, user_id: str, family_id: str, expires_at: datetime) -> None:
    await mongo.refresh_tokens().insert_one({
        "token_hash": token_hash,
        "user_id": user_id,
        "family_id": family_id,
        "created_at": utcnow(),
        "expires_at": expires_at,
        "revoked": False,
    })


async def claim_refresh_token(token_hash: str, replaced_by: str) -> dict | None:
    """Revoke a live token and return it; None if it was unknown, used or expired.

    One write, so two concurrent refreshes can't both rotate the same token.
    """
    now = utcnow()
    return await mongo.refresh_tokens().find_one_and_update(
        {"token_hash": token_hash, "revoked": False, "expires_at": {"$gt": now}},
        {"$set": {"revoked": True, "rotated_at": now, "replaced_by": replaced_by}},
    )


async def find_refresh_token(token_hash: str) -> dict | None:
    return await mongo.refresh_tokens().find_one({"token_hash": token_hash})


async def revoke_family(family_id: str) -> None:
    await mongo.refresh_tokens().update_many({"family_id": family_id}, {"$set": {"revoked": True}})


async def revoke_user_sessions(user_id: str) -> None:
    await mongo.refresh_tokens().update_many({"user_id": user_id}, {"$set": {"revoked": True}})
