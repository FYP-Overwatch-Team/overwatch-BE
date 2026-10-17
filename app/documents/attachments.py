"""Images in docs pages: uploaded straight to storage, checked, then shown through signed links."""

import uuid

from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import workspace_member
from app.core.constants import NATIVE_SOURCE, DocumentKind
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.responses import api_router
from app.core.time import utcnow
from app.db import mongo
from app.documents import repository
from app.storage import keys
from app.storage.proxied import valid_signature
from app.storage.store import ProxiedStore, get_object_store

MAX_BYTES = 10 * 1024 * 1024
#: what a page may show; no SVG, which can carry scripts
IMAGE_TYPES = set(keys.IMAGE_EXTENSIONS)
#: how long a link to a file works; the page asks for fresh ones each time it loads
LINK_TTL_SECONDS = 12 * 3600
PENDING, READY = "pending", "ready"


class NewAttachment(BaseModel):
    filename: str = Field(max_length=255)
    content_type: str
    size: int = Field(gt=0)


class UploadTarget(BaseModel):
    """How the browser sends the file; it follows this as given, so it never needs to know the provider."""

    method: str
    url: str
    fields: dict[str, str]
    headers: dict[str, str]
    #: true when the file goes to our API (dev); false when straight to storage
    via_api: bool


class PendingAttachment(BaseModel):
    id: str
    upload: UploadTarget


class Attachment(BaseModel):
    id: str
    url: str


router = api_router(tags=["attachments"])


def _check_file(content_type: str, size: int) -> None:
    if content_type not in IMAGE_TYPES:
        raise AppError("Only PNG, JPEG, GIF and WebP images can be added", error_code="unsupported_file_type", status_code=415)
    if size > MAX_BYTES:
        raise AppError(f"Images can be at most {MAX_BYTES // (1024 * 1024)} MB", error_code="file_too_large", status_code=413)


def _sniffed_type(data: bytes) -> str | None:
    """The image type from the file's first bytes, so a renamed file can't pass as an image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _pending(workspace_id: str, attachment_id: str) -> dict:
    doc = await mongo.doc_attachments().find_one({"_id": attachment_id, "workspace_id": workspace_id})
    if not doc:
        raise NotFoundError("Upload not found", error_code="attachment_not_found")
    return doc


@router.post("/documents/{node_id}/attachments", status_code=201)
async def start_upload(node_id: str, body: NewAttachment, member: dict = Depends(workspace_member())) -> PendingAttachment:
    workspace_id = member["workspace_id"]
    _check_file(body.content_type, body.size)
    node = await repository.get(workspace_id, node_id, summary=True)
    if not node:
        raise NotFoundError("Document not found", error_code="document_not_found")
    if node["kind"] != DocumentKind.PAGE or node["source"] != NATIVE_SOURCE:
        raise ConflictError("Images can only be added to pages written here", error_code="read_only")
    attachment_id = uuid.uuid4().hex
    key = keys.doc_image(workspace_id, node_id, attachment_id, body.content_type)
    await mongo.doc_attachments().insert_one({
        "_id": attachment_id, "workspace_id": workspace_id, "node_id": node_id, "key": key,
        "filename": body.filename, "content_type": body.content_type, "size": body.size,
        "status": PENDING, "created_by": member["user_id"], "created_at": utcnow(),
    })
    target = get_object_store().upload_target(attachment_id, key, body.content_type, body.size)
    return {"id": attachment_id, "upload": target}


@router.put("/attachments/{attachment_id}/content")
async def upload_content(attachment_id: str, request: Request, member: dict = Depends(workspace_member())) -> None:
    """The file itself, when it goes through our API (dev); on S3 it goes straight to the bucket."""
    store = get_object_store()
    if not isinstance(store, ProxiedStore):
        raise NotFoundError("Uploads go straight to storage", error_code="not_found")
    record = await _pending(member["workspace_id"], attachment_id)
    if record["status"] != PENDING:
        raise ConflictError("This upload is finished", error_code="upload_finished")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise AppError("Images can be at most 10 MB", error_code="file_too_large", status_code=413)
    if _sniffed_type(bytes(data)) != record["content_type"]:
        raise AppError("That file isn't the image it says it is", error_code="unsupported_file_type", status_code=415)
    await store.write(record["key"], bytes(data), record["content_type"])


@router.post("/attachments/{attachment_id}/complete")
async def complete_upload(attachment_id: str, member: dict = Depends(workspace_member())) -> Attachment:
    """Checks what actually arrived, then the image can be shown."""
    record = await _pending(member["workspace_id"], attachment_id)
    store = get_object_store()
    stored = await store.head(record["key"])
    if not stored:
        raise ConflictError("The file hasn't arrived yet", error_code="upload_missing")
    if stored["size"] > MAX_BYTES or stored["content_type"] != record["content_type"]:
        await store.delete(record["key"])
        raise AppError("That file can't be added", error_code="unsupported_file_type", status_code=415)
    await mongo.doc_attachments().update_one({"_id": attachment_id}, {"$set": {"status": READY, "size": stored["size"]}})
    return {"id": attachment_id, "url": store.download_url(attachment_id, record["key"], LINK_TTL_SECONDS)}


@router.get("/attachments/{attachment_id}/file", response_model=None)
async def read_file(attachment_id: str, expires: int = Query(...), signature: str = Query(...)) -> Response:
    """A signed link's file, when files go through our API (dev). Images can't send a login header."""
    store = get_object_store()
    if not isinstance(store, ProxiedStore) or not valid_signature(attachment_id, expires, signature):
        raise NotFoundError("File not found", error_code="not_found")
    record = await mongo.doc_attachments().find_one({"_id": attachment_id, "status": READY})
    found = record and await store.read(record["key"])
    if not found:
        raise NotFoundError("File not found", error_code="not_found")
    data, content_type = found
    return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


async def signed_urls(workspace_id: str, attachment_ids: list[str]) -> dict[str, str]:
    """Links to a page's ready images, one query and no storage calls (signing is local)."""
    if not attachment_ids:
        return {}
    store = get_object_store()
    cursor = mongo.doc_attachments().find({"workspace_id": workspace_id, "_id": {"$in": attachment_ids}, "status": READY}, {"key": 1})
    return {doc["_id"]: store.download_url(doc["_id"], doc["key"], LINK_TTL_SECONDS) async for doc in cursor}


