"""Board items. Native items live only here; tracker items are changed in the tracker first.

A tracker write goes to the provider before anything is stored: if it refuses, nothing
changes here and the error reaches the board. On success the item is re-read from the
tracker, so the board shows exactly what the tracker now has.
"""

import re
import uuid
from datetime import timedelta

from app.auth import usernames
from app.auth.repository import get_user
from app.boards import repository as boards
from app.boards import service as board_service
from app.boards import workflow
from app.connectors import registry
from app.work_item_types import service as work_item_types
from app.connectors import repository as connectors
from app.connectors.service import can_write
from app.core.constants import NATIVE_SOURCE, Role, SprintState, StatusCategory
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.sprints import repository as sprints
from app.sprints import service as sprint_service
from app.work_items import repository
from app.workspaces import repository as workspaces
from app.workspaces.service import rank

# below this gap between neighbours a midpoint loses precision, so the column is renumbered
MIN_RANK_GAP = 1e-6


def to_work_item(doc: dict, writable: set[str]) -> dict:
    return {
        **doc,
        "id": doc["_id"],
        # items stored before boards sit in their category's default column
        "column_id": doc.get("column_id", doc["status_category"]),
        "sprint_id": doc.get("sprint_id"),
        "editable": doc["source"] == NATIVE_SOURCE or doc["source"] in writable,
    }


def _scope(board: dict) -> dict:
    """The items on a board."""
    return {"workspace_id": board["workspace_id"], "source": board["source"], "target_key": board["target_key"]}


def _column(board: dict, column_id: str) -> dict:
    column = next((c for c in board["columns"] if c["id"] == column_id), None)
    if column is None:
        raise NotFoundError("no such column on this board", error_code="column_not_found")
    return column


async def _writable_sources(workspace_id: str) -> set[str]:
    return {c["provider"] for c in await connectors.list_connections(workspace_id) if can_write(c)}


async def _visibility(workspace_id: str) -> dict:
    """Native items, plus tracker items whose target is still connected."""
    by_provider: dict[str, list[str]] = {}
    for target in await connectors.list_targets(workspace_id):
        if registry.has_work_items(target["provider"]):
            by_provider.setdefault(target["provider"], []).append(target["key"])
    return {"$or": [{"source": NATIVE_SOURCE}, *({"source": p, "target_key": {"$in": keys}} for p, keys in by_provider.items())]}


#: the lane of items with no assignee (or no priority)
NO_LANE = "none"
LANE_FIELDS = {"assignee": "assignee.id", "priority": "priority"}


async def _query(
    workspace_id: str, *, board_id: str | None = None, column_id: str | None = None, sprint: str | None = None,
    done_within_days: int | None = None, hide_epics: bool = False, open_only: bool = False, source: str | None = None,
    status_category: str | None = None, target_key: str | None = None, assignee_id: str | None = None,
    priority: str | None = None, q: str | None = None, key: str | None = None,
) -> dict | None:
    """The Mongo filter for a board's items; None when nothing can match (no active sprint for "current")."""
    query: dict = {"workspace_id": workspace_id, **await _visibility(workspace_id)}
    conditions: list[dict] = []
    if board_id:
        board = await board_service.get_visible(workspace_id, board_id)
        query.update(_scope(board))
        # what the tracker's own board doesn't show isn't on ours either
        query["tracker_hidden"] = {"$ne": True}
        if sprint:
            sprint_id = await _sprint_filter(board, sprint)
            if sprint_id is _NO_SPRINT:
                return None
            query["sprint_id"] = sprint_id
    if done_within_days:
        since = utcnow() - timedelta(days=done_within_days)
        conditions.append({"$or": [{"status_category": {"$ne": StatusCategory.DONE}}, {"status_changed_at": {"$gte": since}}]})
    if hide_epics:
        query["issue_type"] = {"$ne": "Epic"}
    if open_only:
        conditions.append({"status_category": {"$ne": StatusCategory.DONE}})
    filters = {
        "column_id": column_id, "source": source, "status_category": status_category, "target_key": target_key, "key": key,
    }
    query.update({k: v for k, v in filters.items() if v is not None})
    # "none" is the lane of items with no assignee (or priority)
    for field, value in ((LANE_FIELDS["assignee"], assignee_id), (LANE_FIELDS["priority"], priority)):
        if value is None:
            continue
        if value != NO_LANE:
            query[field] = value
        else:
            # no assignee is stored as `assignee: null`; match that as well as a missing id
            parent = field.split(".")[0]
            conditions.append({"$or": [{parent: None}, {field: None}]})
    if q:
        conditions.append({"$or": [
            {"title": {"$regex": re.escape(q), "$options": "i"}}, {"key": {"$regex": re.escape(q), "$options": "i"}},
        ]})
    if conditions:
        query["$and"] = conditions
    return query


