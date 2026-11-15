"""Merge proposals, the merge audit trail, and the per-user lock a running merge holds."""

from datetime import datetime

from pymongo import ReturnDocument

from app.core.constants import MergeStatus
from app.core.time import utcnow
from app.db import mongo


async def save_proposal(doc: dict) -> None:
    """One pending proposal per account: a newer proof replaces the earlier one."""
    await mongo.merge_proposals().replace_one({"survivor_id": doc["survivor_id"]}, doc, upsert=True)


async def find_proposal(survivor_id: str) -> dict | None:
    return await mongo.merge_proposals().find_one({"survivor_id": survivor_id, "expires_at": {"$gt": utcnow()}})


async def delete_proposals(user_id: str) -> None:
    await mongo.merge_proposals().delete_many({"$or": [{"survivor_id": user_id}, {"absorbed_id": user_id}]})


async def lock_user(user_id: str, merge_id: str) -> bool:
    """Claim the user for one merge; False when another merge holds them."""
    locked = await mongo.users().find_one_and_update(
        {"_id": user_id, "merge_id": {"$in": [None, merge_id]}}, {"$set": {"merge_id": merge_id}},
        return_document=ReturnDocument.AFTER,
    )
    return locked is not None


async def unlock_user(user_id: str, merge_id: str) -> None:
    await mongo.users().update_one({"_id": user_id, "merge_id": merge_id}, {"$unset": {"merge_id": ""}})


async def insert_merge(doc: dict) -> None:
    await mongo.account_merges().insert_one(doc)


async def finish_merge(merge_id: str, moved: dict[str, int]) -> None:
    """Mark it done; the record stays as the audit trail, without the password hashes the merge needed."""
    await mongo.account_merges().update_one(
        {"_id": merge_id},
        {
            "$set": {"status": MergeStatus.DONE, "moved": moved, "updated_at": utcnow()},
            "$unset": {"survivor_snapshot.password_hash": "", "absorbed_snapshot.password_hash": ""},
        },
    )


async def stalled_merges(before: datetime) -> list[dict]:
    return await mongo.account_merges().find({"status": MergeStatus.RUNNING, "updated_at": {"$lt": before}}).to_list(None)
