from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.constants import StatusCategory
from app.core.responses import WithMeta, api_router
from app.work_items import service
from app.work_items.schemas import CreateWorkItem, MoveWorkItem, UpdateWorkItem, WorkItem

router = api_router(prefix="/work-items", tags=["work-items"])


@router.get("")
async def list_work_items(
    source: str | None = None,
    status_category: StatusCategory | None = None,
    target_key: str | None = None,
    assignee_id: str | None = None,
    q: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    # the board asks per column for the latest items, so a long column drops its oldest, not its newest
    newest_first: bool = False,
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[WorkItem]]:
    items, total = await service.list_items(
        member["workspace_id"], source=source, status_category=status_category, target_key=target_key,
        assignee_id=assignee_id, q=q, page=page, page_size=page_size, newest_first=newest_first,
    )
    return WithMeta(items, {"total": total, "page": page, "page_size": page_size})


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
