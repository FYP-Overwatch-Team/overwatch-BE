"""The docs tree: folders and pages, written here or mirrored from a docs source (Confluence).

A mirrored page can be edited here when its source's connection can write: the edit is saved at
once and pushed back by the docs sync. Folders and the source's own structure stay read-only.
"""

import uuid
from datetime import timedelta

from app.auth.repository import get_user
from app.connectors import registry, repository as connectors
from app.connectors.doc_sync import push_delay
from app.connectors.service import can_write
from app.core.constants import NATIVE_SOURCE, DocPushState, DocumentKind
from app.core.exceptions import ConflictError, NotFoundError
from app.core.time import utcnow
from app.documents import attachments, repository
from app.documents.schemas import ArchiveChildren, ArchiveDocument, ConflictAction, CreateDocument, ResolveConflict, UpdateDocument

#: how deep the tree may nest, counting the node itself
MAX_DEPTH = 10
MIN_RANK_GAP = 1e-6


async def _author(user_id: str) -> dict:
    user = await get_user(user_id) or {}
    return {"id": user_id, "name": user.get("name") or user.get("github_login"), "avatar_url": user.get("avatar_url")}


async def writable_sources(workspace_id: str, docs: list[dict] | None = None) -> set[str]:
    """The docs sources this workspace can write to; skips the lookup when `docs` are all native."""
    if docs is not None and all(doc["source"] == NATIVE_SOURCE for doc in docs):
        return set()
    return {conn["provider"] for conn in await connectors.list_connections(workspace_id) if can_write(conn)}


def _editable(doc: dict, writable: set[str]) -> bool:
    if doc["source"] == NATIVE_SOURCE:
        return True
    return doc["kind"] == DocumentKind.PAGE and doc["source"] in writable and doc.get("push_state") != DocPushState.CONFLICT


def to_summary(doc: dict, writable: set[str]) -> dict:
    return {
        **doc, "id": doc["_id"], "has_children": doc.get("child_count", 0) > 0, "editable": _editable(doc, writable),
        # a mirrored node takes new pages (created at its source) while that source can be written to
        "can_add_pages": doc["source"] == NATIVE_SOURCE or doc["source"] in writable,
    }


async def _found(workspace_id: str, node_id: str, *, summary: bool = False) -> dict:
    doc = await repository.get(workspace_id, node_id, summary=summary)
    if not doc:
        raise NotFoundError("Document not found", error_code="document_not_found")
    return doc


async def _with_breadcrumbs(doc: dict) -> dict:
    ancestors = doc["ancestor_ids"]
    titles = await repository.titles(doc["workspace_id"], ancestors) if ancestors else {}
    crumbs = [{"id": node_id, "title": titles[node_id]} for node_id in ancestors if node_id in titles]
    urls = await attachments.signed_urls(doc["workspace_id"], doc.get("attachment_ids") or [])
    writable = await writable_sources(doc["workspace_id"], [doc])
    conflict = doc.get("conflict_content") and {
        "title": doc.get("conflict_title") or doc["title"], "content": doc["conflict_content"], "content_md": doc.get("conflict_content_md"),
    }
    return {**to_summary(doc, writable), "breadcrumbs": crumbs, "attachment_urls": urls, "conflict": conflict or None}


async def children(workspace_id: str, parent_id: str | None, offset: int, limit: int) -> tuple[list[dict], int]:
    if parent_id:
        await _found(workspace_id, parent_id, summary=True)
    docs, total = await repository.children(workspace_id, parent_id, offset, limit)
    writable = await writable_sources(workspace_id, docs)
    return [to_summary(doc, writable) for doc in docs], total


async def get(workspace_id: str, node_id: str) -> dict:
    return await _with_breadcrumbs(await _found(workspace_id, node_id))


async def _rank_between(workspace_id: str, parent_id: str | None, before_id: str | None, after_id: str | None, node_id: str | None) -> float:
    """A rank between the siblings it was placed between; after the last one when none is given.

    With one neighbour, the other is whatever actually follows (or precedes) it, so a partly
    loaded folder can't make a node jump past siblings the user didn't see.
    """
    ranks = await repository.ranks(workspace_id, [i for i in (before_id, after_id) if i])
    before, after = ranks.get(before_id), ranks.get(after_id)
    if before is None and after is None:
        last = await repository.adjacent_rank(workspace_id, parent_id, None, after=False, exclude_id=node_id)
        return 1.0 if last is None else last + 1
    if after is None:
        after = await repository.adjacent_rank(workspace_id, parent_id, before, after=True, exclude_id=node_id)
    elif before is None:
        before = await repository.adjacent_rank(workspace_id, parent_id, after, after=False, exclude_id=node_id)
    if before is not None and after is not None:
        if after - before < MIN_RANK_GAP:
            await repository.renumber(workspace_id, parent_id)
            return await _rank_between(workspace_id, parent_id, before_id, after_id, node_id)
        return (before + after) / 2
    return before + 1 if before is not None else after - 1


