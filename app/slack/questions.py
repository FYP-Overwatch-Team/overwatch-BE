"""Questions raised in a Slack conversation but not settled there.

The chat read (`app.slack.decisions`) lists them next to the decisions it finds. Each is kept with
a quote checked word for word against the conversation, once per conversation (by the quote's
fingerprint), and waits for the channel's daily digest (`app.slack.digest`). From there someone
tracks it as an open decision or settles it in the thread; one nobody acts on expires after
`slack_question_expiry_days`, and its conversation copy goes with it unless a decision uses it.
"""

import uuid
from datetime import timedelta

import structlog
from pymongo import UpdateOne

from app.core.config import get_settings
from app.core.constants import ACTIVE_QUESTION_STATUSES, ChatQuestionStatus, Provider
from app.core.time import utcnow
from app.db import mongo
from app.decisions import chat_sources, quotes
from app.decisions.suggest import Reply

logger = structlog.get_logger("app.slack")

MAX_PER_CONVERSATION = 3


async def keep_from_reply(workspace_id: str, channel: str, thread_ref: str, external_url: str | None, reply: Reply, text: str) -> int:
    """Keep the reply's open questions whose quote is in the conversation; returns how many were new."""
    now = utcnow()
    expires_at = now + timedelta(days=get_settings().slack_question_expiry_days)
    writes = []
    for found in reply.questions[:MAX_PER_CONVERSATION]:
        if not quotes.contains(text, found.quote):
            continue  # a passage the conversation doesn't contain is made up
        fingerprint = quotes.fingerprint(found.quote)
        writes.append(UpdateOne(
            {"workspace_id": workspace_id, "thread_ref": thread_ref, "fingerprint": fingerprint},
            {"$setOnInsert": {
                "_id": uuid.uuid4().hex, "provider": Provider.SLACK, "channel": channel,
                "question": found.question.strip(), "quote": found.quote, "external_url": external_url,
                "status": ChatQuestionStatus.PENDING, "created_at": now, "expires_at": expires_at,
            }},
            upsert=True,
        ))
    if not writes:
        return 0
    result = await mongo.chat_questions().bulk_write(writes, ordered=False)
    return result.upserted_count


async def settle_conversation(workspace_id: str, thread_ref: str, asked_before) -> None:
    """The conversation went on to state a decision: its questions from before then are settled."""
    await mongo.chat_questions().update_many(
        {
            "workspace_id": workspace_id, "thread_ref": thread_ref, "created_at": {"$lt": asked_before},
            "status": {"$in": list(ACTIVE_QUESTION_STATUSES)},
        },
        {"$set": {"status": ChatQuestionStatus.SETTLED, "updated_at": utcnow()}},
    )


async def expire_due() -> int:
    """Expire the questions nobody acted on in time, and drop the conversations nothing else uses."""
    now = utcnow()
    due = {"status": {"$in": list(ACTIVE_QUESTION_STATUSES)}, "expires_at": {"$lt": now}}
    expired = [doc async for doc in mongo.chat_questions().find(due, {"workspace_id": 1, "thread_ref": 1})]
    if not expired:
        return 0
    await mongo.chat_questions().update_many(
        {"_id": {"$in": [doc["_id"] for doc in expired]}, "status": {"$in": list(ACTIVE_QUESTION_STATUSES)}},
        {"$set": {"status": ChatQuestionStatus.EXPIRED, "updated_at": now}},
    )
    for workspace_id, thread_ref in {(doc["workspace_id"], doc["thread_ref"]) for doc in expired}:
        await chat_sources.release_if_unused(workspace_id, thread_ref)
    logger.info("slack_questions_expired", count=len(expired))
    return len(expired)
