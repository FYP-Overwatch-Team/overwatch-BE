"""Boards and their columns.

A tracker board starts with the three category columns. Each full sync reads the
tracker's own columns; when they differ from what the board has, an admin is asked
once whether to use them. Once accepted, later changes in the tracker follow
automatically; once declined, the board only asks again if the tracker's columns change.
"""

import hashlib
import json

from app.boards import repository
from app.boards.repository import DEFAULT_COLUMNS
from app.connectors import registry
from app.connectors import repository as connectors
from app.connectors.service import can_write
from app.core.constants import NATIVE_SOURCE, ColumnMode, ColumnPrompt, Role
from app.core.exceptions import AppError, ConflictError, ForbiddenError, NotFoundError
from app.workspaces.service import rank


def columns_hash(columns: list[dict]) -> str:
    shape = [[column["name"], column["status_category"], sorted(column["status_ids"])] for column in columns]
    return hashlib.sha1(json.dumps(shape).encode()).hexdigest()


def resolve_column(board: dict, status_id: str | None, category: str) -> str | None:
    """Which column a tracker item sits in; None for a status no column holds (hidden, as in Jira)."""
    if board["column_mode"] == ColumnMode.TRACKER:
        return next((c["id"] for c in board["columns"] if status_id in c["status_ids"]), None)
    return next((c["id"] for c in board["columns"] if c["status_category"] == category), None)


def place(board: dict, fields: dict) -> dict:
    """A synced item's fields with its column on its board, and whether its tracker's board shows it at all."""
    shown = board.get("tracker_status_ids")
    return {
        **fields,
        "column_id": resolve_column(board, fields.get("status_id"), fields["status_category"]),
        "tracker_hidden": bool(shown) and fields.get("status_id") not in shown,
    }


async def offer_columns(board: dict, columns: list[dict]) -> dict:
    """What a full sync found: followed on a board that uses the tracker's columns, else offered once.

    Either way the statuses the tracker's board shows are kept, so items in any other status
    (Jira's unmapped ones, e.g. "Archived") stay off the board and backlog, as they do there.
    """
    if not columns:
        return board
    shown = sorted({status_id for column in columns for status_id in column["status_ids"]})
    if shown != board.get("tracker_status_ids"):
        board = await repository.update(board["_id"], {"tracker_status_ids": shown})
        await repository.mark_hidden({"workspace_id": board["workspace_id"], "source": board["source"], "target_key": board["target_key"]}, shown)
    digest = columns_hash(columns)
    if board["column_mode"] == ColumnMode.TRACKER:
        if digest != board.get("columns_hash"):
            board = await repository.update(board["_id"], {"columns": columns, "columns_hash": digest})
            await repository.place_items(board)
        return board
    prompt = board["column_prompt"]
    if prompt["status"] == ColumnPrompt.DISMISSED and prompt["hash"] == digest:
        return board
    return await repository.update(board["_id"], {"column_prompt": {"status": ColumnPrompt.PENDING, "columns": columns, "hash": digest}})


def _arranged(board: dict) -> list[dict]:
    """The columns in the board's saved order (any it doesn't mention after, in the tracker's order), with colors."""
    order = {column_id: index for index, column_id in enumerate(board.get("column_order", []))}
    colors = board.get("column_colors", {})
    columns = sorted(board["columns"], key=lambda c: order.get(c["id"], len(order)))
    return [
        {**{k: c[k] for k in ("id", "name", "status_category")}, "color": colors.get(c["id"]), "status_ids": c.get("status_ids", []), "next": c.get("next")}
        for c in columns
    ]


def to_board(board: dict, *, writable: bool) -> dict:
    prompt = board["column_prompt"]
    return {
        "id": board["_id"],
        "source": board["source"],
        "target_key": board["target_key"],
        "name": board["name"],
        "kind": board["kind"],
        "column_mode": board["column_mode"],
        "columns": _arranged(board),
        "column_prompt": {"status": prompt["status"], "columns": [c["name"] for c in prompt["columns"]]},
        "editable": writable,
        "workflow": board.get("tracker_workflow"),
    }


async def _writable_sources(workspace_id: str) -> set[str]:
    """The sources whose boards can be changed here: native, and trackers whose grant can write back."""
    return {NATIVE_SOURCE, *(c["provider"] for c in await connectors.list_connections(workspace_id) if can_write(c))}


async def _board_for(board: dict) -> dict:
    return to_board(board, writable=board["source"] in await _writable_sources(board["workspace_id"]))


async def list_boards(workspace_id: str) -> list[dict]:
    """The native board, then one per connected target (created on first sight)."""
    writable = await _writable_sources(workspace_id)
    boards = [await repository.ensure(workspace_id, NATIVE_SOURCE, None)]
    for target in await connectors.list_targets(workspace_id):
        # a docs space is a target too, but it has no board
        if not registry.has_work_items(target["provider"]):
            continue
        boards.append(await repository.ensure(workspace_id, target["provider"], target["key"], target.get("name")))
    return [to_board(board, writable=board["source"] in writable) for board in boards]