async def create(member: dict, body: CreateDocument) -> dict:
    workspace_id = member["workspace_id"]
    ancestors: list[str] = []
    parent = None
    if body.parent_id:
        parent = await _found(workspace_id, body.parent_id, summary=True)
        ancestors = [*parent["ancestor_ids"], parent["_id"]]
        # mirrored trees keep the depth they have at their source
        if parent["source"] == NATIVE_SOURCE and len(ancestors) >= MAX_DEPTH:
            raise ConflictError(f"Documents nest at most {MAX_DEPTH} deep", error_code="max_depth")
    mirrored = parent is not None and parent["source"] != NATIVE_SOURCE
    if mirrored and (body.kind != DocumentKind.PAGE or parent["source"] not in await writable_sources(workspace_id)):
        raise ConflictError("Only pages can be added here, while its source is connected for editing", error_code="parent_is_mirrored")
    now = utcnow()
    is_page = body.kind == DocumentKind.PAGE
    doc = {
        "_id": uuid.uuid4().hex,
        "workspace_id": workspace_id,
        "kind": body.kind,
        "title": body.title.strip(),
        "parent_id": body.parent_id,
        "ancestor_ids": ancestors,
        "rank": await _rank_between(workspace_id, body.parent_id, body.before_id, body.after_id, None),
        "child_count": 0,
        "content": (body.content or {"type": "doc", "content": []}) if is_page else None,
        "content_md": (body.content_md or "") if is_page else None,
        "version": 0,
        "source": NATIVE_SOURCE,
        "external_id": None,
        "target_key": None,
        "external_url": None,
        # stored, so reading a page never looks people up
        "author": await _author(member["user_id"]),
        "push_state": None,
        "created_by": member["user_id"],
        "updated_by": member["user_id"],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "archived_by": None,
        # the node an archive was started from; its whole group is restored together
        "archive_root_id": None,
    }
    if mirrored:
        doc.update(await _create_at_source(workspace_id, parent, doc))
    await repository.insert(doc)
    if body.parent_id:
        await repository.add_children(body.parent_id, 1)
    return await _with_breadcrumbs(doc)


async def _create_at_source(workspace_id: str, parent: dict, doc: dict) -> dict:
    """Create the page at the parent's source first, so it's mirrored from the start."""
    source = parent["source"]
    conn = await connectors.active_connection(workspace_id, source)
    # a target's own folder stands for its top level there
    parent_external = parent["external_id"] if parent["parent_id"] is not None else None
    # sources may want titles unique within a space, so an untitled page gets one of its own
    title = doc["title"] or f"Untitled {doc['_id'][:6]}"
    created = await registry.doc_source(source).create_page(
        registry.auth_for(conn), conn, parent["target_key"], parent_external, title, doc["content"],
    )
    return {
        "source": source, "target_key": parent["target_key"], "external_id": created["external_id"],
        "external_url": created["external_url"], "external_parent_id": parent_external, "external_version": created["external_version"],
        "push_state": DocPushState.NONE, "archived_by_sync": False, "title": created["title"],
    }


async def update(member: dict, node_id: str, body: UpdateDocument) -> dict:
    """One conditional write on the hot path (autosave); a refusal is explained by reading the node."""
    workspace_id = member["workspace_id"]
    fields: dict = {"updated_at": utcnow(), "updated_by": member["user_id"]}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.content is not None:
        fields["content"] = body.content
        fields["content_md"] = body.content_md or ""
        fields["attachment_ids"] = attachments.image_ids(body.content)
    doc = await _write(workspace_id, node_id, body, fields, NATIVE_SOURCE)
    current = None
    if doc is None:
        current = await _found(workspace_id, node_id, summary=True)
        if current["source"] != NATIVE_SOURCE and _editable(current, await writable_sources(workspace_id)):
            # mirrored: saved now, sent to the source once the edits settle
            fields.update(push_state=DocPushState.PENDING, push_due_at=utcnow() + push_delay(), push_error=None)
            doc = await _write(workspace_id, node_id, body, fields, current["source"], push_state={"$ne": DocPushState.CONFLICT})
    if doc is None:
        if not _editable(current, await writable_sources(workspace_id, [current])):
            raise ConflictError("This document is read-only here", error_code="read_only")
        if current["kind"] == DocumentKind.FOLDER:
            raise ConflictError("Folders have no content", error_code="folder_has_no_content")
        # someone saved since this edit began: the caller reloads instead of overwriting
        raise ConflictError("This page changed since you opened it", error_code="doc_conflict")
    return await _with_breadcrumbs(doc)


