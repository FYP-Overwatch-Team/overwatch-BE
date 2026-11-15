from fastapi import Depends

from app.api.deps import get_current_user_id, workspace_member
from app.core.config import get_settings
from app.core.constants import Role
from app.core.exceptions import FieldValidationError
from app.core.responses import api_router
from app.db import mongo
from app.mcp import grants
from app.mcp.record import recording_enabled
from app.mcp.schemas import (
    AiTool,
    AiToolCreated,
    AiToolSetup,
    ChangeAiTool,
    CreateToken,
    RecordingSettings,
    WorkspaceAiTool,
)

# a member manages their own connected AI tools; each one is tied to the workspace it was made in
router = api_router(prefix="/ai-tools", tags=["ai-tools"])


@router.get("")
async def list_ai_tools(user_id: str = Depends(get_current_user_id)) -> list[AiTool]:
    """The caller's connected AI tools, in every workspace."""
    return await grants.list_for_user(user_id)


@router.get("/setup")
async def setup(_: str = Depends(get_current_user_id)) -> AiToolSetup:
    return AiToolSetup(url=get_settings().mcp_url)


@router.get("/workspace")
async def list_workspace_ai_tools(admin: dict = Depends(workspace_member(Role.ADMIN))) -> list[WorkspaceAiTool]:
    """Every member's connected AI tools in the current workspace."""
    return await grants.list_for_workspace(admin["workspace_id"])


@router.delete("/workspace/{tool_id}")
async def revoke_workspace_ai_tool(tool_id: str, admin: dict = Depends(workspace_member(Role.ADMIN))) -> None:
    await grants.revoke_in_workspace(admin["workspace_id"], tool_id)


@router.get("/recording")
async def get_recording(member: dict = Depends(workspace_member())) -> RecordingSettings:
    return RecordingSettings(recording_enabled=recording_enabled(member["workspace"]))


@router.put("/recording")
async def set_recording(body: RecordingSettings, admin: dict = Depends(workspace_member(Role.ADMIN))) -> RecordingSettings:
    """Turn recording by AI tools on or off for the whole workspace."""
    await mongo.workspaces().update_one({"_id": admin["workspace_id"]}, {"$set": {"mcp_recording_off": not body.recording_enabled}})
    return body


@router.post("/tokens", status_code=201)
async def create_token(body: CreateToken, member: dict = Depends(workspace_member())) -> AiToolCreated:
    """A personal token for the current workspace, for tools that take one (coding agents, CLIs)."""
    settings = get_settings()
    if body.expires_in_days and body.expires_in_days > settings.mcp_token_max_days:
        raise FieldValidationError({"expires_in_days": f"A token lasts {settings.mcp_token_max_days} days at most."})
    grant, token = await grants.create_token(
        member, body.label.strip(), read_only=body.read_only, ask_first=body.ask_first, days=body.expires_in_days,
    )
    return AiToolCreated(**grant, token=token, url=settings.mcp_url)


@router.patch("/{tool_id}")
async def change(tool_id: str, body: ChangeAiTool, user_id: str = Depends(get_current_user_id)) -> AiTool:
    return await grants.change(user_id, tool_id, body.model_dump(exclude_none=True))


@router.delete("/{tool_id}")
async def revoke(tool_id: str, user_id: str = Depends(get_current_user_id)) -> None:
    """Disconnect a tool: its token stops working on its next call."""
    await grants.revoke(user_id, tool_id)