async def list_items(
    workspace_id: str, *, page: int = 1, page_size: int = 100, offset: int | None = None, **filters,
) -> tuple[list[dict], int]:
    query = await _query(workspace_id, **filters)
    if query is None:
        return [], 0
    skip = offset if offset is not None else (page - 1) * page_size
    docs, total = await repository.page(query, skip, page_size)
    writable = await _writable_sources(workspace_id)
    return [to_work_item(d, writable) for d in docs], total


async def lanes(workspace_id: str, by: str, **filters) -> list[dict]:
    """The swimlanes of a board view: each assignee (or priority) with items in it, and how many."""
    query = await _query(workspace_id, **filters)
    if query is None:
        return []
    field = LANE_FIELDS[by]
    groups = await repository.group_counts(query, field, label_field="assignee.name" if by == "assignee" else None)
    return [
        {"key": group["_id"] or NO_LANE, "label": group.get("label") or group["_id"], "total": group["total"]}
        for group in groups
    ]


# a board with no active sprint shows nothing for "current"
_NO_SPRINT = object()


async def _sprint_filter(board: dict, sprint: str):
    """The `sprint_id` to match: "backlog" is none, "current" the active sprint's."""
    if sprint == "backlog":
        return None
    if sprint == "current":
        active = await sprints.active(board["_id"])
        return active["_id"] if active else _NO_SPRINT
    found = await sprints.get(board["workspace_id"], sprint)
    if found is None or found["board_id"] != board["_id"]:
        raise NotFoundError("sprint not found", error_code="sprint_not_found")
    return found["_id"]


async def get_visible(workspace_id: str, item_id: str) -> dict:
    item = await repository.get(workspace_id, item_id)
    if item and (item["source"] == NATIVE_SOURCE or await connectors.find_target(workspace_id, item["source"], item["target_key"])):
        return item
    raise NotFoundError("work item not found", error_code="work_item_not_found")


async def writable_connection(workspace_id: str, provider: str) -> dict:
    conn = await connectors.active_connection(workspace_id, provider)
    if conn is None:
        raise ConflictError(f"{provider} is not connected", error_code=f"{provider}_not_connected")
    if not can_write(conn):
        raise ConflictError(
            f"reconnect {provider} to allow changes from Overwatch", error_code=f"{provider}_reconnect_required",
        )
    return conn


async def _assignee(workspace_id: str, user_id: str | None) -> dict | None:
    if user_id is None:
        return None
    user = await workspaces.membership(workspace_id, user_id) and await get_user(user_id)
    if not user:
        raise NotFoundError("assignee is not a member of this workspace", error_code="member_not_found")
    return {"id": user_id, "name": usernames.display_name(user)}


