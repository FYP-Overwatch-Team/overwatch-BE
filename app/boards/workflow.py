"""Which columns a card can move to: the board's own rules, or its tracker's workflow.

Native boards say it per column (`next`: the columns it can move to, None for any). A tracker's
workflow says it per status, so a column is reachable when it holds a status the item can move to.
The frontend mirrors these (features/board/lib.ts) to hint while dragging; the server decides.
"""

from collections import deque

from app.core.constants import ColumnMode


def native_targets(board: dict, from_column_id: str) -> list[str]:
    """The columns a native card can move to from this column."""
    current = next((c for c in board["columns"] if c["id"] == from_column_id), None)
    allowed = None if current is None else current.get("next")
    return [c["id"] for c in board["columns"] if c["id"] != from_column_id and (allowed is None or c["id"] in allowed)]


def first_step(board: dict, from_column_id: str, to_column_id: str) -> str | None:
    """The first column on the shortest way from one column to another; None when there's no way."""
    previous: dict[str, str | None] = {from_column_id: None}
    queue = deque([from_column_id])
    while queue:
        column_id = queue.popleft()
        if column_id == to_column_id:
            while previous[column_id] not in (from_column_id, None):
                column_id = previous[column_id]
            return column_id
        for next_id in native_targets(board, column_id):
            if next_id not in previous:
                previous[next_id] = column_id
                queue.append(next_id)
    return None


def column_holding(board: dict, status_id: str | None, category: str | None) -> dict | None:
    """The column a status shows in: by status on a board with the tracker's columns, by category otherwise."""
    if board["column_mode"] == ColumnMode.TRACKER:
        return next((c for c in board["columns"] if status_id in c["status_ids"]), None)
    return next((c for c in board["columns"] if c["status_category"] == category), None)


def tracker_targets(board: dict, item: dict, reachable: list[dict]) -> list[dict]:
    """Columns reachable through these moves: [{column_id, status_id, status, transition_name, required_fields}], one per status."""
    targets, seen = [], set()
    for move in reachable:
        column = column_holding(board, move["status_id"], move["status_category"])
        key = (column and column["id"], move["status_id"] or move["status_category"])
        if column is None or column["id"] == item.get("column_id") and move["status_id"] == item.get("status_id") or key in seen:
            continue
        seen.add(key)
        targets.append({
            "column_id": column["id"], "status_id": move["status_id"], "status": move["status"],
            "transition_name": move["transition_name"], "required_fields": move.get("required_fields", []),
        })
    return targets


def reachable_in_workflow(board: dict, item: dict) -> list[dict] | None:
    """The moves the stored workflow allows from the item's status; None when it isn't known."""
    workflow = board.get("tracker_workflow") or {}
    workflow_id = (workflow.get("by_issue_type") or {}).get(item.get("issue_type_id") or "")
    if not workflow_id or workflow_id not in workflow.get("workflows", {}):
        return None
    statuses = workflow.get("statuses", {})
    found = []
    for transition in workflow["workflows"][workflow_id]["transitions"]:
        if transition["to"] == item.get("status_id") or (transition["from"] is not None and item.get("status_id") not in transition["from"]):
            continue
        status = statuses.get(transition["to"], {})
        found.append({
            "status_id": transition["to"], "status": status.get("name"), "status_category": status.get("category"),
            "transition_id": transition["id"], "transition_name": transition["name"],
        })
    return found