def image_ids(content: dict) -> list[str]:
    """The uploaded images a page's content shows, in order, once each."""
    found: dict[str, None] = {}
    stack = [content]
    while stack:
        node = stack.pop()
        if node.get("type") == "image" and (attachment_id := (node.get("attrs") or {}).get("attachmentId")):
            found[attachment_id] = None
        stack.extend(reversed(node.get("content") or []))
    return list(found)


async def source_copies(workspace_id: str, source: str, external_ids: list[str]) -> dict[str, dict]:
    """Images already copied from a docs source, by their id there."""
    cursor = mongo.doc_attachments().find({"workspace_id": workspace_id, "source": source, "external_id": {"$in": external_ids}})
    return {doc["external_id"]: doc async for doc in cursor}


async def copy_from_source(workspace_id: str, node_id: str, source: str, file: dict, data: bytes) -> str | None:
    """Keep our own copy of an image from a docs source, so pages show it without the source's auth.

    `file` is the source's description ({external_id, media_id, filename, content_type, external_version}).
    None when it isn't an image we can show.
    """
    content_type = _sniffed_type(data)
    if content_type not in IMAGE_TYPES or len(data) > MAX_BYTES:
        return None
    existing = await mongo.doc_attachments().find_one({"workspace_id": workspace_id, "source": source, "external_id": file["external_id"]})
    attachment_id = existing["_id"] if existing else uuid.uuid4().hex
    key = keys.doc_image(workspace_id, node_id, attachment_id, content_type)
    store = get_object_store()
    await store.write(key, data, content_type)
    await mongo.doc_attachments().update_one({"_id": attachment_id}, {"$set": {
        "workspace_id": workspace_id, "node_id": node_id, "key": key, "filename": file["filename"][:255],
        "content_type": content_type, "size": len(data), "status": READY, "source": source,
        "external_id": file["external_id"], "media_id": file.get("media_id"), "external_version": file.get("external_version"),
        "created_by": None, "created_at": utcnow(),
    }}, upsert=True)
    if existing and existing["key"] != key:
        # the copy moved (a new layout, page or type); only now that the record points at it is the old file unused
        await store.delete(existing["key"])
    return attachment_id


async def media_map(workspace_id: str, node_id: str) -> dict[str, str]:
    """A mirrored page's copied images: the source's media id → our attachment id."""
    cursor = mongo.doc_attachments().find({"workspace_id": workspace_id, "node_id": node_id, "media_id": {"$ne": None}}, {"media_id": 1})
    return {doc["media_id"]: doc["_id"] async for doc in cursor if doc.get("media_id")}


async def delete_for(workspace_id: str, node_ids: list[str]) -> None:
    """Remove the files of deleted pages, then their records."""
    store = get_object_store()
    cursor = mongo.doc_attachments().find({"workspace_id": workspace_id, "node_id": {"$in": node_ids}}, {"key": 1})
    async for doc in cursor:
        await store.delete(doc["key"])
    await mongo.doc_attachments().delete_many({"workspace_id": workspace_id, "node_id": {"$in": node_ids}})
