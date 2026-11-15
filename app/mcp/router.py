from fastapi import Depends

from app.api.deps import get_current_user_id, workspace_member
from app.core.config import get_settings
from app.core.exceptions import FieldValidationError
from app.core.responses import api_router
from app.mcp import grants
from app.mcp.schemas import AiTool, AiToolCreated, AiToolSetup, CreateToken

# a member manages their own connected AI tools; each one is tied to the workspace it was made in
router = api_router(prefix="/ai-tools", tags=["ai-tools"])


@router.get("")
async def list_ai_tools(user_id: str = Depends(get_current_user_id)) -> list[AiTool]:
    """The caller's connected AI tools, in every workspace."""
    return await grants.list_for_user(user_id)


@router.get("/setup")
async def setup(_: str = Depends(get_current_user_id)) -> AiToolSetup:
    return AiToolSetup(url=get_settings().mcp_url)


@router.post("/tokens", status_code=201)
async def create_token(body: CreateToken, member: dict = Depends(workspace_member())) -> AiToolCreated:
    """A personal token for the current workspace, for tools that take one (coding agents, CLIs)."""
    settings = get_settings()
    if body.expires_in_days and body.expires_in_days > settings.mcp_token_max_days:
        raise FieldValidationError({"expires_in_days": f"A token lasts {settings.mcp_token_max_days} days at most."})
    grant, token = await grants.create_token(member, body.label.strip(), read_only=body.read_only, days=body.expires_in_days)
    return AiToolCreated(**grant, token=token, url=settings.mcp_url)


@router.delete("/{tool_id}")
async def revoke(tool_id: str, user_id: str = Depends(get_current_user_id)) -> None:
    """Disconnect a tool: its token stops working on its next call."""
    await grants.revoke(user_id, tool_id)
