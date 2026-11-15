from fastapi import Depends

from app.api.deps import workspace_member
from app.core.constants import Role
from app.core.responses import api_router
from app.work_item_types import service
from app.work_item_types.schemas import CreateWorkItemType, UpdateWorkItemType, WorkItemType

# any member picks a type; admins decide which types there are
router = api_router(prefix="/work-item-types", tags=["work item types"])


@router.get("")
async def list_types(member: dict = Depends(workspace_member())) -> list[WorkItemType]:
    return await service.list_types(member["workspace_id"])


@router.post("", status_code=201)
async def create_type(body: CreateWorkItemType, member: dict = Depends(workspace_member(Role.ADMIN))) -> WorkItemType:
    return await service.create(member["workspace_id"], body)


@router.patch("/{type_id}")
async def update_type(type_id: str, body: UpdateWorkItemType, member: dict = Depends(workspace_member(Role.ADMIN))) -> WorkItemType:
    return await service.update(member["workspace_id"], type_id, body)


@router.delete("/{type_id}")
async def delete_type(type_id: str, reassign_to: str | None = None, member: dict = Depends(workspace_member(Role.ADMIN))) -> None:
    """Remove a type; its items move to `reassign_to`, which is required while any have it."""
    await service.delete(member["workspace_id"], type_id, reassign_to)
