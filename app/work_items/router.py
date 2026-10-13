from typing import Literal

from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.constants import StatusCategory
from app.core.responses import WithMeta, api_router
from app.work_items import service
from app.work_items.schemas import CreateWorkItem, Lane, MoveWorkItem, SetSprint, UpdateWorkItem, WorkItem

router = api_router(prefix="/work-items", tags=["work-items"])


@router.get("")
async def list_work_items(
    board_id: str | None = None,
    column_id: str | None = None,
    # with board_id: "current" (the active sprint), "backlog", or a sprint id
    sprint: str | None = None,
    # done items only if they got done this recently (a kanban board's Done column)
    done_within_days: int | None = Query(None, ge=1, le=365),
    # epics are parents, shown on their children rather than as cards
    hide_epics: bool = False,
    # unfinished items only (the backlog)
    open_only: bool = False,
    source: str | None = None,
    status_category: StatusCategory | None = None,
    target_key: str | None = None,
    # "none" for the unassigned (or no-priority) lane
    assignee_id: str | None = None,
    priority: str | None = None,
    q: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    # items to skip, instead of whole pages: lets a board load a first batch, then smaller ones
    offset: int | None = Query(None, ge=0),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[WorkItem]]:
    items, total = await service.list_items(
        member["workspace_id"], board_id=board_id, column_id=column_id, sprint=sprint,
        done_within_days=done_within_days, hide_epics=hide_epics, open_only=open_only, source=source,
        status_category=status_category, target_key=target_key,
        assignee_id=assignee_id, priority=priority, q=q, page=page, page_size=page_size, offset=offset,
    )
    return WithMeta(items, {"total": total, "page": page, "page_size": page_size})


@router.get("/lanes")
async def list_lanes(
    board_id: str,
    by: Literal["assignee", "priority"],
    sprint: str | None = None,
    done_within_days: int | None = Query(None, ge=1, le=365),
    hide_epics: bool = False,
    member: dict = Depends(workspace_member()),
) -> list[Lane]:
    """A board view's swimlanes: each assignee (or priority) with items, "none" for those without."""
    return await service.lanes(
        member["workspace_id"], by, board_id=board_id, sprint=sprint, done_within_days=done_within_days, hide_epics=hide_epics,
    )


@router.post("", status_code=201)
async def create_work_item(body: CreateWorkItem, member: dict = Depends(workspace_member())) -> WorkItem:
    return await service.create(member, body)


@router.patch("/{item_id}")
async def update_work_item(item_id: str, body: UpdateWorkItem, member: dict = Depends(workspace_member())) -> WorkItem:
    return await service.update(member, item_id, body)


@router.post("/{item_id}/move")
async def move_work_item(item_id: str, body: MoveWorkItem, member: dict = Depends(workspace_member())) -> WorkItem:
    return await service.move(member, item_id, body)


@router.delete("/{item_id}")
async def delete_work_item(item_id: str, member: dict = Depends(workspace_member())) -> None:
    await service.delete(member, item_id)


@router.post("/{item_id}/sprint")
async def set_work_item_sprint(item_id: str, body: SetSprint, member: dict = Depends(workspace_member())) -> WorkItem:
    return await service.set_sprint(member, item_id, body)
