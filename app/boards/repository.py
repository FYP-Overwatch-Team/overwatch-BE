"""Boards (the `boards` collection): one per tracker target plus a native one per workspace."""

import uuid

from pymongo import ReturnDocument

from app.core.constants import NATIVE_SOURCE, BoardKind, ColumnMode, ColumnPrompt, StatusCategory
from app.core.time import utcnow
from app.db import mongo

# ids are the categories, so an item without a `column_id` falls into its category's column
DEFAULT_COLUMNS = [
    {"id": StatusCategory.TODO.value, "name": "To do", "status_category": StatusCategory.TODO.value, "status_ids": []},
    {"id": StatusCategory.IN_PROGRESS.value, "name": "In progress", "status_category": StatusCategory.IN_PROGRESS.value, "status_ids": []},
    {"id": StatusCategory.DONE.value, "name": "Done", "status_category": StatusCategory.DONE.value, "status_ids": []},
]


async def ensure(workspace_id: str, source: str, target_key: str | None, name: str | None = None) -> dict:
    """The board for a source and target, created with the default columns the first time.

    Only a given `name` renames it; without one an existing board is just read.
    """
    now = utcnow()
    default_name = "Overwatch" if source == NATIVE_SOURCE else target_key
    update: dict = {
        "$setOnInsert": {
            "_id": uuid.uuid4().hex,
            "kind": BoardKind.KANBAN,
            # the tracker's board id and field ids, read on the target's full syncs
            "external_id": None,
            "fields": None,
            "column_mode": ColumnMode.DEFAULT,
            "columns": DEFAULT_COLUMNS,
            "columns_hash": None,
            "column_prompt": {"status": ColumnPrompt.NONE, "columns": [], "hash": None},
            "created_at": now,
            **({} if name else {"name": default_name, "updated_at": now}),
        },
    }
    if name:
        update["$set"] = {"name": name, "updated_at": now}
    return await mongo.boards().find_one_and_update(
        {"workspace_id": workspace_id, "source": source, "target_key": target_key},
        update,
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def find(workspace_id: str, source: str, target_key: str | None) -> dict | None:
    return await mongo.boards().find_one({"workspace_id": workspace_id, "source": source, "target_key": target_key})


async def get(workspace_id: str, board_id: str) -> dict | None:
    return await mongo.boards().find_one({"_id": board_id, "workspace_id": workspace_id})


async def update(board_id: str, fields: dict) -> dict:
    return await mongo.boards().find_one_and_update(
        {"_id": board_id}, {"$set": {**fields, "updated_at": utcnow()}}, return_document=ReturnDocument.AFTER,
    )


async def place_items(board: dict) -> None:
    """Recompute `column_id` for every item on a tracker board after its columns changed."""
    scope = {"workspace_id": board["workspace_id"], "source": board["source"], "target_key": board["target_key"]}
    if board["column_mode"] == ColumnMode.DEFAULT:
        for column in board["columns"]:
            await mongo.work_items().update_many({**scope, "status_category": column["status_category"]}, {"$set": {"column_id": column["id"]}})
        return
    await mongo.work_items().update_many(scope, {"$set": {"column_id": None}})
    for column in board["columns"]:
        await mongo.work_items().update_many({**scope, "status_id": {"$in": column["status_ids"]}}, {"$set": {"column_id": column["id"]}})
