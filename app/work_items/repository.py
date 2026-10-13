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
    """Drop items the tracker no longer has, with the comment records kept for them."""
    gone = {"workspace_id": workspace_id, "source": source, "target_key": target_key, "external_id": {"$nin": keep_ids}}
    gone_ids = [doc["_id"] async for doc in mongo.work_items().find(gone, {"_id": 1})]
    if not gone_ids:
        return
    await mongo.work_item_comments().delete_many({"workspace_id": workspace_id, "item_id": {"$in": gone_ids}})
    await mongo.work_items().delete_many({"_id": {"$in": gone_ids}})


async def get(workspace_id: str, item_id: str) -> dict | None:
    return await mongo.work_items().find_one({"_id": item_id, "workspace_id": workspace_id})


async def find_external(workspace_id: str, source: str, external_id: str) -> dict | None:
    return await mongo.work_items().find_one({"workspace_id": workspace_id, "source": source, "external_id": external_id})


async def page(query: dict, skip: int, limit: int) -> tuple[list[dict], int]:
    total = await mongo.work_items().count_documents(query)
    cursor = mongo.work_items().find(query, {"raw": 0}).sort([("rank", 1), ("_id", 1)]).skip(skip).limit(limit)
    return [doc async for doc in cursor], total


async def group_counts(query: dict, field: str, *, label_field: str | None = None) -> list[dict]:
    """How many items match per value of a field: [{_id, total, label?}]."""
    group: dict = {"_id": f"${field}", "total": {"$sum": 1}}
    if label_field:
        group["label"] = {"$first": f"${label_field}"}
    return [doc async for doc in mongo.work_items().aggregate([{"$match": query}, {"$group": group}])]


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
    await mongo.work_item_comments().delete_many({"item_id": item_id})


async def ranks(workspace_id: str, item_ids: list[str]) -> dict[str, float]:
    cursor = mongo.work_items().find({"workspace_id": workspace_id, "_id": {"$in": item_ids}}, {"rank": 1})
    return {doc["_id"]: doc["rank"] async for doc in cursor}


async def adjacent_rank(scope: dict, column_id: str | None, rank: float, *, after: bool, exclude_id: str) -> float | None:
    """The rank of the next item below (`after`) or above `rank` in a board column, leaving one item out."""
    doc = await mongo.work_items().find_one(
        {**scope, "column_id": column_id, "_id": {"$ne": exclude_id}, "rank": {"$gt" if after else "$lt": rank}},
        {"rank": 1},
        sort=[("rank", 1 if after else -1)],
    )
    return doc and doc["rank"]


async def renumber_column(scope: dict, column_id: str) -> None:
    """Spread a board column's ranks out again (1, 2, 3, ...) once midpoints get too close."""
    cursor = mongo.work_items().find({**scope, "column_id": column_id}, {"_id": 1}).sort([("rank", 1), ("_id", 1)])
    for position, doc in enumerate([d async for d in cursor], start=1):
        await mongo.work_items().update_one({"_id": doc["_id"]}, {"$set": {"rank": float(position)}})
