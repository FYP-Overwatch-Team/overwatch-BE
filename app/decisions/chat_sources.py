"""Chat conversations kept as sources: a copy stays only while something rests on it.

Chat messages expire within hours, but a decision rechecks its evidence against its source for as
long as it lives, and an open question waits days for someone to act on it. So a conversation is
copied to `chat_threads` when it's read, and the copy goes as soon as neither a decision nor a
still-open question uses it.
"""

from app.core.constants import ACTIVE_QUESTION_STATUSES
from app.db import mongo


async def in_use(workspace_id: str, ref_id: str) -> bool:
    """A decision quotes the conversation, or a question raised in it is still waiting on someone."""
    if await mongo.decisions().find_one({"workspace_id": workspace_id, "evidence.ref_id": ref_id}, {"_id": 1}):
        return True
    waiting = {"workspace_id": workspace_id, "thread_ref": ref_id, "status": {"$in": list(ACTIVE_QUESTION_STATUSES)}}
    return await mongo.chat_questions().find_one(waiting, {"_id": 1}) is not None


async def release_if_unused(workspace_id: str, ref_id: str) -> None:
    if not await in_use(workspace_id, ref_id):
        await mongo.chat_threads().delete_one({"workspace_id": workspace_id, "_id": ref_id})
