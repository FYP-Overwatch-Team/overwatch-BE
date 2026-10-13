"""AI chat conversations (`chat_conversations`), each private to the member who started it.

Every read and write is filtered by workspace and user, so another member's
conversation is simply not found.
"""

import uuid
from datetime import timedelta

from pymongo import ReturnDocument

from app.core.time import utcnow
from app.db import mongo

RETENTION = timedelta(days=30)
MAX_MESSAGES = 100
LIST_LIMIT = 50


def _owned(workspace_id: str, user_id: str, conversation_id: str) -> dict:
    return {"_id": conversation_id, "workspace_id": workspace_id, "user_id": user_id}


async def get(workspace_id: str, user_id: str, conversation_id: str, *, last_messages: int | None = None) -> dict | None:
    """`last_messages` limits what's read to the newest few, for building a question's history."""
    projection = (
        {"messages": {"$slice": -last_messages}, "repo_full_name": 1, "message_count": 1} if last_messages else None
    )
    return await mongo.chat_conversations().find_one(_owned(workspace_id, user_id, conversation_id), projection)


async def list_for(workspace_id: str, user_id: str, repo_full_name: str) -> list[dict]:
    cursor = (
        mongo.chat_conversations()
        .find({"workspace_id": workspace_id, "user_id": user_id, "repo_full_name": repo_full_name}, {"messages": 0})
        .sort("updated_at", -1)
        .limit(LIST_LIMIT)
    )
    return [doc async for doc in cursor]


async def create(workspace_id: str, user_id: str, repo_full_name: str, title: str, messages: list[dict]) -> dict:
    now = utcnow()
    doc = {
        "_id": uuid.uuid4().hex,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "repo_full_name": repo_full_name,
        "title": title,
        "messages": messages,
        "message_count": len(messages),
        "created_at": now,
        "updated_at": now,
        "expires_at": now + RETENTION,
    }
    await mongo.chat_conversations().insert_one(doc)
    return doc


async def append(workspace_id: str, user_id: str, conversation_id: str, messages: list[dict]) -> dict | None:
    """Add messages unless that would pass the cap; None when it would (or it's gone)."""
    now = utcnow()
    return await mongo.chat_conversations().find_one_and_update(
        {**_owned(workspace_id, user_id, conversation_id), "message_count": {"$lte": MAX_MESSAGES - len(messages)}},
        {
            "$push": {"messages": {"$each": messages}},
            "$inc": {"message_count": len(messages)},
            "$set": {"updated_at": now, "expires_at": now + RETENTION},
        },
        projection={"messages": 0},
        return_document=ReturnDocument.AFTER,
    )


async def delete(workspace_id: str, user_id: str, conversation_id: str) -> bool:
    result = await mongo.chat_conversations().delete_one(_owned(workspace_id, user_id, conversation_id))
    return result.deleted_count == 1
