from fastapi import Depends

from app.api.deps import get_current_user, get_current_user_id, workspace_member_by_path
from app.core.constants import Role
from app.core.responses import api_router
from app.workspaces import service
from app.workspaces.schemas import (
    CreateInviteRequest,
    Invite,
    InviteCreated,
    InvitePreview,
    Member,
    UpdateMemberRequest,
    Workspace,
    WorkspaceName,
)

router = api_router(tags=["workspaces"])

member = workspace_member_by_path()
admin = workspace_member_by_path(Role.ADMIN)
owner = workspace_member_by_path(Role.OWNER)


@router.get("/workspaces")
async def list_workspaces(user_id: str = Depends(get_current_user_id)) -> list[Workspace]:
    return await service.list_workspaces(user_id)


@router.post("/workspaces", status_code=201)
async def create_workspace(body: WorkspaceName, user_id: str = Depends(get_current_user_id)) -> Workspace:
    return await service.create(user_id, body.name)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(actor: dict = Depends(member)) -> Workspace:
    return service.to_workspace(actor["workspace"], actor)


@router.patch("/workspaces/{workspace_id}")
async def rename_workspace(body: WorkspaceName, actor: dict = Depends(admin)) -> Workspace:
    return await service.rename(actor, body.name)


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(actor: dict = Depends(owner)) -> None:
    await service.delete(actor)


@router.get("/workspaces/{workspace_id}/members")
async def list_members(actor: dict = Depends(member)) -> list[Member]:
    return await service.list_members(actor["workspace_id"])


@router.patch("/workspaces/{workspace_id}/members/{user_id}")
async def update_member(user_id: str, body: UpdateMemberRequest, actor: dict = Depends(admin)) -> None:
    await service.set_role(actor, user_id, body.role)


@router.delete("/workspaces/{workspace_id}/members/{user_id}")
async def remove_member(user_id: str, actor: dict = Depends(member)) -> None:
    await service.remove_member(actor, user_id)


@router.post("/workspaces/{workspace_id}/invites", status_code=201)
async def create_invite(body: CreateInviteRequest, actor: dict = Depends(admin)) -> InviteCreated:
    return await service.create_invite(actor, body.role, body.github_login)


@router.get("/workspaces/{workspace_id}/invites")
async def list_invites(actor: dict = Depends(admin)) -> list[Invite]:
    return await service.list_invites(actor["workspace_id"])


@router.delete("/workspaces/{workspace_id}/invites/{invite_id}")
async def revoke_invite(invite_id: str, actor: dict = Depends(admin)) -> None:
    await service.revoke_invite(actor["workspace_id"], invite_id)


@router.get("/invites/{token}")
async def preview_invite(token: str, user: dict = Depends(get_current_user)) -> InvitePreview:
    return await service.preview_invite(token, user)


@router.post("/invites/{token}/accept")
async def accept_invite(token: str, user: dict = Depends(get_current_user)) -> Workspace:
    return await service.accept_invite(token, user)
