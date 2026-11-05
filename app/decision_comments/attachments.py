"""Files shared in a decision's discussion: images, PDFs, Office files and text.

1. `POST /decisions/{id}/attachments` with the file's name and size: checked against the allowed
   types and size, recorded as pending, answered with where to upload it.
2. The browser uploads it there (straight to storage; to our API in dev).
3. `POST /decision-attachments/{id}/complete`: what arrived is checked (its size, and that its
   first bytes are the kind of file its name says) and it's ready to send with a message.
4. Sending a message claims its files; downloading asks for a short-lived link each time.

A file never sent is removed by the hourly sweep a day later; deleting a message, or its
decision, removes its files at once.
"""

import unicodedata
import uuid
from datetime import timedelta
from pathlib import PurePosixPath

import structlog
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import workspace_member
from app.core.exceptions import AppError, ConflictError, ForbiddenError, NotFoundError
from app.core.rate_limit import Limit, take
from app.core.responses import api_router
from app.core.time import utcnow
from app.db import mongo
from app.decisions import service as decisions
from app.storage import keys, sniff
from app.storage.proxied import valid_signature
from app.storage.store import ProxiedStore, disposition, get_object_store

logger = structlog.get_logger("app.decision_comments")

MAX_BYTES = 25 * 1024 * 1024
#: files on one message
MAX_PER_MESSAGE = 10
#: a download link works this long: enough to start the download, too short to pass around
DOWNLOAD_TTL_SECONDS = 120
#: an upload never sent with a message is removed after this
UNSENT_FOR = timedelta(days=1)
UPLOADS = Limit("decision_attachment", 60, timedelta(hours=1))
API_PATH = "/decision-attachments"
PENDING, READY = "pending", "ready"


class FileKind(BaseModel):
    content_type: str
    #: how it's shown: image, pdf, document, spreadsheet, presentation or text
    kind: str


