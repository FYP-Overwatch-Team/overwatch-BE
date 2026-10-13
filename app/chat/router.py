from fastapi import Depends

from app.api.deps import workspace_member
from app.chat import service
from app.chat.schemas import AskRequest, AskResult, Conversation, ConversationSummary
from app.core.responses import api_router

router = api_router(prefix="/chat", tags=["chat"])


@router.post("/ask")
async def ask(body: AskRequest, member: dict = Depends(workspace_member())) -> AskResult:
    return await service.ask(member, body)


@router.get("/conversations")
async def list_conversations(repo_full_name: str, member: dict = Depends(workspace_member())) -> list[ConversationSummary]:
    return await service.list_conversations(member, repo_full_name)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, member: dict = Depends(workspace_member())) -> Conversation:
    return await service.get_conversation(member, conversation_id)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, member: dict = Depends(workspace_member())) -> None:
    await service.delete_conversation(member, conversation_id)
