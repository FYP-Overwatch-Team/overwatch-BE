from fastapi import Depends

from app.api.deps import get_current_user_id
from app.core.responses import api_router
from app.github.repository import get_readable
from app.query import service
from app.query.schemas import AskAnswer, AskRequest, FlowRequest, FlowResult

router = api_router(prefix="/query", tags=["query"])


@router.post("/ask")
async def ask(body: AskRequest, user_id: str = Depends(get_current_user_id)) -> AskAnswer:
    # the repo arrives in the body, so it's authorised here rather than by a dependency
    await get_readable(user_id, body.repo_full_name)
    return await service.ask(body.repo_full_name, body.question, body.focused_node_id)


@router.post("/flow")
async def flow(body: FlowRequest, user_id: str = Depends(get_current_user_id)) -> FlowResult:
    await get_readable(user_id, body.repo_full_name)
    return await service.flow(body.repo_full_name, body.question)
