"""A decision's discussion: every member of its workspace can read it and write in it.

Messages are ordered by their id (a uuid7, so newer ids sort later) and paged backwards from
the newest. A message's author edits it; its author or an admin deletes it.
"""

import uuid
from datetime import timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.auth import usernames
from app.auth.repository import get_user
from app.core.constants import Role
from app.core.exceptions import FieldValidationError, ForbiddenError, NotFoundError
from app.core.rate_limit import Limit, take
from app.core.time import utcnow
from app.db import mongo
from app.decision_comments import attachments
from app.decision_comments.schemas import MAX_REACTIONS, DecisionComment
from app.decisions import service as decisions
from app.realtime import topics
from app.realtime.events import EventName
from app.realtime.hub import get_hub
from app.workspaces.service import rank

#: messages per member per minute, across decisions
SENDS = Limit("decision_comment", 30, timedelta(minutes=1))


def _out(doc: dict) -> dict:
    return {**doc, "id": doc["_id"], "edited": doc.get("edited_at") is not None}


def _publish(decision_id: str, name: EventName, data: dict) -> None:
    """Tell everyone viewing the decision's discussion, once the change is stored."""
    get_hub().publish(topics.name("decision", decision_id), name, data)


def _as_event(message: dict) -> dict:
    # as the REST API returns it, dates and all
    return {"message": DecisionComment.model_validate(message).model_dump(mode="json")}


def _not_found() -> NotFoundError:
    return NotFoundError("message not found", error_code="comment_not_found")


async def list_page(member: dict, decision_id: str, before: str | None, limit: int) -> tuple[list[dict], bool]:
    """Up to `limit` messages sent before the message `before` (the newest when None), oldest first,
    and whether there are older ones."""
    workspace_id = member["workspace_id"]
    await decisions.found(workspace_id, decision_id)
    query: dict = {"workspace_id": workspace_id, "decision_id": decision_id}
    if before:
        query["_id"] = {"$lt": before}
    newest = await mongo.decision_comments().find(query).sort("_id", -1).limit(limit + 1).to_list(None)
    return [_out(doc) for doc in reversed(newest[:limit])], len(newest) > limit


async def add(member: dict, decision_id: str, client_id: str, body: str, attachment_ids: list[str]) -> dict:
    """The message sent, with the member's uploads `attachment_ids`; sent again with the same
    `client_id`, the message already posted."""
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    await decisions.found(workspace_id, decision_id)
    sent = {"decision_id": decision_id, "author.id": user_id, "client_id": client_id}
    # a retry of a send that reached us (the others were told the first time)
    if existing := await mongo.decision_comments().find_one(sent):
        return _out(existing)
    await take(SENDS, user_id)
    now, comment_id = utcnow(), uuid.uuid7().hex
    doc = {
        "_id": comment_id, "workspace_id": workspace_id, "decision_id": decision_id,
        "author": {"id": user_id, "name": usernames.display_name(await get_user(user_id))},
        "body": body, "client_id": client_id, "reactions": [],
        "attachments": await attachments.claim(member, decision_id, comment_id, attachment_ids),
        "version": 1, "created_at": now, "updated_at": now, "edited_at": None,
    }
    try:
        await mongo.decision_comments().insert_one(doc)
    except DuplicateKeyError:
        # the same send, twice at once: the unique (decision, author, client id) index kept one
        await attachments.release(comment_id)
        return _out(await mongo.decision_comments().find_one(sent))
    message = _out(doc)
    _publish(decision_id, EventName.DECISION_COMMENT_CREATED, _as_event(message))
    return message