async def _write(workspace_id: str, node_id: str, body: UpdateDocument, fields: dict, source: str, **query) -> dict | None:
    if body.content is None:
        # a mirrored node's title is only editable on a page (folders are the source's structure)
        page_only = {"kind": DocumentKind.PAGE} if source != NATIVE_SOURCE else {}
        return await repository.update(workspace_id, node_id, fields, source=source, **page_only, **query)
    return await repository.save_content(workspace_id, node_id, body.base_version, fields, source=source, **query)


async def resolve_conflict(member: dict, node_id: str, body: ResolveConflict) -> dict:
    """Settle a page edited here and at its source at once: keep the source's version, or send ours over it."""
    workspace_id = member["workspace_id"]
    current = await _found(workspace_id, node_id)
    if current.get("push_state") != DocPushState.CONFLICT:
        raise ConflictError("This page has no conflict to resolve", error_code="no_conflict")
    fields: dict = {"push_state": DocPushState.NONE, "push_error": None}
    if body.action == ConflictAction.OVERWRITE:
        content = current["conflict_content"]
        fields.update(
            title=current.get("conflict_title") or current["title"], content=content, content_md=current.get("conflict_content_md") or "",
            attachment_ids=attachments.image_ids(content), push_state=DocPushState.PENDING, push_due_at=utcnow(),
            updated_at=utcnow(), updated_by=member["user_id"],
        )
    doc = await repository.resolve_conflict(workspace_id, node_id, body.base_version, fields)
    if doc is None:
        raise ConflictError("This page changed since you opened it", error_code="doc_conflict")
    return await _with_breadcrumbs(doc)


async def _move_children_out(workspace_id: str, node: dict, move_to: str | None) -> None:
    """Put a node's children (with everything under them) at the end of `move_to`, or of the top level."""
    prefix: list[str] = []
    if move_to:
        if move_to == node["_id"]:
            raise ConflictError("Pick a folder outside the one being archived", error_code="move_into_archived")
        target = await _found(workspace_id, move_to, summary=True)
        if node["_id"] in target["ancestor_ids"]:
            raise ConflictError("Pick a folder outside the one being archived", error_code="move_into_archived")
        if target["source"] != NATIVE_SOURCE:
            raise ConflictError("Mirrored documents can't be added to here yet", error_code="parent_is_mirrored")
        prefix = [*target["ancestor_ids"], move_to]
    old_prefix_len = len(node["ancestor_ids"]) + 1
    # every node keeps its depth below the moved child; the deepest must still fit
    if await repository.deepest(workspace_id, node["_id"]) - old_prefix_len + len(prefix) >= MAX_DEPTH:
        raise ConflictError(f"Documents nest at most {MAX_DEPTH} deep", error_code="max_depth")
    children = await repository.child_ids(workspace_id, node["_id"])
    last = await repository.adjacent_rank(workspace_id, move_to, None, after=False, exclude_id=None)
    await repository.rebase(_subtree(workspace_id, node["_id"]), old_prefix_len, prefix, MAX_DEPTH)
    await repository.place_children(children, move_to, 1.0 if last is None else last + 1)
    if move_to:
        await repository.add_children(move_to, len(children))


def _subtree(workspace_id: str, node_id: str) -> dict:
    return {"workspace_id": workspace_id, "archived_at": None, "ancestor_ids": node_id}