async def _refresh(workspace_id: str, conn: dict, target_key: str, external_id: str) -> dict:
    adapter = registry.work_item_source(conn["provider"])
    board = await boards.ensure(workspace_id, conn["provider"], target_key)
    raw = await adapter.fetch(registry.auth_for(conn), conn, external_id, board)
    fields = board_service.place(board, adapter.normalise(conn, target_key, raw, board))
    fields["type_id"] = await work_item_types.type_for(workspace_id, conn["provider"], fields.get("issue_type"), {})
    await repository.upsert_synced(workspace_id, await sprint_service.apply_tracker_sprint(board, fields))
    return await repository.find_external(workspace_id, conn["provider"], external_id)


async def _result(workspace_id: str, item_id: str) -> dict:
    return to_work_item(await repository.get(workspace_id, item_id), await _writable_sources(workspace_id))


async def create(member: dict, body) -> dict:
    workspace_id = member["workspace_id"]
    board = await board_service.get_visible(workspace_id, body.board_id)
    column = _column(board, body.column_id or board["columns"][0]["id"])
    sprint = await _open_sprint(board, body.sprint_id)
    if board["source"] != NATIVE_SOURCE:
        conn = await writable_connection(workspace_id, board["source"])
        adapter, auth = registry.work_item_source(board["source"]), registry.auth_for(conn)
        fields = {"title": body.title, "description": body.description}
        if body.assignee_id:
            fields["assignee_id"] = body.assignee_id
        if body.type_id:
            fields["issue_type"] = (await work_item_types.get(workspace_id, body.type_id))["name"]
        external_id = await adapter.create(auth, conn, board["target_key"], fields)
        item = await _refresh(workspace_id, conn, board["target_key"], external_id)
        # a new issue starts in the workflow's first status; move it if that's not the column asked for
        if item.get("column_id") != column["id"]:
            await adapter.move(auth, conn, external_id, column)
            item = await _refresh(workspace_id, conn, board["target_key"], external_id)
        if sprint:
            await adapter.set_sprint(auth, conn, [external_id], sprint["external_id"])
            item = await _refresh(workspace_id, conn, board["target_key"], external_id)
        await repository.update(item["_id"], {"created_by": member["user_id"]})
        return await _result(workspace_id, item["_id"])

    now = utcnow()
    key = await repository.next_native_key(workspace_id)
    doc = {
        "_id": uuid.uuid4().hex, "workspace_id": workspace_id, "source": NATIVE_SOURCE, "external_id": key, "key": key,
        "target_key": None, "title": body.title, "description": body.description, "status": None,
        "status_category": column["status_category"], "column_id": column["id"], "rank": now.timestamp(),
        "sprint_id": sprint and sprint["_id"], "status_changed_at": now, "description_md": body.description,
        "reporter": await _assignee(workspace_id, member["user_id"]), "labels": [], "due_date": None, "story_points": None,
        "assignee": await _assignee(workspace_id, body.assignee_id), "priority": None, "issue_type": None,
        "type_id": (await work_item_types.get(workspace_id, body.type_id))["_id"] if body.type_id else await work_item_types.default_type_id(workspace_id),
        "external_url": None, "created_by": member["user_id"], "created_at": now, "updated_at": now,
    }
    await repository.insert(doc)
    return to_work_item(doc, set())


async def update(member: dict, item_id: str, body) -> dict:
    workspace_id = member["workspace_id"]
    item = await get_visible(workspace_id, item_id)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("due_date"):
        # Mongo has no date-only type, and trackers take the ISO date
        fields["due_date"] = fields["due_date"].isoformat()
    if "type_id" in fields:
        chosen = await work_item_types.get(workspace_id, fields["type_id"])
        if item["source"] != NATIVE_SOURCE:
            fields.pop("type_id")
            fields["issue_type_id"] = await _tracker_type_id(workspace_id, item, chosen)
    if item["source"] == NATIVE_SOURCE:
        if "assignee_id" in fields:
            fields["assignee"] = await _assignee(workspace_id, fields.pop("assignee_id"))
        if "description" in fields:
            # native descriptions are written as Markdown, so they display as they were written
            fields["description_md"] = fields["description"]
        await repository.update(item_id, fields)
    elif fields:
        conn = await writable_connection(workspace_id, item["source"])
        board = await boards.ensure(workspace_id, item["source"], item["target_key"])
        await registry.work_item_source(item["source"]).update(registry.auth_for(conn), conn, item["external_id"], fields, board)
        await _refresh(workspace_id, conn, item["target_key"], item["external_id"])
    return await _result(workspace_id, item_id)


