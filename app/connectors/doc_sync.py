"""Docs sync: a docs source's pages mirrored into `documents`, and edits made here pushed back.

Pull: polling, like the work-item sync. Incremental runs read the pages changed since the last
run; a full run (first, then hourly) reads every page, so pages archived or deleted at the source
leave the mirror. The tree is rebuilt in memory from every stored node plus what was read, and
only the nodes that changed are written. Content is fetched only for pages whose version moved.

Push: saving a mirrored page here marks it pending; once nobody has typed for a while, the push
loop sends it as one new version. If the source moved meanwhile, its version is loaded and the
local edit kept beside it for the user to resolve, never overwritten silently.
"""

import asyncio
import uuid
from datetime import timedelta

import structlog
from pymongo import InsertOne, UpdateOne

from app.auth.repository import get_user
from app.connectors import registry, repository as connectors
from app.connectors.adf import EDITOR_FORMAT
from app.connectors.ports import AuthBackend, DocSource, RemoteConflict
from app.connectors.service import can_write
from app.connectors.sync import OVERLAP_MINUTES, run_target
from app.core.config import get_settings
from app.core.constants import DocPushState, DocumentKind
from app.core.exceptions import AppError, ExternalServiceError
from app.core.time import utcnow
from app.documents import attachments, repository as documents

logger = structlog.get_logger("app.connectors.doc_sync")

# pages whose images and content are fetched at once
BODY_BATCH = 50
# requests to the source in flight at once while fetching images
CONCURRENCY = 4
# a push older than this was lost (a restart mid-push) and is retried
STALE_PUSH = timedelta(minutes=10)
PUSH_RETRY = timedelta(minutes=5)
# pages pushed per tick, so one busy workspace can't hold the loop
PUSH_BATCH = 50
# what the tree, or a node within it, sorts by when the source gives no position
LAST = 1e12
# the fields a sync owns on each node; anything else is left alone
TREE_FIELDS = ("kind", "title", "parent_id", "ancestor_ids", "rank", "child_count", "external_parent_id", "external_url", "position")


async def run_document_sync(workspace_id: str, provider: str, target_key: str) -> None:
    await run_target(workspace_id, provider, target_key, _sync)


async def _sync(workspace_id: str, provider: str, target_key: str, target: dict, full: bool) -> int:
    conn = await connectors.active_connection(workspace_id, provider)
    if conn is None:
        raise ExternalServiceError(f"{provider} is not connected", error_code=f"{provider}_not_connected")
    adapter, auth = registry.doc_source(provider), registry.auth_for(conn)
    stored = await documents.mirror(workspace_id, provider, target_key)
    root = next((doc for doc in stored if doc["parent_id"] is None), None)
    full = full or root is None
    now = utcnow()

    if full:
        root = await _root(workspace_id, provider, target_key, root, await adapter.target_root(auth, conn, target_key), now)
    changed_since = None if full else target["last_synced_at"] - timedelta(minutes=OVERLAP_MINUTES)
    items: list[dict] = []
    token, seen_tokens = None, set()
    while True:
        page = await adapter.list_nodes(auth, conn, target_key, changed_since=changed_since, page_token=token)
        items.extend(page.items)
        token = page.next_token
        if not token:
            break
        if token in seen_tokens:
            raise ExternalServiceError(f"{provider} returned a repeated page token", error_code=f"{provider}_sync_failed")
        seen_tokens.add(token)

    by_external = {doc["external_id"]: doc for doc in stored if doc["_id"] != root["_id"]}
    ops, ids = _tree(workspace_id, provider, target_key, root, by_external, items, full, now)
    await documents.write_mirror(ops)
    await _contents(workspace_id, provider, adapter, auth, conn, by_external, ids, items, now)
    return len(items)


