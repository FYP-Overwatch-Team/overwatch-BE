"""The docs tree: folders and pages, written here or (later) mirrored from a docs provider."""

import uuid

from app.core.constants import NATIVE_SOURCE, DocumentKind
from app.core.exceptions import ConflictError, NotFoundError
from app.auth.repository import get_user
from app.core.time import utcnow
from app.documents import attachments, repository
from app.documents.schemas import ArchiveChildren, ArchiveDocument, CreateDocument, UpdateDocument

#: how deep the tree may nest, counting the node itself
MAX_DEPTH = 10
MIN_RANK_GAP = 1e-6


async def _author(user_id: str) -> dict:
    user = await get_user(user_id) or {}
    return {"id": user_id, "name": user.get("name") or user.get("github_login"), "avatar_url": user.get("avatar_url")}


def _editable(doc: dict) -> bool:
    return doc["source"] == NATIVE_SOURCE


def to_summary(doc: dict) -> dict:
    return {**doc, "id": doc["_id"], "has_children": doc.get("child_count", 0) > 0, "editable": _editable(doc)}


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
    return {**to_summary(doc), "breadcrumbs": crumbs, "attachment_urls": urls}


async def children(workspace_id: str, parent_id: str | None, offset: int, limit: int) -> tuple[list[dict], int]:
    if parent_id:
        await _found(workspace_id, parent_id, summary=True)
    docs, total = await repository.children(workspace_id, parent_id, offset, limit)
    return [to_summary(doc) for doc in docs], total


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
    if body.parent_id:
        parent = await _found(workspace_id, body.parent_id, summary=True)
        if not _editable(parent):
            raise ConflictError("Mirrored documents can't be added to here yet", error_code="parent_is_mirrored")
        ancestors = [*parent["ancestor_ids"], parent["_id"]]
        if len(ancestors) >= MAX_DEPTH:
            raise ConflictError(f"Documents nest at most {MAX_DEPTH} deep", error_code="max_depth")
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
        "created_by": member["user_id"],
        "updated_by": member["user_id"],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "archived_by": None,
        # the node an archive was started from; its whole group is restored together
        "archive_root_id": None,
    }
    await repository.insert(doc)
    if body.parent_id:
        await repository.add_children(body.parent_id, 1)
    return await _with_breadcrumbs(doc)


async def update(member: dict, node_id: str, body: UpdateDocument) -> dict:
    """One conditional write on the hot path (autosave); a refusal is explained by reading the node."""
    workspace_id = member["workspace_id"]
    fields: dict = {"updated_at": utcnow(), "updated_by": member["user_id"]}
    if body.title is not None:
        fields["title"] = body.title.strip()
    if body.content is None:
        doc = await repository.update(workspace_id, node_id, fields, source=NATIVE_SOURCE)
    else:
        fields["content"] = body.content
        fields["content_md"] = body.content_md or ""
        fields["attachment_ids"] = attachments.image_ids(body.content)
        doc = await repository.save_content(workspace_id, node_id, body.base_version, fields, source=NATIVE_SOURCE)
    if doc is None:
        current = await _found(workspace_id, node_id, summary=True)
        if not _editable(current):
            raise ConflictError("This document is read-only here", error_code="read_only")
        if current["kind"] == DocumentKind.FOLDER:
            raise ConflictError("Folders have no content", error_code="folder_has_no_content")
        # someone saved since this edit began: the caller reloads instead of overwriting
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
        if not _editable(target):
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
    if not _editable(node):
        raise ConflictError("This document is read-only here", error_code="read_only")
    move = body.children == ArchiveChildren.MOVE
    if move and node.get("child_count", 0):
        await _move_children_out(workspace_id, node, body.move_to)
    await repository.archive(workspace_id, node_id, with_subtree=not move, fields={"archived_at": utcnow(), "archived_by": member["user_id"]})
    if node["parent_id"]:
        await repository.add_children(node["parent_id"], -1)


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
