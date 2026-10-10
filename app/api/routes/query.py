from fastapi import Depends
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.responses import api_router
from app.github.repository import get_connected
from app.services import llm_grounding_service

router = api_router(prefix="/query", tags=["query"])


class AskRequest(BaseModel):
    repo_full_name: str
    question: str
    focused_node_id: str | None = None


class FlowRequest(BaseModel):
    repo_full_name: str
    question: str


@router.post("/ask")
async def ask(body: AskRequest, user_id: str = Depends(get_current_user_id)) -> dict:
    await get_connected(user_id, body.repo_full_name)
    return await llm_grounding_service.ask(body.repo_full_name, body.question, body.focused_node_id)


@router.post("/flow")
async def flow(body: FlowRequest, user_id: str = Depends(get_current_user_id)) -> dict:
    await get_connected(user_id, body.repo_full_name)
    return await llm_grounding_service.flow(body.repo_full_name, body.question)