async def _root(workspace_id: str, provider: str, target_key: str, root: dict | None, info: dict, now) -> dict:
    """The target's top-level folder, created on its first sync and brought back if it was re-added."""
    fields = {"title": info["title"], "external_url": info["external_url"]}
    if root is None:
        last = await documents.adjacent_rank(workspace_id, None, None, after=False, exclude_id=None)
        root = _node(workspace_id, provider, target_key, uuid.uuid4().hex, DocumentKind.FOLDER, now)
        root.update(fields, external_id=info["external_id"], rank=1.0 if last is None else last + 1)
        await documents.insert(root)
        return root
    if root.get("archived_by_sync"):
        # only the folder: each page comes back as this sync sees it at the source
        fields.update(archived_at=None, archived_by=None, archive_root_id=None, archived_root=False, archived_by_sync=False)
    await documents.write_mirror([UpdateOne({"_id": root["_id"]}, {"$set": fields})])
    return {**root, **fields}


def _node(workspace_id: str, provider: str, target_key: str, node_id: str, kind: str, now) -> dict:
    is_page = kind == DocumentKind.PAGE
    return {
        "_id": node_id, "workspace_id": workspace_id, "kind": kind, "title": "", "parent_id": None, "ancestor_ids": [],
        "rank": LAST, "child_count": 0, "content": {"type": "doc", "content": []} if is_page else None,
        "content_md": "" if is_page else None, "version": 0, "source": provider, "external_id": None,
        "target_key": target_key, "external_url": None, "external_parent_id": None, "external_version": None,
        "push_state": DocPushState.NONE if is_page else None, "author": None, "created_by": None, "updated_by": None,
        "created_at": now, "updated_at": now, "archived_at": None, "archived_by": None, "archive_root_id": None,
        "archived_by_sync": False,
    }


def _tree(
    workspace_id: str, provider: str, target_key: str, root: dict, stored: dict[str, dict], items: list[dict], full: bool, now,
) -> tuple[list, dict[str, str]]:
    """The writes that bring the stored mirror in line with what was read, and every node's id by external id.

    Every stored node takes part, so a page moved at the source moves its whole subtree here too.
    """
    read = {item["external_id"]: item for item in items}
    ids = {external_id: doc["_id"] for external_id, doc in stored.items()}
    live: dict[str, bool] = {}
    for external_id, doc in stored.items():
        item = read.get(external_id)
        user_archived = doc["archived_at"] is not None and not doc.get("archived_by_sync")
        if user_archived:
            live[external_id] = False
        elif item is not None:
            live[external_id] = item["status"] not in ("archived", "trashed", "deleted")
        else:
            # a full run reads everything, so what it didn't see is gone at the source
            live[external_id] = doc["archived_at"] is None and not full
    for external_id, item in read.items():
        if external_id not in stored and item["status"] not in ("archived", "trashed", "deleted"):
            ids[external_id] = uuid.uuid4().hex
            live[external_id] = True

    def parent_of(external_id: str) -> str | None:
        item = read.get(external_id)
        return item["parent_external_id"] if item else stored[external_id].get("external_parent_id")

    ancestors: dict[str, list[str]] = {}

    def ancestors_of(external_id: str, depth: int = 0) -> list[str]:
        if external_id not in ancestors:
            parent = parent_of(external_id)
            # a parent that isn't mirrored (or a loop) puts the node at the target's top level
            if parent and live.get(parent) and depth < 200:
                ancestors[external_id] = [*ancestors_of(parent, depth + 1), ids[parent]]
            else:
                ancestors[external_id] = [root["_id"]]
        return ancestors[external_id]

    children: dict[str, int] = {}
    for external_id in ids:
        if live.get(external_id):
            parent_id = ancestors_of(external_id)[-1]
            children[parent_id] = children.get(parent_id, 0) + 1

    # gone since the last sync, or archived with the target when it was removed and not back at the source
    gone_now = {
        e for e in stored
        if not live[e] and (stored[e]["archived_at"] is None or (stored[e].get("archived_by_sync") and stored[e].get("archive_root_id") == root["_id"]))
    }
    ops: list = []
    for external_id, node_id in ids.items():
        doc = stored.get(external_id)
        item = read.get(external_id)
        if not live[external_id]:
            if external_id in gone_now:
                # archived as groups: a gone page's gone children go with it
                group = next((ids[a] for a in _external_chain(external_id, parent_of) if a in gone_now), node_id)
                ops.append(UpdateOne({"_id": node_id}, {"$set": {
                    "archived_at": now, "archived_by": None, "archived_by_sync": True, "archive_root_id": group,
                    "archived_root": group == node_id,
                }}))
            continue
        chain = ancestors_of(external_id)
        fields = {
            "ancestor_ids": chain, "parent_id": chain[-1], "child_count": children.get(node_id, 0),
            "external_parent_id": parent_of(external_id),
        }
        if item:
            fields.update(
                kind=item["kind"], title=item["title"], external_url=item["external_url"], position=item["position"],
                rank=float(item["position"]) if item["position"] is not None else LAST,
            )
        if doc is None:
            node = _node(workspace_id, provider, target_key, node_id, item["kind"], now)
            node.update(fields, external_id=external_id, updated_at=item["updated_at"] or now)
            ops.append(InsertOne(node))
            continue
        changed = {k: v for k, v in fields.items() if doc.get(k) != v}
        if doc.get("archived_by_sync"):
            changed.update(archived_at=None, archived_by=None, archived_by_sync=False, archive_root_id=None, archived_root=False)
        if changed:
            ops.append(UpdateOne({"_id": node_id}, {"$set": changed}))
    ops.append(UpdateOne({"_id": root["_id"]}, {"$set": {"child_count": children.get(root["_id"], 0)}}))
    return ops, ids


