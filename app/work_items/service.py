"""Board items. Native items live only here; tracker items are changed in the tracker first.

A tracker write goes to the provider before anything is stored: if it refuses, nothing
changes here and the error reaches the board. On success the item is re-read from the
tracker, so the board shows exactly what the tracker now has.
"""

import re
import uuid

from app.auth.repository import get_user
from app.connectors import registry
from app.connectors import repository as connectors
from app.connectors.service import can_write
from app.core.constants import NATIVE_SOURCE, Role, StatusCategory
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.work_items import repository
from app.workspaces import repository as workspaces
from app.workspaces.service import rank

# below this gap between neighbours a midpoint loses precision, so the column is renumbered
MIN_RANK_GAP = 1e-6


def to_work_item(doc: dict, writable: set[str]) -> dict:
    return {**doc, "id": doc["_id"], "editable": doc["source"] == NATIVE_SOURCE or doc["source"] in writable}


async def _writable_sources(workspace_id: str) -> set[str]:
    return {c["provider"] for c in await connectors.list_connections(workspace_id) if can_write(c)}


async def _visibility(workspace_id: str) -> dict:
    """Native items, plus tracker items whose target is still connected."""
    by_provider: dict[str, list[str]] = {}
    for target in await connectors.list_targets(workspace_id):
        by_provider.setdefault(target["provider"], []).append(target["key"])
    return {"$or": [{"source": NATIVE_SOURCE}, *({"source": p, "target_key": {"$in": keys}} for p, keys in by_provider.items())]}


async def list_items(
    workspace_id: str, *, source: str | None, status_category: str | None, target_key: str | None,
    assignee_id: str | None, q: str | None, page: int, page_size: int, newest_first: bool = False,
) -> tuple[list[dict], int]:
    query: dict = {"workspace_id": workspace_id, **await _visibility(workspace_id)}
    filters = {"source": source, "status_category": status_category, "target_key": target_key, "assignee.id": assignee_id}
    query.update({k: v for k, v in filters.items() if v is not None})
    if q:
        query["$and"] = [{"$or": [
            {"title": {"$regex": re.escape(q), "$options": "i"}}, {"key": {"$regex": re.escape(q), "$options": "i"}},
        ]}]
    docs, total = await repository.page(query, (page - 1) * page_size, page_size, newest_first=newest_first)
    writable = await _writable_sources(workspace_id)
    return [to_work_item(d, writable) for d in docs], total


async def _get_visible(workspace_id: str, item_id: str) -> dict:
    item = await repository.get(workspace_id, item_id)
    if item and (item["source"] == NATIVE_SOURCE or await connectors.find_target(workspace_id, item["source"], item["target_key"])):
        return item
    raise NotFoundError("work item not found", error_code="work_item_not_found")


async def _writable_connection(workspace_id: str, provider: str) -> dict:
    conn = await connectors.active_connection(workspace_id, provider)
    if conn is None:
        raise ConflictError(f"{provider} is not connected", error_code=f"{provider}_not_connected")
    if not can_write(conn):
        raise ConflictError(
            f"reconnect {provider} to allow changes from Overwatch", error_code=f"{provider}_reconnect_required",
        )
    return conn


async def _assignee(workspace_id: str, user_id: str | None) -> dict | None:
    if user_id is None:
        return None
    user = await workspaces.membership(workspace_id, user_id) and await get_user(user_id)
    if not user:
        raise NotFoundError("assignee is not a member of this workspace", error_code="member_not_found")
    return {"id": user_id, "name": user.get("name") or user["github_login"]}


async def _refresh(workspace_id: str, conn: dict, target_key: str, external_id: str) -> dict:
    connector = registry.get(conn["provider"])
    raw = await connector.adapter.fetch(registry.auth_for(conn), conn, external_id)
    await repository.upsert_synced(workspace_id, connector.adapter.normalise(conn, target_key, raw))
    return await repository.find_external(workspace_id, conn["provider"], external_id)


async def _result(workspace_id: str, item_id: str) -> dict:
    return to_work_item(await repository.get(workspace_id, item_id), await _writable_sources(workspace_id))


