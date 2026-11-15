"""Docs tree nodes (the `documents` collection). Reads of the tree leave archived nodes out."""

from pymongo import ReturnDocument, UpdateOne

from app.core.constants import DocPushState, DocumentKind
from app.db import mongo

# tree rows never carry the content, which is the bulk of a page
SUMMARY = {"content": 0, "content_md": 0}


def _live(workspace_id: str, **query) -> dict:
    return {"workspace_id": workspace_id, "archived_at": None, **query}


async def get(workspace_id: str, node_id: str, *, summary: bool = False) -> dict | None:
    return await mongo.documents().find_one(_live(workspace_id, _id=node_id), SUMMARY if summary else None)


async def children(workspace_id: str, parent_id: str | None, skip: int, limit: int) -> tuple[list[dict], int]:
    query = _live(workspace_id, parent_id=parent_id)
    total = await mongo.documents().count_documents(query)
    cursor = mongo.documents().find(query, SUMMARY).sort([("rank", 1), ("_id", 1)]).skip(skip).limit(limit)
    return [doc async for doc in cursor], total


async def titles(workspace_id: str, node_ids: list[str]) -> dict[str, str]:
    cursor = mongo.documents().find({"workspace_id": workspace_id, "_id": {"$in": node_ids}}, {"title": 1})
    return {doc["_id"]: doc["title"] async for doc in cursor}


async def insert(doc: dict) -> None:
    await mongo.documents().insert_one(doc)


async def update(workspace_id: str, node_id: str, fields: dict, *, source: str, **query) -> dict | None:
    return await mongo.documents().find_one_and_update(
        _live(workspace_id, _id=node_id, source=source, **query), {"$set": fields}, return_document=ReturnDocument.AFTER,
    )


async def save_content(workspace_id: str, node_id: str, base_version: int, fields: dict, *, source: str, **query) -> dict | None:
    """Write a page's content edited from `base_version`; None when it changed in between, or can't take content."""
    return await mongo.documents().find_one_and_update(
        _live(workspace_id, _id=node_id, source=source, kind=DocumentKind.PAGE, version=base_version, **query),
        {"$set": fields, "$inc": {"version": 1}},
        return_document=ReturnDocument.AFTER,
    )


async def add_children(node_id: str, count: int) -> None:
    await mongo.documents().update_one({"_id": node_id}, {"$inc": {"child_count": count}})


async def ranks(workspace_id: str, node_ids: list[str]) -> dict[str, float]:
    cursor = mongo.documents().find(_live(workspace_id, _id={"$in": node_ids}), {"rank": 1})
    return {doc["_id"]: doc["rank"] async for doc in cursor}


async def adjacent_rank(workspace_id: str, parent_id: str | None, rank: float | None, *, after: bool, exclude_id: str | None) -> float | None:
    """The next sibling's rank below (`after`) or above `rank`; with no rank, the last (or first) one's."""
    query = _live(workspace_id, parent_id=parent_id)
    if rank is not None:
        query["rank"] = {"$gt" if after else "$lt": rank}
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    doc = await mongo.documents().find_one(query, {"rank": 1}, sort=[("rank", 1 if after else -1)])
    return doc and doc["rank"]


async def renumber(workspace_id: str, parent_id: str | None) -> None:
    """Spread a folder's ranks out again (1, 2, 3, ...) once midpoints get too close."""
    cursor = mongo.documents().find(_live(workspace_id, parent_id=parent_id), {"_id": 1}).sort([("rank", 1), ("_id", 1)])
    ids = [doc["_id"] async for doc in cursor]
    if ids:
        await mongo.documents().bulk_write([
            UpdateOne({"_id": node_id}, {"$set": {"rank": float(position)}}) for position, node_id in enumerate(ids, start=1)
        ])



async def child_ids(workspace_id: str, parent_id: str) -> list[str]:
    cursor = mongo.documents().find(_live(workspace_id, parent_id=parent_id), {"_id": 1}).sort([("rank", 1), ("_id", 1)])
    return [doc["_id"] async for doc in cursor]


async def deepest(workspace_id: str, node_id: str) -> int:
    """The most ancestors any live node under `node_id` has; 0 when there are none."""
    pipeline = [
        {"$match": _live(workspace_id, ancestor_ids=node_id)},
        {"$group": {"_id": None, "deepest": {"$max": {"$size": "$ancestor_ids"}}}},
    ]
    result = [doc async for doc in mongo.documents().aggregate(pipeline)]
    return result[0]["deepest"] if result else 0


async def place_children(child_ids: list[str], parent_id: str | None, first_rank: float) -> None:
    """Make these nodes `parent_id`'s last children, in the order given."""
    await mongo.documents().bulk_write([
        UpdateOne({"_id": node_id}, {"$set": {"parent_id": parent_id, "rank": first_rank + offset}}) for offset, node_id in enumerate(child_ids)
    ])


async def rebase(query: dict, old_prefix_len: int, new_prefix: list[str], max_depth: int) -> None:
    """Swap the first `old_prefix_len` ancestors of every matching node for `new_prefix`, in one write.

    A slice, not a pull: the new prefix may share ancestors with the old one (moving up a level).
    """
    rest = {"$slice": ["$ancestor_ids", old_prefix_len, max_depth]}
    await mongo.documents().update_many(query, [{"$set": {"ancestor_ids": {"$concatArrays": [new_prefix, rest]}}}])


