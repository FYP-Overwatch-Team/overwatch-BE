"""A workspace's work item types: Task, Bug and Research to start with, the workspace's own, and its trackers'.

Defaults are created the first time a workspace's types are read, so no workspace needs seeding.
A tracker's types are matched by name and created when first seen; the tracker owns their names.
"""

import uuid

from pymongo.errors import DuplicateKeyError

from app.core.constants import NATIVE_SOURCE, ColumnColor, WorkItemIcon
from app.core.exceptions import ConflictError, NotFoundError
from app.core.time import utcnow
from app.db import mongo

TASK = "task"
DEFAULTS = (
    ("Task", WorkItemIcon.CHECK_SQUARE, ColumnColor.BLUE),
    ("Bug", WorkItemIcon.BUG, ColumnColor.RED),
    ("Research", WorkItemIcon.FLASK, ColumnColor.PURPLE),
)
# how a tracker's type looks when first seen, by its name
TRACKER_LOOKS = {
    "task": (WorkItemIcon.CHECK_SQUARE, ColumnColor.BLUE),
    "bug": (WorkItemIcon.BUG, ColumnColor.RED),
    "story": (WorkItemIcon.BOOKMARK, ColumnColor.GREEN),
    "epic": (WorkItemIcon.LIGHTNING, ColumnColor.PURPLE),
    "sub-task": (WorkItemIcon.TREE, ColumnColor.SLATE),
    "subtask": (WorkItemIcon.TREE, ColumnColor.SLATE),
    "spike": (WorkItemIcon.FLASK, ColumnColor.AMBER),
    "research": (WorkItemIcon.FLASK, ColumnColor.PURPLE),
}


def name_key(name: str) -> str:
    return name.strip().lower()


def to_type(doc: dict) -> dict:
    return {**doc, "id": doc["_id"]}


async def _upsert(workspace_id: str, name: str, icon: str, color: str, source: str, rank: float) -> None:
    now = utcnow()
    try:
        await mongo.work_item_types().update_one(
            {"workspace_id": workspace_id, "name_key": name_key(name)},
            {"$setOnInsert": {
                "_id": uuid.uuid4().hex, "name": name.strip(), "icon": icon, "color": color, "source": source,
                "rank": rank, "created_at": now,
            }},
            upsert=True,
        )
    except DuplicateKeyError:
        pass  # created by a concurrent request


async def _ensure_defaults(workspace_id: str) -> None:
    if await mongo.work_item_types().count_documents({"workspace_id": workspace_id}, limit=1):
        return
    for rank, (name, icon, color) in enumerate(DEFAULTS, start=1):
        await _upsert(workspace_id, name, icon, color, NATIVE_SOURCE, float(rank))


async def list_types(workspace_id: str) -> list[dict]:
    await _ensure_defaults(workspace_id)
    cursor = mongo.work_item_types().find({"workspace_id": workspace_id}).sort([("rank", 1), ("_id", 1)])
    return [to_type(doc) async for doc in cursor]


async def default_type_id(workspace_id: str) -> str:
    """Task, or the first type when the workspace has removed it."""
    types = await list_types(workspace_id)
    return next((t["_id"] for t in types if t["name_key"] == TASK), types[0]["_id"])


async def type_for(workspace_id: str, source: str, name: str | None, cache: dict[str, str | None]) -> str | None:
    """The type a tracker item of type `name` has here, created the first time that name is seen."""
    if not name:
        return None
    key = name_key(name)
    if key not in cache:
        found = await mongo.work_item_types().find_one({"workspace_id": workspace_id, "name_key": key}, {"_id": 1})
        if found is None:
            await _ensure_defaults(workspace_id)
            icon, color = TRACKER_LOOKS.get(key, (WorkItemIcon.CIRCLE, ColumnColor.GRAY))
            await _upsert(workspace_id, name, icon, color, source, utcnow().timestamp())
            found = await mongo.work_item_types().find_one({"workspace_id": workspace_id, "name_key": key}, {"_id": 1})
        cache[key] = found and found["_id"]
    return cache[key]


async def get(workspace_id: str, type_id: str) -> dict:
    doc = await mongo.work_item_types().find_one({"_id": type_id, "workspace_id": workspace_id})
    if doc is None:
        raise NotFoundError("no such work item type", error_code="work_item_type_not_found")
    return doc


async def create(workspace_id: str, body) -> dict:
    await _ensure_defaults(workspace_id)
    if await mongo.work_item_types().find_one({"workspace_id": workspace_id, "name_key": name_key(body.name)}):
        raise ConflictError("a type with that name exists", error_code="work_item_type_exists")
    await _upsert(workspace_id, body.name, body.icon, body.color, NATIVE_SOURCE, utcnow().timestamp())
    return to_type(await mongo.work_item_types().find_one({"workspace_id": workspace_id, "name_key": name_key(body.name)}))


async def update(workspace_id: str, type_id: str, body) -> dict:
    current = await get(workspace_id, type_id)
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    if "name" in fields:
        if current["source"] != NATIVE_SOURCE and name_key(fields["name"]) != current["name_key"]:
            raise ConflictError("this type's name comes from its tracker", error_code="work_item_type_from_tracker")
        clash = await mongo.work_item_types().find_one(
            {"workspace_id": workspace_id, "name_key": name_key(fields["name"]), "_id": {"$ne": type_id}},
        )
        if clash:
            raise ConflictError("a type with that name exists", error_code="work_item_type_exists")
        fields.update(name=fields["name"].strip(), name_key=name_key(fields["name"]))
    if fields:
        await mongo.work_item_types().update_one({"_id": type_id}, {"$set": fields})
    return to_type(await get(workspace_id, type_id))


async def delete(workspace_id: str, type_id: str, reassign_to: str | None) -> None:
    """Remove a type; its items take `reassign_to`, required while any have it."""
    await get(workspace_id, type_id)
    if await mongo.work_item_types().count_documents({"workspace_id": workspace_id}) <= 1:
        raise ConflictError("a workspace keeps at least one type", error_code="last_work_item_type")
    in_use = await mongo.work_items().count_documents({"workspace_id": workspace_id, "type_id": type_id}, limit=1)
    if in_use:
        if not reassign_to or reassign_to == type_id:
            raise ConflictError("choose a type for its items", error_code="work_item_type_in_use")
        await get(workspace_id, reassign_to)
        await mongo.work_items().update_many({"workspace_id": workspace_id, "type_id": type_id}, {"$set": {"type_id": reassign_to}})
    await mongo.work_item_types().delete_one({"_id": type_id})
