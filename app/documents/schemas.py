import json
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app.core.constants import DocPushState, DocumentKind

TITLE = {"max_length": 255}
#: a page's editor document, serialised; Mongo's own cap is 16 MB
MAX_CONTENT_BYTES = 1_000_000
MAX_CONTENT_MD = 1_000_000


def _content_size(content: dict) -> int:
    return len(json.dumps(content, separators=(",", ":")))


class Person(BaseModel):
    id: str
    name: str | None
    avatar_url: str | None = None


class Crumb(BaseModel):
    id: str
    title: str


class DocumentSummary(BaseModel):
    """A tree row: everything but the content."""

    id: str
    kind: DocumentKind
    title: str
    parent_id: str | None
    rank: float
    has_children: bool
    source: str  # NATIVE_SOURCE or the docs provider it's mirrored from
    # false for a mirrored doc whose provider can't take writes
    editable: bool
    # whether pages can be created inside it (mirrored ones are created at their source)
    can_add_pages: bool
    external_url: str | None = None
    updated_at: datetime


class ConflictCopy(BaseModel):
    """What was saved here while the source changed too; kept until the user resolves it."""

    title: str
    content: dict
    content_md: str | None = None


class Document(DocumentSummary):
    #: the editor's document (ProseMirror JSON); null for folders
    content: dict | None = None
    #: the same content as Markdown, for search, AI answers and export
    content_md: str | None = None
    #: bumped on every content save; send it back as `base_version`
    version: int
    breadcrumbs: list[Crumb]
    #: who created it, as they were named then
    author: Person | None = None
    #: signed links to the page's uploaded images, by attachment id; they expire, so reload for fresh ones
    attachment_urls: dict[str, str] = {}
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    #: a mirrored page's way back to its source; null for pages written here
    push_state: DocPushState | None = None
    #: why the last push didn't go through; it's retried
    push_error: str | None = None
    #: when the source's content was last read or written
    synced_at: datetime | None = None
    conflict: ConflictCopy | None = None


class CreateDocument(BaseModel):
    kind: DocumentKind
    title: str = Field(default="", **TITLE)
    # the tree's root when omitted
    parent_id: str | None = None
    # the siblings it goes between; the end of the list when neither is sent
    before_id: str | None = None
    after_id: str | None = None
    content: dict | None = None
    content_md: str | None = Field(default=None, max_length=MAX_CONTENT_MD)

    @model_validator(mode="after")
    def _content_fits(self):
        if self.content is not None:
            if self.kind == DocumentKind.FOLDER:
                raise ValueError("folders have no content")
            if _content_size(self.content) > MAX_CONTENT_BYTES:
                raise ValueError("content is too large")
        return self


class UpdateDocument(BaseModel):
    """Only the fields sent are changed. Content saves need the version they were edited from."""

    title: str | None = Field(default=None, **TITLE)
    content: dict | None = None
    content_md: str | None = Field(default=None, max_length=MAX_CONTENT_MD)
    base_version: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _content_needs_version(self):
        if self.content is not None:
            if self.base_version is None:
                raise ValueError("base_version is required with content")
            if _content_size(self.content) > MAX_CONTENT_BYTES:
                raise ValueError("content is too large")
        return self


class ArchiveChildren(StrEnum):
    """What happens to what's inside a node that's archived."""

    ARCHIVE = "archive"
    MOVE = "move"


class ArchiveDocument(BaseModel):
    children: ArchiveChildren = ArchiveChildren.ARCHIVE
    # with `move`: the folder (or page) they go to; the top level when null
    move_to: str | None = None


class ArchivedDocument(BaseModel):
    """An Archive entry: a node archived on its own, with whatever was archived along with it."""

    id: str
    kind: DocumentKind
    title: str
    #: how many nodes inside it were archived with it
    archived_with: int
    archived_at: datetime
    archived_by: str | None
    source: str


class ConflictAction(StrEnum):
    #: keep the source's version; the copy saved here is dropped (save it as a page first to keep it)
    KEEP_SOURCE = "keep_source"
    #: send the copy saved here over the source's version
    OVERWRITE = "overwrite"


class ResolveConflict(BaseModel):
    action: ConflictAction
    base_version: int = Field(ge=0)