_IMAGE, _PDF = "image", "pdf"
#: by extension: the type always comes from here, never from the browser, which reports .md and .csv
#: differently; no SVG or HTML (they can carry scripts), executables, archives or macro files
FILE_TYPES: dict[str, FileKind] = {
    "png": FileKind(content_type="image/png", kind=_IMAGE),
    "jpg": FileKind(content_type="image/jpeg", kind=_IMAGE),
    "jpeg": FileKind(content_type="image/jpeg", kind=_IMAGE),
    "gif": FileKind(content_type="image/gif", kind=_IMAGE),
    "webp": FileKind(content_type="image/webp", kind=_IMAGE),
    "pdf": FileKind(content_type="application/pdf", kind=_PDF),
    "docx": FileKind(content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", kind="document"),
    "xlsx": FileKind(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", kind="spreadsheet"),
    "pptx": FileKind(content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation", kind="presentation"),
    "txt": FileKind(content_type="text/plain", kind="text"),
    "md": FileKind(content_type="text/markdown", kind="text"),
    "csv": FileKind(content_type="text/csv", kind="text"),
    "json": FileKind(content_type="application/json", kind="text"),
}


class NewAttachment(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


class UploadTarget(BaseModel):
    method: str
    url: str
    fields: dict[str, str]
    headers: dict[str, str]
    via_api: bool


class PendingAttachment(BaseModel):
    id: str
    upload: UploadTarget


class Attachment(BaseModel):
    """A file as messages show it."""

    id: str
    filename: str
    content_type: str
    kind: str
    size: int


class DownloadLink(BaseModel):
    url: str


router = api_router(tags=["decision attachments"])


def clean_filename(filename: str) -> str:
    """The name to show and download it as: no folders, control characters or odd Unicode forms."""
    name = PurePosixPath(unicodedata.normalize("NFC", filename).replace("\\", "/")).name
    name = "".join(char for char in name if unicodedata.category(char)[0] != "C").strip()
    return name[:255] or "file"


def extension_of(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower().removeprefix(".")


def _matches(kind: FileKind, head: bytes) -> bool:
    """Whether the file's first bytes are the kind of file its name says."""
    if kind.kind == _IMAGE:
        return sniff.image_type(head) == kind.content_type
    if kind.kind == _PDF:
        return sniff.is_pdf(head)
    if kind.kind in ("document", "spreadsheet", "presentation"):
        return sniff.is_zip(head)
    return sniff.is_text(head)


def as_attachment(record: dict) -> dict:
    return {key: record[key] for key in ("filename", "content_type", "kind", "size")} | {"id": record["_id"]}


async def _record(workspace_id: str, attachment_id: str) -> dict:
    record = await mongo.decision_attachments().find_one({"_id": attachment_id, "workspace_id": workspace_id})
    if record is None:
        raise NotFoundError("File not found", error_code="attachment_not_found")
    return record


@router.post("/decisions/{decision_id}/attachments", status_code=201)
async def start_upload(decision_id: str, body: NewAttachment, member: dict = Depends(workspace_member())) -> PendingAttachment:
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    filename = clean_filename(body.filename)
    extension = extension_of(filename)
    kind = FILE_TYPES.get(extension)
    if kind is None:
        allowed = ", ".join(sorted(FILE_TYPES))
        raise AppError(f"That kind of file can't be shared here. Allowed: {allowed}.", error_code="unsupported_file_type", status_code=415)
    if body.size > MAX_BYTES:
        raise AppError(f"Files can be at most {MAX_BYTES // (1024 * 1024)} MB", error_code="file_too_large", status_code=413)
    await decisions.found(workspace_id, decision_id)
    await take(UPLOADS, user_id)
    attachment_id = uuid.uuid4().hex
    key = keys.decision_attachment(workspace_id, decision_id, attachment_id, extension)
    await mongo.decision_attachments().insert_one({
        "_id": attachment_id, "workspace_id": workspace_id, "decision_id": decision_id, "comment_id": None,
        "key": key, "filename": filename, "content_type": kind.content_type, "kind": kind.kind, "size": body.size,
        "status": PENDING, "created_by": user_id, "created_at": utcnow(), "expires_at": utcnow() + UNSENT_FOR,
    })
    target = get_object_store().upload_target(attachment_id, key, kind.content_type, body.size, api_path=API_PATH)
    return {"id": attachment_id, "upload": target}


@router.put(f"{API_PATH}/{{attachment_id}}/content")
async def upload_content(attachment_id: str, request: Request, member: dict = Depends(workspace_member())) -> None:
    """The file itself, when files go through our API (dev); otherwise it goes straight to storage."""
    store = get_object_store()
    if not isinstance(store, ProxiedStore):
        raise NotFoundError("Uploads go straight to storage", error_code="not_found")
    record = await _record(member["workspace_id"], attachment_id)
    if record["created_by"] != member["user_id"] or record["status"] != PENDING:
        raise ConflictError("This upload can't take a file", error_code="upload_finished")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > record["size"]:
            raise AppError("The file is bigger than it said", error_code="file_too_large", status_code=413)
    await store.write(record["key"], bytes(data), record["content_type"])


@router.post(f"{API_PATH}/{{attachment_id}}/complete")
async def complete_upload(attachment_id: str, member: dict = Depends(workspace_member())) -> Attachment:
    """Checks what arrived; a file that isn't what its name says is deleted and refused."""
    record = await _record(member["workspace_id"], attachment_id)
    if record["created_by"] != member["user_id"]:
        raise ForbiddenError("Only its uploader can finish an upload", error_code="not_uploader")
    if record["status"] == READY:
        return as_attachment(record)
    store = get_object_store()
    stored = await store.head(record["key"])
    if not stored:
        raise ConflictError("The file hasn't arrived yet", error_code="upload_missing")
    head = await store.read_start(record["key"], sniff.HEAD_BYTES) or b""
    if stored["size"] != record["size"] or not _matches(FILE_TYPES[extension_of(record["filename"])], head):
        await store.delete(record["key"])
        await mongo.decision_attachments().delete_one({"_id": attachment_id})
        raise AppError("That file isn't the kind of file its name says", error_code="unsupported_file_type", status_code=415)
    await mongo.decision_attachments().update_one({"_id": attachment_id}, {"$set": {"status": READY}})
    return as_attachment({**record, "status": READY})


@router.get(f"{API_PATH}/{{attachment_id}}/download")
async def download(attachment_id: str, member: dict = Depends(workspace_member())) -> DownloadLink:
    """A link that downloads the file under its name, for a couple of minutes."""
    record = await _record(member["workspace_id"], attachment_id)
    # a sent file is the workspace's; an unsent one only its uploader's
    if record["status"] != READY or (record["comment_id"] is None and record["created_by"] != member["user_id"]):
        raise NotFoundError("File not found", error_code="attachment_not_found")
    url = get_object_store().download_url(
        attachment_id, record["key"], DOWNLOAD_TTL_SECONDS, download_name=record["filename"], api_path=API_PATH,
    )
    return {"url": url}


@router.get(f"{API_PATH}/{{attachment_id}}/file", response_model=None)
async def read_file(attachment_id: str, expires: int = Query(...), signature: str = Query(...)) -> Response:
    """A download link's file, when files go through our API (dev)."""
    store = get_object_store()
    if not isinstance(store, ProxiedStore) or not valid_signature(attachment_id, expires, signature):
        raise NotFoundError("File not found", error_code="not_found")
    record = await mongo.decision_attachments().find_one({"_id": attachment_id, "status": READY})
    found = record and await store.read(record["key"])
    if not found:
        raise NotFoundError("File not found", error_code="not_found")
    return Response(found[0], media_type=record["content_type"], headers={
        "Content-Disposition": disposition(record["filename"]),
        "X-Content-Type-Options": "nosniff",
        # never run anything the file holds, should a browser open it anyway
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "private, no-store",
    })


async def claim(member: dict, decision_id: str, comment_id: str, attachment_ids: list[str]) -> list[dict]:
    """Attach the member's ready, unsent uploads on this decision to a message; all of them or none."""
    if not attachment_ids:
        return []
    query = {
        "_id": {"$in": attachment_ids}, "workspace_id": member["workspace_id"], "decision_id": decision_id,
        "created_by": member["user_id"], "status": READY, "comment_id": None,
    }
    claimed = await mongo.decision_attachments().update_many(query, {"$set": {"comment_id": comment_id, "expires_at": None}})
    if claimed.modified_count != len(set(attachment_ids)):
        await release(comment_id)
        raise AppError("A file isn't ready to send, or was already sent", error_code="attachment_unavailable", status_code=422)
    records = await mongo.decision_attachments().find({"comment_id": comment_id}).to_list(None)
    order = {attachment_id: index for index, attachment_id in enumerate(attachment_ids)}
    return [as_attachment(record) for record in sorted(records, key=lambda record: order[record["_id"]])]


async def release(comment_id: str) -> None:
    """Undo a claim: the files are unsent again (a send that didn't go through)."""
    await mongo.decision_attachments().update_many(
        {"comment_id": comment_id}, {"$set": {"comment_id": None, "expires_at": utcnow() + UNSENT_FOR}},
    )


async def remove(query: dict) -> int:
    """Delete the matching files from storage, then their records; one that fails is retried by the sweep."""
    store, removed = get_object_store(), 0
    async for record in mongo.decision_attachments().find(query, {"key": 1}):
        try:
            await store.delete(record["key"])
        except Exception:
            logger.exception("decision_attachment_delete_failed", attachment_id=record["_id"])
            await mongo.decision_attachments().update_one({"_id": record["_id"]}, {"$set": {"expires_at": utcnow()}})
            continue
        await mongo.decision_attachments().delete_one({"_id": record["_id"]})
        removed += 1
    return removed


async def sweep() -> int:
    """Remove uploads never sent with a message, and files whose removal failed before."""
    return await remove({"expires_at": {"$lte": utcnow()}})
