"""Sprints and the backlog.

Every board can plan in sprints. On the native board they live only here; on a tracker
board they are the tracker's, mirrored on each sync, and every change made here (create,
edit, start, complete, moving items in or out) is made in the tracker first.

An item's `sprint_id` is its open sprint; None is the backlog. Membership comes from the
item's own sprint field, so a grant without the tracker's sprint scope still sees which
sprint each item is in; only managing sprints needs that scope.
"""

from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.boards import repository as boards
from app.boards import service as board_service
from app.connectors import registry
from app.connectors import repository as connectors
from app.connectors.service import can_manage_sprints
from app.core.constants import NATIVE_SOURCE, BoardKind, Role, SprintState, StatusCategory
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.db import mongo
from app.sprints import repository
from app.workspaces.service import rank

DEFAULT_LENGTH = timedelta(days=14)


def to_sprint(doc: dict) -> dict:
    return {**doc, "id": doc["_id"]}


async def mirror(board: dict, sprints: list[dict]) -> None:
    """The tracker's open sprints for a board, as one sync read them."""
    for fields in sprints:
        await repository.upsert_tracker(board, fields)
    await repository.close_missing(board, [s["external_id"] for s in sprints])


async def apply_tracker_sprint(board: dict, fields: dict, seen: dict[str, str] | None = None) -> dict:
    """A synced item's fields with `tracker_sprint` turned into its local `sprint_id`.

    `seen` caches sprint ids within one sync, so a sprint shared by many items is stored once.
    """
    if "tracker_sprint" not in fields:
        return fields
    fields = dict(fields)
    sprint = fields.pop("tracker_sprint")
    if sprint is None:
        return {**fields, "sprint_id": None}
    if seen is not None and sprint["external_id"] in seen:
        return {**fields, "sprint_id": seen[sprint["external_id"]]}
    stored = await repository.upsert_tracker(board, sprint)
    if board["kind"] != BoardKind.SCRUM:
        # a board whose items are in sprints plans in sprints, whatever the grant could tell us
        board.update(await boards.update(board["_id"], {"kind": BoardKind.SCRUM}))
    if seen is not None:
        seen[sprint["external_id"]] = stored["_id"]
    return {**fields, "sprint_id": stored["_id"]}


async def list_sprints(workspace_id: str, board_id: str) -> list[dict]:
    board = await board_service.get_visible(workspace_id, board_id)
    return [to_sprint(doc) for doc in await repository.list_open(board["_id"])]


async def tracker_for(board: dict) -> tuple:
    """The adapter, auth and connection to change this board's sprints in its tracker."""
    conn = await connectors.active_connection(board["workspace_id"], board["source"])
    if not can_manage_sprints(conn) or not board.get("external_id"):
        raise ConflictError(
            f"reconnect {board['source']} to manage its sprints from Overwatch", error_code=f"{board['source']}_reconnect_required",
        )
    return registry.get(board["source"]).adapter, registry.auth_for(conn), conn


def _require_admin(member: dict) -> None:
    if rank(member["role"]) < rank(Role.ADMIN):
        raise ForbiddenError("only admins start and complete sprints", error_code="insufficient_role")


async def _sprint_and_board(member: dict, sprint_id: str) -> tuple[dict, dict]:
    sprint = await repository.get(member["workspace_id"], sprint_id)
    if sprint is None:
        raise NotFoundError("sprint not found", error_code="sprint_not_found")
    return sprint, await board_service.get_visible(member["workspace_id"], sprint["board_id"])


async def create(member: dict, board_id: str, body) -> dict:
    board = await board_service.get_visible(member["workspace_id"], board_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    fields.setdefault("name", f"{board['name']} Sprint {await repository.count(board['_id']) + 1}")
    if board["source"] == NATIVE_SOURCE:
        doc = await repository.insert({
            "workspace_id": board["workspace_id"], "board_id": board["_id"], "source": NATIVE_SOURCE, "target_key": None,
            "external_id": None, "state": SprintState.FUTURE, "goal": None, "start_at": None, "end_at": None,
            "completed_at": None, **fields,
        })
    else:
        adapter, auth, conn = await tracker_for(board)
        doc = await repository.upsert_tracker(board, await adapter.create_sprint(auth, conn, board["external_id"], fields))
    if board["kind"] != BoardKind.SCRUM:
        await boards.update(board["_id"], {"kind": BoardKind.SCRUM})
    return to_sprint(doc)


async def _change(board: dict, sprint: dict, fields: dict) -> dict:
    if board["source"] == NATIVE_SOURCE:
        return await repository.update(sprint["_id"], fields)
    adapter, auth, conn = await tracker_for(board)
    return await repository.upsert_tracker(board, await adapter.update_sprint(auth, conn, sprint["external_id"], fields))


async def update(member: dict, sprint_id: str, body) -> dict:
    sprint, board = await _sprint_and_board(member, sprint_id)
    if sprint["state"] == SprintState.CLOSED:
        raise ConflictError("a completed sprint can't be changed", error_code="sprint_closed")
    fields = body.model_dump(exclude_unset=True)
    return to_sprint(await _change(board, sprint, fields) if fields else sprint)


async def start(member: dict, sprint_id: str, body) -> dict:
    _require_admin(member)
    sprint, board = await _sprint_and_board(member, sprint_id)
    if sprint["state"] != SprintState.FUTURE:
        raise ConflictError("only a sprint that hasn't started can be started", error_code="sprint_not_future")
    if await repository.active(board["_id"]):
        raise ConflictError("complete the active sprint first", error_code="sprint_already_active")
    start_at = body.start_at or sprint.get("start_at") or utcnow()
    end_at = body.end_at or sprint.get("end_at") or start_at + DEFAULT_LENGTH
    try:
        return to_sprint(await _change(board, sprint, {"state": SprintState.ACTIVE, "start_at": start_at, "end_at": end_at}))
    except DuplicateKeyError:
        # another sprint was started since the check above
        raise ConflictError("complete the active sprint first", error_code="sprint_already_active") from None


async def complete(member: dict, sprint_id: str, body) -> dict:
    """Close the active sprint; its unfinished items go to the backlog or to another open sprint."""
    _require_admin(member)
    sprint, board = await _sprint_and_board(member, sprint_id)
    if sprint["state"] != SprintState.ACTIVE:
        raise ConflictError("only the active sprint can be completed", error_code="sprint_not_active")
    destination = None
    if body.move_open_to:
        destination = await repository.get(member["workspace_id"], body.move_open_to)
        if destination is None or destination["board_id"] != board["_id"] or destination["state"] == SprintState.CLOSED:
            raise NotFoundError("no such open sprint on this board", error_code="sprint_not_found")

    unfinished = {"sprint_id": sprint["_id"], "status_category": {"$ne": StatusCategory.DONE}}
    open_items = [doc async for doc in mongo.work_items().find(unfinished, {"external_id": 1})]
    if board["source"] != NATIVE_SOURCE:
        adapter, auth, conn = await tracker_for(board)
        await adapter.set_sprint(auth, conn, [doc["external_id"] for doc in open_items], destination and destination["external_id"])
    await mongo.work_items().update_many(
        {"_id": {"$in": [doc["_id"] for doc in open_items]}}, {"$set": {"sprint_id": destination and destination["_id"]}},
    )
    # a tracker records when its sprint completed; a native one is stamped here
    closing = {"state": SprintState.CLOSED, **({"completed_at": utcnow()} if board["source"] == NATIVE_SOURCE else {})}
    return to_sprint(await _change(board, sprint, closing))
