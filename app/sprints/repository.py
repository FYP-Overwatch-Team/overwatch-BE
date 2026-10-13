"""Sprints (the `sprints` collection): a board's native sprints and mirrors of its tracker's."""

import uuid

from pymongo import ReturnDocument

from app.core.constants import SprintState
from app.core.time import utcnow
from app.db import mongo

OPEN = [SprintState.ACTIVE, SprintState.FUTURE]


async def upsert_tracker(board: dict, fields: dict) -> dict:
    """Store a tracker sprint as the tracker has it, keyed by its external id."""
    now = utcnow()
    return await mongo.sprints().find_one_and_update(
        {"workspace_id": board["workspace_id"], "source": board["source"], "external_id": fields["external_id"]},
        {
            "$set": {**fields, "board_id": board["_id"], "target_key": board["target_key"], "updated_at": now},
            "$setOnInsert": {"_id": uuid.uuid4().hex, "created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def close_missing(board: dict, open_external_ids: list[str]) -> None:
    """Tracker sprints that are no longer open there were completed (or deleted) there."""
    await mongo.sprints().update_many(
        {"board_id": board["_id"], "external_id": {"$type": "string", "$nin": open_external_ids}, "state": {"$in": OPEN}},
        {"$set": {"state": SprintState.CLOSED, "updated_at": utcnow()}},
    )


async def get(workspace_id: str, sprint_id: str) -> dict | None:
    return await mongo.sprints().find_one({"_id": sprint_id, "workspace_id": workspace_id})


async def list_open(board_id: str) -> list[dict]:
    cursor = mongo.sprints().find({"board_id": board_id, "state": {"$in": OPEN}})
    sprints = [doc async for doc in cursor]
    # the active sprint first, then future ones in the order they'll run
    return sorted(sprints, key=lambda s: (s["state"] != SprintState.ACTIVE, s.get("start_at") is None, s.get("start_at") or s["created_at"], s["created_at"]))


async def active(board_id: str) -> dict | None:
    return await mongo.sprints().find_one({"board_id": board_id, "state": SprintState.ACTIVE}, sort=[("start_at", 1)])


async def count(board_id: str) -> int:
    return await mongo.sprints().count_documents({"board_id": board_id})


async def insert(doc: dict) -> dict:
    now = utcnow()
    doc = {"_id": uuid.uuid4().hex, "created_at": now, "updated_at": now, **doc}
    await mongo.sprints().insert_one(doc)
    return doc


async def update(sprint_id: str, fields: dict) -> dict:
    return await mongo.sprints().find_one_and_update(
        {"_id": sprint_id}, {"$set": {**fields, "updated_at": utcnow()}}, return_document=ReturnDocument.AFTER,
    )
