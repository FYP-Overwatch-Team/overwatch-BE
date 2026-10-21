from fastapi import Depends

from app.api.deps import workspace_member
from app.boards import service
from app.boards.schemas import Board, ColumnSettings, NativeColumns
from app.connectors import sync
from app.connectors.schemas import SyncTarget
from app.core.constants import NATIVE_SOURCE
from app.core.exceptions import ConflictError
from app.core.responses import api_router

router = api_router(prefix="/boards", tags=["boards"])


@router.get("")
async def list_boards(member: dict = Depends(workspace_member())) -> list[Board]:
    return await service.list_boards(member["workspace_id"])


@router.post("/{board_id}/columns/tracker")
async def use_tracker_columns(board_id: str, member: dict = Depends(workspace_member())) -> Board:
    return await service.use_tracker_columns(member, board_id)


@router.post("/{board_id}/columns/default")
async def keep_default_columns(board_id: str, member: dict = Depends(workspace_member())) -> Board:
    return await service.keep_default_columns(member, board_id)


@router.get("/{board_id}/priorities")
async def list_priorities(board_id: str, member: dict = Depends(workspace_member())) -> list[str]:
    return await service.priorities(member["workspace_id"], board_id)


@router.patch("/{board_id}/columns/settings")
async def update_column_settings(board_id: str, body: ColumnSettings, member: dict = Depends(workspace_member())) -> Board:
    return await service.update_column_settings(member, board_id, body)


@router.post("/{board_id}/sync")
async def sync_board(board_id: str, force: bool = False, member: dict = Depends(workspace_member())) -> SyncTarget:
    """Bring a tracker board up to date now: its changes since the last sync (everything, the first time).

    Any member may ask; one sync runs per board at a time, and a board synced moments ago is
    left alone unless `force` (the refresh button).
    """
    board = await service.get_visible(member["workspace_id"], board_id)
    if board["source"] == NATIVE_SOURCE:
        raise ConflictError("Boards made here have nothing to sync", error_code="board_not_synced")
    return await sync.sync_now(member["workspace_id"], board["source"], board["target_key"], force=force)


@router.put("/{board_id}/columns")
async def set_native_columns(board_id: str, body: NativeColumns, member: dict = Depends(workspace_member())) -> Board:
    """A native board's columns and workflow (admins); tracker boards follow their tracker."""
    return await service.set_native_columns(member, board_id, body)