async def _tracker_type_id(workspace_id: str, item: dict, chosen: dict) -> str:
    """The tracker's id for the type a tracker item is changed to; only types it can change to in place."""
    conn = await writable_connection(workspace_id, item["source"])
    allowed = await registry.work_item_source(item["source"]).allowed_types(registry.auth_for(conn), conn, item["external_id"])
    match = next((t for t in allowed if work_item_types.name_key(t["name"] or "") == chosen["name_key"]), None)
    if match is None:
        raise ConflictError(f"Change this item to {chosen['name']} in its tracker", error_code="type_change_in_tracker")
    return match["id"]


async def allowed_types(member: dict, item_id: str) -> dict:
    """The types an item can be changed to: any for native items, what its tracker allows in place otherwise."""
    workspace_id = member["workspace_id"]
    item = await get_visible(workspace_id, item_id)
    if item["source"] == NATIVE_SOURCE:
        return {"type_ids": None}
    conn = await writable_connection(workspace_id, item["source"])
    allowed = await registry.work_item_source(item["source"]).allowed_types(registry.auth_for(conn), conn, item["external_id"])
    cache: dict[str, str | None] = {}
    ids = [await work_item_types.type_for(workspace_id, item["source"], t["name"], cache) for t in allowed]
    return {"type_ids": [type_id for type_id in ids if type_id]}


async def _rank_between(scope: dict, column_id: str, before_id: str | None, after_id: str | None, item_id: str) -> float:
    """A rank between the cards it was dropped between.

    With one neighbour, the other is whatever actually follows (or precedes) it in the column,
    not just what the board had loaded: a partly loaded or filtered column must not let a drop
    jump past items the user couldn't see.
    """
    ranks = await repository.ranks(scope["workspace_id"], [i for i in (before_id, after_id) if i])
    before, after = ranks.get(before_id), ranks.get(after_id)
    if before is not None and after is None:
        after = await repository.adjacent_rank(scope, column_id, before, after=True, exclude_id=item_id)
    elif after is not None and before is None:
        before = await repository.adjacent_rank(scope, column_id, after, after=False, exclude_id=item_id)
    if before is not None and after is not None:
        if abs(after - before) < MIN_RANK_GAP:
            await repository.renumber_column(scope, column_id)
            return await _rank_between(scope, column_id, before_id, after_id, item_id)
        return (before + after) / 2
    if before is not None:
        return before + 1
    if after is not None:
        return after - 1
    return utcnow().timestamp()


async def move(member: dict, item_id: str, body) -> dict:
    workspace_id = member["workspace_id"]
    item = await get_visible(workspace_id, item_id)
    board = await boards.ensure(workspace_id, item["source"], item["target_key"])
    column = _column(board, body.column_id)
    fields: dict = {}
    current = to_work_item(item, set())["column_id"]
    if item["source"] == NATIVE_SOURCE:
        if column["id"] != current and column["id"] not in workflow.native_targets(board, current):
            raise ConflictError(_blocked(board, current, column), error_code="transition_not_allowed")
        fields = {"column_id": column["id"], "status_category": column["status_category"]}
        if column["status_category"] != item["status_category"]:
            fields["status_changed_at"] = utcnow()
    elif column["id"] != current or (body.status_id and body.status_id != item.get("status_id")):
        conn = await writable_connection(workspace_id, item["source"])
        await registry.work_item_source(item["source"]).move(registry.auth_for(conn), conn, item["external_id"], column, body.status_id)
        # the column comes from the status the tracker now reports
        await _refresh(workspace_id, conn, item["target_key"], item["external_id"])
    fields["rank"] = await _rank_between(_scope(board), column["id"], body.before_id, body.after_id, item_id)
    await repository.update(item_id, fields)
    return await _result(workspace_id, item_id)


