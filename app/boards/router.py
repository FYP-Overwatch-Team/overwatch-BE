from fastapi import Depends

from app.api.deps import workspace_member
from app.boards import service
from app.boards.schemas import Board, ColumnSettings
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