def _external_chain(external_id: str, parent_of) -> list[str]:
    """A node's external ancestors, outermost first."""
    chain: list[str] = []
    parent = parent_of(external_id)
    while parent and parent not in chain and len(chain) < 200:
        chain.append(parent)
        parent = parent_of(parent) if parent else None
    return list(reversed(chain))


async def _contents(
    workspace_id: str, provider: str, adapter: DocSource, auth: AuthBackend, conn: dict,
    stored: dict[str, dict], ids: dict[str, str], items: list[dict], now,
) -> None:
    """Fetch the content of pages whose version moved, or that were converted by an older editor format,
    with their images, in batches.

    Pages with edits on their way to the source are left to the push: its own write moves the source's
    version too, so only the push can tell a real conflict from its own save.
    """
    stale = [
        item for item in items
        if item["kind"] == DocumentKind.PAGE and item["external_id"] in ids
        and item["status"] not in ("archived", "trashed", "deleted")
        and (item["external_id"] not in stored or (
            (stored[item["external_id"]].get("external_version") != item["external_version"]
             or stored[item["external_id"]].get("editor_format") != EDITOR_FORMAT)
            and stored[item["external_id"]].get("push_state") not in (DocPushState.PENDING, DocPushState.PUSHING)
        ))
    ]
    limit = asyncio.Semaphore(CONCURRENCY)
    for start in range(0, len(stale), BODY_BATCH):
        batch = stale[start:start + BODY_BATCH]
        maps = await asyncio.gather(*(
            _copy_images(workspace_id, provider, adapter, auth, conn, item["external_id"], ids[item["external_id"]], limit) for item in batch
        ))
        images = {media_id: attachment_id for found in maps for media_id, attachment_id in found.items()}
        bodies = await adapter.fetch_bodies(auth, conn, [item["external_id"] for item in batch], images)
        for item in batch:
            body = bodies.get(item["external_id"])
            if body:
                await _take_remote(stored.get(item["external_id"]), ids[item["external_id"]], body, now)


async def _copy_images(
    workspace_id: str, provider: str, adapter: DocSource, auth: AuthBackend, conn: dict, external_id: str, node_id: str,
    limit: asyncio.Semaphore,
) -> dict[str, str]:
    """A page's images copied into our storage (each once per version): media id → attachment id."""
    async with limit:
        files = await adapter.list_attachments(auth, conn, external_id)
    copies = await attachments.source_copies(workspace_id, provider, [f["external_id"] for f in files])
    found: dict[str, str] = {}
    for file in files:
        copy = copies.get(file["external_id"])
        if copy and copy.get("external_version") == file["external_version"]:
            attachment_id = copy["_id"]
        elif file["size"] > attachments.MAX_BYTES:
            continue
        else:
            try:
                async with limit:
                    data = await adapter.download_attachment(auth, conn, external_id, file["external_id"])
            except AppError:
                logger.warning("doc_image_unavailable", provider=provider, page=external_id, exc_info=True)
                continue
            attachment_id = await attachments.copy_from_source(workspace_id, node_id, provider, file, data)
        if attachment_id and file.get("media_id"):
            found[file["media_id"]] = attachment_id
    return found


