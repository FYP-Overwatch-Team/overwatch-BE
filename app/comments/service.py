"""Comments on work items.

Native items keep their comments here. A tracker item's comments live in the tracker and
are read from it each time; a comment posted from Overwatch goes there under the
connecting account, so it's also recorded here with its Overwatch author. That record is
what shows who wrote it and lets only them change it.
"""

import uuid

from app.auth.repository import get_user
from app.connectors import registry
from app.connectors import repository as connectors
from app.core.constants import NATIVE_SOURCE, Role
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.db import mongo
from app.work_items import service as work_items
from app.workspaces.service import rank

# appended to comments posted to a tracker, so people reading them there know who wrote them
ATTRIBUTION = "\n\n_Posted from Overwatch by {name}_"


async def _author(member: dict) -> dict:
    user = await get_user(member["user_id"])
    return {"id": member["user_id"], "name": (user or {}).get("name") or (user or {}).get("github_login")}


def _rights(member: dict, author_id: str | None, local: bool) -> dict:
    mine = local and author_id == member["user_id"]
    return {"editable": mine, "deletable": mine or (local and rank(member["role"]) >= rank(Role.ADMIN))}


async def _item_and_tracker(member: dict, item_id: str) -> tuple[dict, tuple | None]:
    item = await work_items.get_visible(member["workspace_id"], item_id)
    if item["source"] == NATIVE_SOURCE:
        return item, None
    conn = await work_items.writable_connection(member["workspace_id"], item["source"])
    return item, (registry.get(item["source"]).adapter, registry.auth_for(conn), conn)


async def list_comments(member: dict, item_id: str) -> list[dict]:
    item = await work_items.get_visible(member["workspace_id"], item_id)
    stored = [doc async for doc in mongo.work_item_comments().find({"workspace_id": member["workspace_id"], "item_id": item_id}).sort("created_at", 1)]
    if item["source"] == NATIVE_SOURCE:
        return [{**doc, "id": doc["_id"], **_rights(member, doc["author"]["id"], True)} for doc in stored]

    conn = await connectors.active_connection(member["workspace_id"], item["source"])
    if conn is None:
        raise ConflictError(f"{item['source']} is not connected", error_code=f"{item['source']}_not_connected")
    adapter, auth = registry.get(item["source"]).adapter, registry.auth_for(conn)
    ours = {doc["external_id"]: doc for doc in stored}
    comments = []
    for comment in await adapter.list_comments(auth, conn, item["external_id"]):
        local = ours.get(comment["external_id"])
        if local:
            # shown as its Overwatch author wrote it, without the note added for the tracker
            comment = {**comment, "author": local["author"], "body": comment["body"].removesuffix(ATTRIBUTION.format(name=local["author"]["name"]))}
        comments.append({**comment, "id": local["_id"] if local else f"{item['source']}:{comment['external_id']}", **_rights(member, comment["author"]["id"], bool(local))})
    return comments


async def add(member: dict, item_id: str, body: str) -> dict:
    item, tracker = await _item_and_tracker(member, item_id)
    author = await _author(member)
    now = utcnow()
    doc = {"_id": uuid.uuid4().hex, "workspace_id": member["workspace_id"], "item_id": item_id, "author": author, "external_id": None, "created_at": now, "updated_at": now}
    if tracker:
        adapter, auth, conn = tracker
        posted = await adapter.add_comment(auth, conn, item["external_id"], body + ATTRIBUTION.format(name=author["name"]))
        doc["external_id"] = posted["external_id"]
    else:
        doc["body"] = body
    await mongo.work_item_comments().insert_one(doc)
    return {**doc, "body": body, "id": doc["_id"], **_rights(member, author["id"], True)}


async def _own(member: dict, item_id: str, comment_id: str) -> dict:
    doc = await mongo.work_item_comments().find_one({"_id": comment_id, "workspace_id": member["workspace_id"], "item_id": item_id})
    if doc is None:
        raise NotFoundError("comment not found", error_code="comment_not_found")
    return doc


async def update(member: dict, item_id: str, comment_id: str, body: str) -> dict:
    doc = await _own(member, item_id, comment_id)
    if doc["author"]["id"] != member["user_id"]:
        raise ForbiddenError("only its author can change a comment", error_code="not_comment_author")
    item, tracker = await _item_and_tracker(member, item_id)
    if tracker:
        adapter, auth, conn = tracker
        await adapter.update_comment(auth, conn, item["external_id"], doc["external_id"], body + ATTRIBUTION.format(name=doc["author"]["name"]))
    fields = {"updated_at": utcnow(), **({} if tracker else {"body": body})}
    await mongo.work_item_comments().update_one({"_id": comment_id}, {"$set": fields})
    return {**doc, **fields, "body": body, "id": comment_id, **_rights(member, doc["author"]["id"], True)}


async def delete(member: dict, item_id: str, comment_id: str) -> None:
    doc = await _own(member, item_id, comment_id)
    if not _rights(member, doc["author"]["id"], True)["deletable"]:
        raise ForbiddenError("only its author or an admin can delete a comment", error_code="not_comment_author")
    item, tracker = await _item_and_tracker(member, item_id)
    if tracker:
        adapter, auth, conn = tracker
        await adapter.delete_comment(auth, conn, item["external_id"], doc["external_id"])
    await mongo.work_item_comments().delete_one({"_id": comment_id})