async def archive(member: dict, node_id: str, body: ArchiveDocument) -> None:
    """Archive a node with what's inside it, or after moving what's inside elsewhere.

    The children move first: a failure part way leaves them moved and the node still live,
    never anything stranded inside an archived node.
    """
    workspace_id = member["workspace_id"]
    node = await _found(workspace_id, node_id, summary=True)
    if not _editable(node, await writable_sources(workspace_id, [node])):
        raise ConflictError("This document is read-only here", error_code="read_only")
    move = body.children == ArchiveChildren.MOVE
    if node["source"] != NATIVE_SOURCE:
        if move and node.get("child_count", 0):
            raise ConflictError("Pages from a source are archived with what's under them", error_code="mirrored_children_stay")
        await _archive_at_source(workspace_id, node)
    if move and node.get("child_count", 0):
        await _move_children_out(workspace_id, node, body.move_to)
    await repository.archive(workspace_id, node_id, with_subtree=not move, fields={"archived_at": utcnow(), "archived_by": member["user_id"]})
    if node["parent_id"]:
        await repository.add_children(node["parent_id"], -1)


async def _archive_at_source(workspace_id: str, node: dict) -> None:
    """Archive the page and the pages under it at their source too, before they leave the tree here."""
    source = node["source"]
    conn = await connectors.active_connection(workspace_id, source)
    pages = await repository.mirrored_pages(workspace_id, node["_id"], source)
    await registry.doc_source(source).archive_pages(registry.auth_for(conn), conn, [page["external_id"] for page in pages])


async def archived(workspace_id: str, offset: int, limit: int) -> tuple[list[dict], int]:
    roots, total = await repository.archived(workspace_id, offset, limit)
    sizes = await repository.group_sizes(workspace_id, [doc["_id"] for doc in roots]) if roots else {}
    return [{**doc, "id": doc["_id"], "archived_with": sizes.get(doc["_id"], 1) - 1} for doc in roots], total


async def restore(member: dict, node_id: str) -> dict:
    """Bring an archive back where it was; to the top level when its folder is gone (or archived)."""
    workspace_id = member["workspace_id"]
    root = await repository.get_archived_root(workspace_id, node_id)
    if not root:
        raise NotFoundError("Nothing archived by that id", error_code="archive_not_found")
    if root["source"] != NATIVE_SOURCE:
        # the next sync would archive it again: it comes back once it's restored at the source
        raise ConflictError("Restore it at its source; it comes back here on the next sync", error_code="restore_at_source")
    parent_id = root["parent_id"]
    if parent_id and not await repository.get(workspace_id, parent_id, summary=True):
        group = {"workspace_id": workspace_id, "archive_root_id": node_id}
        await repository.rebase(group, len(root["ancestor_ids"]), [], MAX_DEPTH)
        last = await repository.adjacent_rank(workspace_id, None, None, after=False, exclude_id=None)
        await repository.place_children([node_id], None, 1.0 if last is None else last + 1)
        parent_id = None
    await repository.restore(workspace_id, node_id)
    if parent_id:
        await repository.add_children(parent_id, 1)
    return await get(workspace_id, node_id)


async def delete_archived(member: dict, node_id: str) -> None:
    """Delete an archive for good, with its images; pages archived here from a source are deleted there too.

    Pages the sync archived (gone at the source, or a space no longer synced) are only deleted here.
    """
    workspace_id = member["workspace_id"]
    if not await repository.get_archived_root(workspace_id, node_id):
        raise NotFoundError("Nothing archived by that id", error_code="archive_not_found")
    group = await repository.archive_group(workspace_id, node_id)
    at_source: dict[str, list[str]] = {}
    for doc in group:
        if doc["source"] != NATIVE_SOURCE and doc["kind"] == DocumentKind.PAGE and not doc.get("archived_by_sync") and doc.get("external_id"):
            at_source.setdefault(doc["source"], []).append(doc["external_id"])
    for source, external_ids in at_source.items():
        conn = await connectors.active_connection(workspace_id, source)
        if not can_write(conn):
            raise ConflictError(f"Reconnect {registry.get(source).name} to delete these pages there too", error_code=f"{source}_not_connected")
        adapter, auth = registry.doc_source(source), registry.auth_for(conn)
        for external_id in external_ids:
            await adapter.delete_page(auth, conn, external_id)
    await _purge(workspace_id, node_id, [doc["_id"] for doc in group])


async def _purge(workspace_id: str, root_id: str, node_ids: list[str]) -> None:
    await attachments.delete_for(workspace_id, node_ids)
    await repository.delete_group(workspace_id, root_id)


async def purge_expired(retention: timedelta) -> int:
    """Delete archives older than `retention` here; their source copies (if any) are left as they are."""
    purged = 0
    async for root in repository.archived_before(utcnow() - retention):
        group = await repository.archive_group(root["workspace_id"], root["_id"])
        await _purge(root["workspace_id"], root["_id"], [doc["_id"] for doc in group])
        purged += 1
    return purged