def _blocked(board: dict, from_column_id: str, column: dict) -> str:
    """Why a native card can't move there, and the column to move it to first when there's a way."""
    names = {c["id"]: c["name"] for c in board["columns"]}
    step = workflow.first_step(board, from_column_id, column["id"])
    if step:
        return f"Move it to {names[step]} first: this board's workflow doesn't go from {names.get(from_column_id)} to {column['name']}."
    return f"This board's workflow doesn't allow moving from {names.get(from_column_id)} to {column['name']}."


async def allowed_moves(member: dict, item_id: str) -> dict:
    """Where an item can move from where it is: its board's rules, or what its tracker allows it now."""
    workspace_id = member["workspace_id"]
    item = await get_visible(workspace_id, item_id)
    board = await boards.ensure(workspace_id, item["source"], item["target_key"])
    current = to_work_item(item, set())["column_id"]
    if item["source"] == NATIVE_SOURCE:
        targets = [{"column_id": column_id} for column_id in workflow.native_targets(board, current)]
        rules = any(c.get("next") is not None for c in board["columns"])
        return {"basis": "board" if rules else "open", "targets": targets}
    conn = await writable_connection(workspace_id, item["source"])
    reachable = await registry.work_item_source(item["source"]).allowed_statuses(registry.auth_for(conn), conn, item["external_id"])
    if reachable is None:
        return {"basis": "open", "targets": [{"column_id": c["id"]} for c in board["columns"] if c["id"] != current]}
    return {"basis": "live", "targets": workflow.tracker_targets(board, {**item, "column_id": current}, reachable)}


async def _open_sprint(board: dict, sprint_id: str | None) -> dict | None:
    if sprint_id is None:
        return None
    sprint = await sprints.get(board["workspace_id"], sprint_id)
    if sprint is None or sprint["board_id"] != board["_id"] or sprint["state"] == SprintState.CLOSED:
        raise NotFoundError("no such open sprint on this board", error_code="sprint_not_found")
    return sprint


async def set_sprint(member: dict, item_id: str, body) -> dict:
    """Move an item into an open sprint of its board, or to the backlog; the tracker first."""
    workspace_id = member["workspace_id"]
    item = await get_visible(workspace_id, item_id)
    board = await boards.ensure(workspace_id, item["source"], item["target_key"])
    sprint = await _open_sprint(board, body.sprint_id)
    if (sprint and sprint["_id"]) != item.get("sprint_id") and item["source"] != NATIVE_SOURCE:
        adapter, auth, conn = await sprint_service.tracker_for(board)
        await adapter.set_sprint(auth, conn, [item["external_id"]], sprint and sprint["external_id"])
    rank = await _rank_between(_scope(board), to_work_item(item, set())["column_id"], body.before_id, body.after_id, item_id)
    await repository.update(item_id, {"sprint_id": sprint and sprint["_id"], "rank": rank})
    return await _result(workspace_id, item_id)


async def delete(member: dict, item_id: str) -> None:
    workspace_id = member["workspace_id"]
    item = await get_visible(workspace_id, item_id)
    if item["source"] != NATIVE_SOURCE:
        # it's deleted in the tracker under the connecting admin's account, and can't be undone
        if rank(member["role"]) < rank(Role.ADMIN):
            raise ForbiddenError(f"only admins delete {item['source']} items", error_code="insufficient_role")
        conn = await writable_connection(workspace_id, item["source"])
        await registry.work_item_source(item["source"]).delete(registry.auth_for(conn), conn, item["external_id"])
    await repository.delete(item_id)