async def edit(member: dict, decision_id: str, comment_id: str, body: str) -> dict:
    """Only its author changes a message."""
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    now = utcnow()
    doc = await mongo.decision_comments().find_one_and_update(
        {"_id": comment_id, "workspace_id": workspace_id, "decision_id": decision_id, "author.id": user_id},
        {"$set": {"body": body, "edited_at": now, "updated_at": now}, "$inc": {"version": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        if await mongo.decision_comments().count_documents({"_id": comment_id, "workspace_id": workspace_id, "decision_id": decision_id}):
            raise ForbiddenError("Only its author can edit a message", error_code="not_comment_author")
        raise _not_found()
    message = _out(doc)
    _publish(decision_id, EventName.DECISION_COMMENT_UPDATED, _as_event(message))
    return message


async def delete(member: dict, decision_id: str, comment_id: str) -> None:
    """Its author or an admin removes a message."""
    workspace_id = member["workspace_id"]
    doc = await mongo.decision_comments().find_one({"_id": comment_id, "workspace_id": workspace_id, "decision_id": decision_id})
    if doc is None:
        raise _not_found()
    if doc["author"]["id"] != member["user_id"] and rank(member["role"]) < rank(Role.ADMIN):
        raise ForbiddenError("Only its author or an admin can delete a message", error_code="not_comment_author")
    await mongo.decision_comments().delete_one({"_id": comment_id})
    await attachments.remove({"comment_id": comment_id})
    _publish(decision_id, EventName.DECISION_COMMENT_DELETED, {"id": comment_id})


async def react(member: dict, decision_id: str, comment_id: str, emoji: str) -> dict:
    """Add the member's `emoji` to a message; already there, nothing changes."""
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    await decisions.found(workspace_id, decision_id)
    here = {"_id": comment_id, "workspace_id": workspace_id, "decision_id": decision_id}
    comments = mongo.decision_comments()
    reacted = {**here, "reactions": {"$elemMatch": {"emoji": emoji, "user_ids": user_id}}}
    # twice at most: someone else may start the same emoji between the two updates below
    for _ in range(2):
        if await comments.count_documents(reacted):
            break
        # join an emoji already on the message...
        joined = await comments.update_one(
            {**here, "reactions": {"$elemMatch": {"emoji": emoji}}},
            {"$addToSet": {"reactions.$.user_ids": user_id}, "$inc": {"version": 1}},
        )
        if joined.matched_count:
            break
        # ...or start it, while there's room
        started = await comments.update_one(
            {**here, "reactions.emoji": {"$ne": emoji}, f"reactions.{MAX_REACTIONS - 1}": {"$exists": False}},
            {"$push": {"reactions": {"emoji": emoji, "user_ids": [user_id]}}, "$inc": {"version": 1}},
        )
        if started.matched_count:
            break
    doc = await comments.find_one(here, {"reactions": 1})
    if doc is None:
        raise _not_found()
    if emoji not in {reaction["emoji"] for reaction in doc.get("reactions", [])}:
        raise FieldValidationError({"emoji": f"A message holds up to {MAX_REACTIONS} different reactions."})
    return await _reacted(here, decision_id)


async def unreact(member: dict, decision_id: str, comment_id: str, emoji: str) -> dict:
    """Take the member's `emoji` off a message; an emoji nobody's left on goes."""
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    await decisions.found(workspace_id, decision_id)
    here = {"_id": comment_id, "workspace_id": workspace_id, "decision_id": decision_id}
    left = await mongo.decision_comments().update_one(
        {**here, "reactions": {"$elemMatch": {"emoji": emoji, "user_ids": user_id}}},
        {"$pull": {"reactions.$.user_ids": user_id}, "$inc": {"version": 1}},
    )
    if left.modified_count:
        await mongo.decision_comments().update_one(here, {"$pull": {"reactions": {"user_ids": {"$size": 0}}}})
    return await _reacted(here, decision_id)


async def _reacted(here: dict, decision_id: str) -> dict:
    doc = await mongo.decision_comments().find_one(here)
    if doc is None:
        raise _not_found()
    message = _out(doc)
    _publish(decision_id, EventName.DECISION_COMMENT_UPDATED, _as_event(message))
    return message


async def delete_for_decision(workspace_id: str, decision_id: str) -> None:
    """The whole discussion and its files, when its decision goes."""
    await mongo.decision_comments().delete_many({"workspace_id": workspace_id, "decision_id": decision_id})
    await attachments.remove({"workspace_id": workspace_id, "decision_id": decision_id})