async def archive(workspace_id: str, node_id: str, *, with_subtree: bool, fields: dict) -> None:
    """Archive a node and, with `with_subtree`, every live node under it, as one group."""
    group = {"$or": [{"_id": node_id}, {"ancestor_ids": node_id}]} if with_subtree else {"_id": node_id}
    await mongo.documents().update_many(_live(workspace_id, **group), {"$set": {**fields, "archive_root_id": node_id}})
    await mongo.documents().update_one({"_id": node_id}, {"$set": {"archived_root": True}})


async def archived(workspace_id: str, skip: int, limit: int) -> tuple[list[dict], int]:
    query = {"workspace_id": workspace_id, "archived_root": True}
    total = await mongo.documents().count_documents(query)
    cursor = mongo.documents().find(query, SUMMARY).sort([("archived_at", -1), ("_id", 1)]).skip(skip).limit(limit)
    return [doc async for doc in cursor], total


async def group_sizes(workspace_id: str, root_ids: list[str]) -> dict[str, int]:
    pipeline = [
        {"$match": {"workspace_id": workspace_id, "archive_root_id": {"$in": root_ids}}},
        {"$group": {"_id": "$archive_root_id", "size": {"$sum": 1}}},
    ]
    return {doc["_id"]: doc["size"] async for doc in mongo.documents().aggregate(pipeline)}


async def get_archived_root(workspace_id: str, node_id: str) -> dict | None:
    return await mongo.documents().find_one({"_id": node_id, "workspace_id": workspace_id, "archived_root": True}, SUMMARY)


async def restore(workspace_id: str, root_id: str) -> None:
    await mongo.documents().update_many(
        {"workspace_id": workspace_id, "archive_root_id": root_id},
        {"$set": {"archived_at": None, "archived_by": None, "archive_root_id": None, "archived_root": False}},
    )


async def mirror(workspace_id: str, source: str, target_key: str) -> list[dict]:
    """Every node mirrored from one synced target, archived ones too, without content."""
    query = {"workspace_id": workspace_id, "source": source, "target_key": target_key}
    return [doc async for doc in mongo.documents().find(query, {**SUMMARY, "conflict_content": 0})]


async def write_mirror(ops: list) -> None:
    if ops:
        await mongo.documents().bulk_write(ops, ordered=False)


async def save_synced(node_id: str, expected: dict, fields: dict) -> bool:
    """Store content read from the source, unless the page changed here since it was read (`expected`)."""
    result = await mongo.documents().update_one({"_id": node_id, **expected}, {"$set": fields, "$inc": {"version": 1}})
    return result.modified_count > 0


async def archive_target(workspace_id: str, source: str, target_key: str, root_id: str, fields: dict) -> None:
    """Archive a target's whole mirror as one group under its root folder."""
    query = {"workspace_id": workspace_id, "source": source, "target_key": target_key, "archived_at": None}
    await mongo.documents().update_many(query, {"$set": {**fields, "archive_root_id": root_id}})
    await mongo.documents().update_one({"_id": root_id}, {"$set": {"archived_root": True}})


async def mirrored_pages(workspace_id: str, node_id: str, source: str) -> list[dict]:
    """A node and the live pages under it that came from `source`."""
    query = _live(workspace_id, source=source, kind=DocumentKind.PAGE, **{"$or": [{"_id": node_id}, {"ancestor_ids": node_id}]})
    return [doc async for doc in mongo.documents().find(query, {"external_id": 1})]


async def claim_push(now, stale_before) -> dict | None:
    """The next page due to go back to its source, marked as on its way; a push stuck past `stale_before` is retried."""
    for query in (
        {"push_state": DocPushState.PENDING, "push_due_at": {"$lte": now}},
        {"push_state": DocPushState.PUSHING, "push_started_at": {"$lt": stale_before}},
    ):
        doc = await mongo.documents().find_one_and_update(
            query, {"$set": {"push_state": DocPushState.PUSHING, "push_started_at": now}}, return_document=ReturnDocument.AFTER,
        )
        if doc:
            return doc
    return None


async def push_done(node_id: str, external_version: int, now) -> None:
    await mongo.documents().update_one({"_id": node_id}, {"$set": {"external_version": external_version, "synced_at": now}})
    # an edit saved while it was on its way is still pending, and goes next
    await mongo.documents().update_one(
        {"_id": node_id, "push_state": DocPushState.PUSHING}, {"$set": {"push_state": DocPushState.NONE, "push_error": None}},
    )


async def push_later(node_id: str, due, error: str) -> None:
    await mongo.documents().update_one(
        {"_id": node_id, "push_state": DocPushState.PUSHING},
        {"$set": {"push_state": DocPushState.PENDING, "push_due_at": due, "push_error": error}},
    )


async def resolve_conflict(workspace_id: str, node_id: str, base_version: int, fields: dict) -> dict | None:
    return await mongo.documents().find_one_and_update(
        _live(workspace_id, _id=node_id, push_state=DocPushState.CONFLICT, version=base_version),
        {"$set": {**fields, "conflict_content": None, "conflict_content_md": None, "conflict_title": None}, "$inc": {"version": 1}},
        return_document=ReturnDocument.AFTER,
    )


async def archive_group(workspace_id: str, root_id: str) -> list[dict]:
    query = {"workspace_id": workspace_id, "archive_root_id": root_id}
    return [doc async for doc in mongo.documents().find(query, {"source": 1, "external_id": 1, "kind": 1, "archived_by_sync": 1})]


async def delete_group(workspace_id: str, root_id: str) -> None:
    await mongo.documents().delete_many({"workspace_id": workspace_id, "archive_root_id": root_id})


def archived_before(before):
    """Archive roots older than `before`, across workspaces."""
    return mongo.documents().find({"archived_root": True, "archived_at": {"$lt": before}}, {"workspace_id": 1})
