"""Board items (the `work_items` collection), mirrored from trackers or created natively."""

import uuid

from pymongo import ReturnDocument

from app.core.time import utcnow
from app.db import mongo


async def upsert_synced(workspace_id: str, fields: dict) -> None:
    """Write a tracker item as the tracker has it; its board position is ours and kept."""
    now = utcnow()
    item = {k: v for k, v in fields.items() if k != "rank"}
    await mongo.work_items().update_one(
        {"workspace_id": workspace_id, "source": item["source"], "external_id": item["external_id"]},
        {
            "$set": {**item, "updated_at": now},
            "$setOnInsert": {"_id": uuid.uuid4().hex, "rank": fields["rank"], "created_by": None, "created_at": now},
        },
        upsert=True,
    )


async def sweep(workspace_id: str, source: str, target_key: str, keep_ids: list[str]) -> None:
    """Drop items the tracker no longer has."""
    await mongo.work_items().delete_many({
        "workspace_id": workspace_id, "source": source, "target_key": target_key, "external_id": {"$nin": keep_ids},
    })


async def get(workspace_id: str, item_id: str) -> dict | None:
    return await mongo.work_items().find_one({"_id": item_id, "workspace_id": workspace_id})


async def find_external(workspace_id: str, source: str, external_id: str) -> dict | None:
    return await mongo.work_items().find_one({"workspace_id": workspace_id, "source": source, "external_id": external_id})


async def page(query: dict, skip: int, limit: int, *, newest_first: bool = False) -> tuple[list[dict], int]:
    total = await mongo.work_items().count_documents(query)
    direction = -1 if newest_first else 1
    cursor = mongo.work_items().find(query, {"raw": 0}).sort([("rank", direction), ("_id", direction)]).skip(skip).limit(limit)
    return [doc async for doc in cursor], total


async def next_native_key(workspace_id: str) -> str:
    doc = await mongo.workspaces().find_one_and_update(
        {"_id": workspace_id}, {"$inc": {"work_item_seq": 1}}, return_document=ReturnDocument.AFTER,
    )
    return f"OW-{doc['work_item_seq']}"


async def insert(doc: dict) -> None:
    await mongo.work_items().insert_one(doc)


async def update(item_id: str, fields: dict) -> None:
    await mongo.work_items().update_one({"_id": item_id}, {"$set": {**fields, "updated_at": utcnow()}})


async def delete(item_id: str) -> None:
    await mongo.work_items().delete_one({"_id": item_id})


async def ranks(workspace_id: str, item_ids: list[str]) -> dict[str, float]:
    cursor = mongo.work_items().find({"workspace_id": workspace_id, "_id": {"$in": item_ids}}, {"rank": 1})
    return {doc["_id"]: doc["rank"] async for doc in cursor}


async def renumber_column(workspace_id: str, category: str) -> None:
    """Spread a column's ranks out again (1, 2, 3, ...) once midpoints get too close."""
    cursor = mongo.work_items().find(
        {"workspace_id": workspace_id, "status_category": category}, {"_id": 1},
    ).sort([("rank", 1), ("_id", 1)])
    for position, doc in enumerate([d async for d in cursor], start=1):
        await mongo.work_items().update_one({"_id": doc["_id"]}, {"$set": {"rank": float(position)}})
