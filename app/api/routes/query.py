from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.exceptions import NotFoundError
from app.db import mongo
from app.services import llm_grounding_service

router = APIRouter(prefix="/query", tags=["query"])


class AskRequest(BaseModel):
    repo_full_name: str
    question: str
    focused_node_id: str | None = None


class FlowRequest(BaseModel):
    repo_full_name: str
    question: str


async def _require_connected_repo(user_id: str, repo_full_name: str) -> None:
    repo = await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})
    if repo is None:
        raise NotFoundError("repository is not connected", error_code="repo_not_connected")


@router.post("/ask")
async def ask(body: AskRequest, user_id: str = Depends(get_current_user_id)) -> dict:
    await _require_connected_repo(user_id, body.repo_full_name)
    return await llm_grounding_service.ask(body.repo_full_name, body.question, body.focused_node_id)


@router.post("/flow")
async def flow(body: FlowRequest, user_id: str = Depends(get_current_user_id)) -> dict:
    await _require_connected_repo(user_id, body.repo_full_name)
    return await llm_grounding_service.flow(body.repo_full_name, body.question)