async def create(member: dict, body) -> dict:
    workspace_id = member["workspace_id"]
    if body.provider:
        if not await connectors.find_target(workspace_id, body.provider, body.target_key):
            raise NotFoundError("not connected", error_code="target_not_found")
        conn = await _writable_connection(workspace_id, body.provider)
        adapter, auth = registry.get(body.provider).adapter, registry.auth_for(conn)
        fields = {"title": body.title, "description": body.description}
        if body.assignee_id:
            fields["assignee_id"] = body.assignee_id
        external_id = await adapter.create(auth, conn, body.target_key, fields)
        if body.status_category != StatusCategory.TODO:
            await adapter.move(auth, conn, external_id, body.status_category)
        item = await _refresh(workspace_id, conn, body.target_key, external_id)
        await repository.update(item["_id"], {"created_by": member["user_id"]})
        return await _result(workspace_id, item["_id"])

    now = utcnow()
    key = await repository.next_native_key(workspace_id)
    doc = {
        "_id": uuid.uuid4().hex, "workspace_id": workspace_id, "source": NATIVE_SOURCE, "external_id": key, "key": key,
        "target_key": None, "title": body.title, "description": body.description, "status": None,
        "status_category": body.status_category, "rank": now.timestamp(),
        "assignee": await _assignee(workspace_id, body.assignee_id), "priority": None, "issue_type": None,
        "external_url": None, "created_by": member["user_id"], "created_at": now, "updated_at": now,
    }
    await repository.insert(doc)
    return to_work_item(doc, set())


async def update(member: dict, item_id: str, body) -> dict:
    workspace_id = member["workspace_id"]
    item = await _get_visible(workspace_id, item_id)
    fields = body.model_dump(exclude_unset=True)
    if item["source"] == NATIVE_SOURCE:
        if "assignee_id" in fields:
            fields["assignee"] = await _assignee(workspace_id, fields.pop("assignee_id"))
        await repository.update(item_id, fields)
    elif fields:
        conn = await _writable_connection(workspace_id, item["source"])
        await registry.get(item["source"]).adapter.update(registry.auth_for(conn), conn, item["external_id"], fields)
        await _refresh(workspace_id, conn, item["target_key"], item["external_id"])
    return await _result(workspace_id, item_id)


async def _rank_between(workspace_id: str, category: str, before_id: str | None, after_id: str | None) -> float:
    ranks = await repository.ranks(workspace_id, [i for i in (before_id, after_id) if i])
    before, after = ranks.get(before_id), ranks.get(after_id)
    if before is not None and after is not None:
        if abs(after - before) < MIN_RANK_GAP:
            await repository.renumber_column(workspace_id, category)
            return await _rank_between(workspace_id, category, before_id, after_id)
        return (before + after) / 2
    if before is not None:
        return before + 1
    if after is not None:
        return after - 1
    return utcnow().timestamp()


async def move(member: dict, item_id: str, body) -> dict:
    workspace_id = member["workspace_id"]
    item = await _get_visible(workspace_id, item_id)
    if body.status_category != item["status_category"] and item["source"] != NATIVE_SOURCE:
        conn = await _writable_connection(workspace_id, item["source"])
        await registry.get(item["source"]).adapter.move(registry.auth_for(conn), conn, item["external_id"], body.status_category)
        await _refresh(workspace_id, conn, item["target_key"], item["external_id"])
    rank = await _rank_between(workspace_id, body.status_category, body.before_id, body.after_id)
    await repository.update(item_id, {"status_category": body.status_category, "rank": rank})
    return await _result(workspace_id, item_id)


async def delete(member: dict, item_id: str) -> None:
    workspace_id = member["workspace_id"]
    item = await _get_visible(workspace_id, item_id)
    if item["source"] != NATIVE_SOURCE:
        # it's deleted in the tracker under the connecting admin's account, and can't be undone
        if rank(member["role"]) < rank(Role.ADMIN):
            raise ForbiddenError(f"only admins delete {item['source']} items", error_code="insufficient_role")
        conn = await _writable_connection(workspace_id, item["source"])
        await registry.get(item["source"]).adapter.delete(registry.auth_for(conn), conn, item["external_id"])
    await repository.delete(item_id)
