"""Questions raised in a Slack conversation but not settled there.

The chat read (`app.slack.decisions`) lists them next to the decisions it finds. Each is kept with
a quote checked word for word against the conversation, once per conversation (by the quote's
fingerprint), and waits for the channel's daily digest (`app.slack.digest`). From there someone
tracks it as an open decision or settles it in the thread; one nobody acts on expires after
`slack_question_expiry_days`, and one whose message is deleted or edited away in Slack is withdrawn.

Once a question is closed (settled, tracked, expired, withdrawn) its quote of the chat is dropped,
and the record itself goes after `CLOSED_RETENTION`; until then it only stops the same question
being kept again. Its conversation copy goes as soon as nothing else uses it.
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
CLOSED_RETENTION = timedelta(days=30)
CLOSED_STATUSES = [s for s in ChatQuestionStatus if s not in ACTIVE_QUESTION_STATUSES]
_ACTIVE = {"$in": list(ACTIVE_QUESTION_STATUSES)}


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


def closing(status: ChatQuestionStatus, **fields) -> dict:
    """The update that closes a question: its quote of the chat goes, and it's kept a while only to dedupe."""
    now = utcnow()
    return {"$set": {"status": status, "closed_at": now, "updated_at": now, **fields}, "$unset": {"quote": ""}}


async def open_in(workspace_id: str, thread_ref: str) -> list[str]:
    """The ids of the conversation's questions still open."""
    query = {"workspace_id": workspace_id, "thread_ref": thread_ref, "status": _ACTIVE}
    return [doc["_id"] async for doc in mongo.chat_questions().find(query, {"_id": 1})]


async def settle_conversation(workspace_id: str, thread_ref: str, asked: list[str]) -> None:
    """The conversation went on to state a decision: its questions `asked` before then are settled.

    They're named by id, not by a time, because Mongo keeps times to the millisecond: a question kept
    in the same millisecond a later read started would look asked after it.
    """
    if not asked:
        return
    await mongo.chat_questions().update_many(
        {"_id": {"$in": asked}, "workspace_id": workspace_id, "thread_ref": thread_ref, "status": _ACTIVE},
        closing(ChatQuestionStatus.SETTLED),
    )


async def withdraw_unquoted(workspace_id: str, thread_ref: str, text: str | None) -> None:
    """Withdraw the conversation's open questions whose message was deleted (`text` None) or edited away."""
    waiting = [doc async for doc in mongo.chat_questions().find(
        {"workspace_id": workspace_id, "thread_ref": thread_ref, "status": _ACTIVE}, {"quote": 1},
    )]
    gone = [doc["_id"] for doc in waiting if text is None or not quotes.contains(text, doc.get("quote") or "")]
    if not gone:
        return
    await mongo.chat_questions().update_many({"_id": {"$in": gone}, "status": _ACTIVE}, closing(ChatQuestionStatus.WITHDRAWN))
    await chat_sources.release_if_unused(workspace_id, thread_ref)


async def expire_due() -> int:
    """Expire the questions nobody acted on in time, drop the conversations nothing else uses, and forget
    closed questions after `CLOSED_RETENTION`."""
    now = utcnow()
    await mongo.chat_questions().delete_many({"status": {"$in": CLOSED_STATUSES}, "closed_at": {"$lt": now - CLOSED_RETENTION}})
    due = {"status": _ACTIVE, "expires_at": {"$lt": now}}
    expired = [doc async for doc in mongo.chat_questions().find(due, {"workspace_id": 1, "thread_ref": 1})]
    if not expired:
        return 0
    await mongo.chat_questions().update_many(
        {"_id": {"$in": [doc["_id"] for doc in expired]}, "status": _ACTIVE}, closing(ChatQuestionStatus.EXPIRED),
    )
    for workspace_id, thread_ref in {(doc["workspace_id"], doc["thread_ref"]) for doc in expired}:
        await chat_sources.release_if_unused(workspace_id, thread_ref)
    logger.info("slack_questions_expired", count=len(expired))
    return len(expired)
