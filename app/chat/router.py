import json
from collections.abc import AsyncIterator

import structlog
from fastapi import Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from app.api.deps import workspace_member
from app.chat import service
from app.chat.schemas import AskRequest, AskResult, Conversation, ConversationSummary
from app.core.exceptions import AppError
from app.core.responses import api_router

logger = structlog.get_logger("app.chat")

router = api_router(prefix="/chat", tags=["chat"])


@router.post("/ask")
async def ask(body: AskRequest, member: dict = Depends(workspace_member())) -> AskResult:
    return await service.ask(member, body)


@router.post("/ask/stream")
async def ask_stream(body: AskRequest, member: dict = Depends(workspace_member())) -> StreamingResponse:
    """The answer as server-sent events while it's written: `started`, `thinking` and `answer`
    pieces and a `status` for each lookup ("Reading SignupForm"), then `done` with what
    `/chat/ask` returns, or `error` with `error_code` and `message`.

    Anything that refuses the question does so before the stream opens, as an ordinary error response.
    """
    events = service.ask_stream(member, body)
    first = await anext(events)  # raises the refusals while an error response is still possible
    return StreamingResponse(
        _server_sent(first, events),
        media_type="text/event-stream",
        # keep proxies from holding the stream back to buffer or compress it
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _server_sent(first: tuple[str, dict], events: AsyncIterator[tuple[str, dict]]) -> AsyncIterator[str]:
    yield _event(*first)
    try:
        async for event, data in events:
            yield _event(event, data)
    except AppError as exc:
        yield _event("error", {"error_code": exc.error_code, "message": exc.message})
    except Exception:
        logger.exception("chat_stream_failed")
        yield _event("error", {"error_code": "internal_error", "message": "An internal error occurred"})


def _event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(jsonable_encoder(data))}\n\n"


@router.get("/conversations")
async def list_conversations(repo_full_name: str, member: dict = Depends(workspace_member())) -> list[ConversationSummary]:
    return await service.list_conversations(member, repo_full_name)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, member: dict = Depends(workspace_member())) -> Conversation:
    return await service.get_conversation(member, conversation_id)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, member: dict = Depends(workspace_member())) -> None:
    await service.delete_conversation(member, conversation_id)