async def _take_remote(doc: dict | None, node_id: str, body: dict, now) -> None:
    """Store the source's content for a page. Called by a push that hit a conflict, the local edit is kept beside it."""
    fields = {
        "title": body["title"], "content": body["content"], "content_md": body["content_md"],
        "external_version": body["external_version"], "attachment_ids": attachments.image_ids(body["content"]),
        "editor_format": EDITOR_FORMAT, "synced_at": now, "updated_at": now,
    }
    state = (doc or {}).get("push_state") or DocPushState.NONE
    expected: dict = {"push_state": state}
    if doc is not None:
        expected["version"] = doc["version"]
    if state == DocPushState.PUSHING:
        full = await documents.get(doc["workspace_id"], node_id)
        fields.update(
            push_state=DocPushState.CONFLICT, push_error=None,
            conflict_content=full["content"], conflict_content_md=full.get("content_md"), conflict_title=full["title"],
        )
    # a save here in between wins; the next sync tries again, since the version still differs
    await documents.save_synced(node_id, expected, fields)


async def archive_target(workspace_id: str, provider: str, target_key: str) -> None:
    """A target no longer synced: its mirror goes to the Archive as one group, and comes back if it's re-added."""
    stored = await documents.mirror(workspace_id, provider, target_key)
    root = next((doc for doc in stored if doc["parent_id"] is None), None)
    if root:
        await documents.archive_target(
            workspace_id, provider, target_key, root["_id"], {"archived_at": utcnow(), "archived_by": None, "archived_by_sync": True},
        )


async def push_due() -> None:
    """Send pages whose edits have settled back to their source."""
    for _ in range(PUSH_BATCH):
        now = utcnow()
        doc = await documents.claim_push(now, now - STALE_PUSH)
        if doc is None:
            return
        try:
            await _push(doc)
        except Exception:
            logger.exception("doc_push_failed", node=doc["_id"])
            await documents.push_later(doc["_id"], utcnow() + PUSH_RETRY, "Couldn't reach the source; retrying")


async def _push(doc: dict) -> None:
    provider, now = doc["source"], utcnow()
    conn = await connectors.active_connection(doc["workspace_id"], provider)
    if not can_write(conn):
        await documents.push_later(doc["_id"], now + PUSH_RETRY, f"{registry.get(provider).name} isn't connected for editing")
        return
    adapter, auth = registry.doc_source(provider), registry.auth_for(conn)
    editor = await get_user(doc["updated_by"]) if doc.get("updated_by") else None
    name = (editor or {}).get("name") or (editor or {}).get("github_login") or "someone"
    try:
        version = await adapter.update_page(
            auth, conn, doc["external_id"], title=doc["title"], content=doc["content"],
            base_version=doc["external_version"] or 0, message=f"Edited by {name} via Overwatch",
        )
    except RemoteConflict:
        images = await attachments.media_map(doc["workspace_id"], doc["_id"])
        body = (await adapter.fetch_bodies(auth, conn, [doc["external_id"]], images)).get(doc["external_id"])
        if body:
            await _take_remote(doc, doc["_id"], body, now)
        else:
            await documents.push_later(doc["_id"], now + PUSH_RETRY, "The page is gone at the source")
        return
    except AppError as exc:
        await documents.push_later(doc["_id"], now + PUSH_RETRY, exc.message)
        return
    await documents.push_done(doc["_id"], version, now)


def push_delay() -> timedelta:
    return timedelta(seconds=get_settings().doc_push_idle_seconds)