async def get_visible(workspace_id: str, board_id: str) -> dict:
    """A board whose target is still connected; 404 otherwise."""
    board = await repository.get(workspace_id, board_id)
    if board and (
        board["source"] == NATIVE_SOURCE
        or (registry.has_work_items(board["source"]) and await connectors.find_target(workspace_id, board["source"], board["target_key"]))
    ):
        return board
    raise NotFoundError("board not found", error_code="board_not_found")


async def _admin_board(member: dict, board_id: str) -> dict:
    if rank(member["role"]) < rank(Role.ADMIN):
        raise ForbiddenError("only admins change a board's columns", error_code="insufficient_role")
    return await get_visible(member["workspace_id"], board_id)


async def use_tracker_columns(member: dict, board_id: str) -> dict:
    board = await _admin_board(member, board_id)
    columns = board["column_prompt"]["columns"]
    if not columns:
        raise ConflictError("the tracker's columns haven't been read yet; sync first", error_code="no_tracker_columns")
    board = await repository.update(board["_id"], {
        "column_mode": ColumnMode.TRACKER, "columns": columns, "columns_hash": columns_hash(columns),
        "column_prompt": {**board["column_prompt"], "status": ColumnPrompt.NONE},
    })
    await repository.place_items(board)
    return await _board_for(board)


async def keep_default_columns(member: dict, board_id: str) -> dict:
    """Decline the offer, or go back to the category columns; asked again only if the tracker's columns change."""
    board = await _admin_board(member, board_id)
    board = await repository.update(board["_id"], {
        "column_mode": ColumnMode.DEFAULT, "columns": DEFAULT_COLUMNS, "columns_hash": None,
        "column_prompt": {**board["column_prompt"], "status": ColumnPrompt.DISMISSED},
    })
    await repository.place_items(board)
    return await _board_for(board)


#: the Overwatch board's priorities, most urgent first (Jira's defaults)
NATIVE_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]


async def priorities(workspace_id: str, board_id: str) -> list[str]:
    board = await get_visible(workspace_id, board_id)
    if board["source"] == NATIVE_SOURCE:
        return NATIVE_PRIORITIES
    conn = await connectors.active_connection(workspace_id, board["source"])
    if conn is None:
        raise ConflictError(f"{board['source']} is not connected", error_code=f"{board['source']}_not_connected")
    return await registry.work_item_source(board["source"]).list_priorities(registry.auth_for(conn), conn)


async def update_column_settings(member: dict, board_id: str, body) -> dict:
    """Column order and colors: how the whole team sees the board. Any member may change them."""
    board = await get_visible(member["workspace_id"], board_id)
    known = {column["id"] for column in board["columns"]}
    sent = [*(body.column_order or []), *(body.column_colors or {})]
    unknown = [column_id for column_id in sent if column_id not in known]
    if unknown:
        raise AppError(f"not columns of this board: {', '.join(unknown)}", error_code="column_not_found", status_code=422)
    changes: dict = {}
    if body.column_order is not None:
        changes["column_order"] = list(dict.fromkeys(body.column_order))
    if body.column_colors is not None:
        # colors of columns the tracker has since dropped go too
        colors = {k: v for k, v in board.get("column_colors", {}).items() if k in known}
        for column_id, color in body.column_colors.items():
            if color is None:
                colors.pop(column_id, None)
            else:
                colors[column_id] = color
        changes["column_colors"] = colors
    if changes:
        board = await repository.update(board["_id"], changes)
    return await _board_for(board)


async def set_native_columns(member: dict, board_id: str, body) -> dict:
    """Replace a native board's columns and workflow; items in removed columns move where asked (else the first)."""
    board = await _admin_board(member, board_id)
    if board["source"] != NATIVE_SOURCE:
        raise ConflictError("a tracker's board follows the tracker's columns and workflow", error_code="board_follows_tracker")
    ids = [column.id for column in body.columns]
    names = [column.name.strip().lower() for column in body.columns]
    if len(set(ids)) != len(ids) or len(set(names)) != len(names):
        raise AppError("columns need unique ids and names", error_code="duplicate_columns", status_code=422)
    unknown = sorted({target for column in body.columns for target in column.next or [] if target not in ids})
    removed = [column["id"] for column in board["columns"] if column["id"] not in ids]
    bad_moves = sorted({target for source, target in body.reassign.items() if source not in removed or target not in ids})
    if unknown or bad_moves:
        raise AppError(f"not columns of this board: {', '.join(unknown + bad_moves)}", error_code="column_not_found", status_code=422)

    columns = [
        {"id": column.id, "name": column.name.strip(), "status_category": column.status_category, "status_ids": [],
         "next": None if column.next is None else [target for target in dict.fromkeys(column.next) if target != column.id]}
        for column in body.columns
    ]
    scope = {"workspace_id": board["workspace_id"], "source": NATIVE_SOURCE}
    for removed_id in removed:
        target = next(c for c in columns if c["id"] == body.reassign.get(removed_id, ids[0]))
        await repository.move_items(scope, removed_id, target["id"], target["status_category"])
    for column in columns:
        await repository.move_items(scope, column["id"], column["id"], column["status_category"])
    board = await repository.update(board["_id"], {
        "columns": columns, "column_order": ids,
        "column_colors": {column.id: column.color for column in body.columns if column.color},
    })
    return await _board_for(board)
