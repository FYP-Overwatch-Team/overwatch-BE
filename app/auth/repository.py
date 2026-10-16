"""Users and refresh-token sessions in Mongo."""

import uuid
from datetime import datetime

from pymongo import ReturnDocument

from app.core.time import utcnow
from app.db import mongo


async def upsert_github_user(profile: dict, username: str | None) -> dict:
    """Create or refresh the user for a GitHub profile in one atomic write; `username` is only set on create."""
    now = utcnow()
    refreshed = {"github_login": profile["login"], "updated_at": now}
    # a linked email account keeps its own name and picture when GitHub has none
    refreshed |= {key: profile[key] for key in ("name", "avatar_url") if profile.get(key)}
    return await mongo.users().find_one_and_update(
        {"github_id": mongo.typed(profile["id"], "number")},
        {"$set": refreshed, "$setOnInsert": {"_id": uuid.uuid4().hex, "created_at": now, "username": username}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def find_user_by_email(email: str) -> dict | None:
    return await mongo.users().find_one({"email": mongo.typed(email, "string")})


async def insert_user(doc: dict) -> dict:
    now = utcnow()
    doc = {"_id": uuid.uuid4().hex, "avatar_url": None, "created_at": now, "updated_at": now, **doc}
    await mongo.users().insert_one(doc)
    return doc


async def update_user(user_id: str, fields: dict, unset: tuple[str, ...] = ()) -> dict | None:
    update: dict = {"$set": {**fields, "updated_at": utcnow()}}
    if unset:
        update["$unset"] = dict.fromkeys(unset, "")
    return await mongo.users().find_one_and_update({"_id": user_id}, update, return_document=ReturnDocument.AFTER)


async def set_username_if_unset(user_id: str, username: str) -> bool:
    result = await mongo.users().update_one(
        {"_id": user_id, "username": {"$in": [None]}}, {"$set": {"username": username, "updated_at": utcnow()}},
    )
    return result.matched_count > 0


async def link_github(user_id: str, profile: dict) -> None:
    """Attach a GitHub account to a signed-in user; their own name and picture win."""
    user = await get_user(user_id) or {}
    fields = {"github_id": profile["id"], "github_login": profile["login"]}
    fields |= {key: profile[key] for key in ("name", "avatar_url") if profile.get(key) and not user.get(key)}
    await update_user(user_id, fields)


async def insert_challenge(doc: dict) -> None:
    await mongo.auth_challenges().insert_one(doc)


async def delete_challenges(user_id: str, purpose: str | None = None) -> None:
    await mongo.auth_challenges().delete_many({"user_id": user_id, **({"purpose": purpose} if purpose else {})})


async def find_challenge(challenge_id: str, purpose: str | None = None) -> dict | None:
    query = {"_id": challenge_id, "expires_at": {"$gt": utcnow()}}
    return await mongo.auth_challenges().find_one(query | ({"purpose": purpose} if purpose else {}))


async def latest_challenge(user_id: str, purpose: str) -> dict | None:
    return await mongo.auth_challenges().find_one(
        {"user_id": user_id, "purpose": purpose, "expires_at": {"$gt": utcnow()}}, sort=[("sent_at", -1)],
    )


async def take_attempt(challenge_id: str, purpose: str, max_attempts: int) -> dict | None:
    """Count one guess at a live code, in one write so parallel guesses can't exceed the limit."""
    now = utcnow()
    return await mongo.auth_challenges().find_one_and_update(
        {"_id": challenge_id, "purpose": purpose, "attempts": {"$lt": max_attempts},
         "code_expires_at": {"$gt": now}, "expires_at": {"$gt": now}},
        {"$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )


async def consume_challenge(challenge_id: str, code_hash: str) -> bool:
    """Use a code up; only one of two parallel correct guesses gets it."""
    result = await mongo.auth_challenges().delete_one({"_id": challenge_id, "code_hash": code_hash})
    return result.deleted_count == 1


async def renew_challenge(challenge_id: str, code_hash: str, sent_before, max_resends: int, fields: dict) -> bool:
    result = await mongo.auth_challenges().update_one(
        {"_id": challenge_id, "sent_at": {"$lte": sent_before}, "resend_count": {"$lt": max_resends}},
        {"$set": {"code_hash": code_hash, "attempts": 0, **fields}, "$inc": {"resend_count": 1}},
    )
    return result.modified_count > 0


async def get_user(user_id: str) -> dict | None:
    return await mongo.users().find_one({"_id": user_id})


async def find_user_by_github_id(github_id: int) -> dict | None:
    return await mongo.users().find_one({"github_id": mongo.typed(github_id, "number")})


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
