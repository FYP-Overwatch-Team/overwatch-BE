from datetime import date, datetime

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from app.core.constants import StatusCategory

TITLE = {"min_length": 1, "max_length": 255}
DESCRIPTION = {"max_length": 20_000}


class WorkItemAssignee(BaseModel):
    id: str | None
    name: str | None


class LinkedItem(BaseModel):
    key: str | None
    title: str | None = None
    status: str | None = None
    status_category: StatusCategory | None = None
    #: how it relates, from this item's side ("blocks", "is blocked by"); subtasks have none
    type: str | None = None


class ParentItem(BaseModel):
    key: str | None
    title: str | None = None


class WorkItem(BaseModel):
    id: str
    source: str  # NATIVE_SOURCE or the tracker's provider
    key: str
    title: str
    description: str | None = None
    #: the tracker's rich text as Markdown, for display; edits still go through `description`
    description_md: str | None = None
    status: str | None = None
    status_category: StatusCategory
    #: the board column it's in; null for a tracker status no column holds
    column_id: str | None = None
    #: its open sprint; null is the backlog
    sprint_id: str | None = None
    rank: float
    assignee: WorkItemAssignee | None = None
    priority: str | None = None
    issue_type: str | None = None
    reporter: WorkItemAssignee | None = None
    labels: list[str] = []
    parent: ParentItem | None = None
    due_date: date | None = None
    story_points: float | None = None
    subtasks: list[LinkedItem] = []
    links: list[LinkedItem] = []
    target_key: str | None = None
    external_url: str | None = None
    # false when the tracker grant predates write-back; reconnect to edit
    editable: bool
    created_at: datetime
    updated_at: datetime


class CreateWorkItem(BaseModel):
    title: str = Field(**TITLE)
    description: str | None = Field(default=None, **DESCRIPTION)
    # a workspace member for native items, the tracker's account id otherwise
    assignee_id: str | None = None
    # the native board, or a tracker target's board to create it in that tracker
    board_id: str
    # the board's first column when omitted
    column_id: str | None = None
    # an open sprint of the board; the backlog when omitted
    sprint_id: str | None = None


class UpdateWorkItem(BaseModel):
    """Only the fields sent are changed; send `assignee_id: null` to unassign."""

    title: str | None = Field(default=None, **TITLE)
    description: str | None = Field(default=None, **DESCRIPTION)
    assignee_id: str | None = None
    priority: str | None = Field(default=None, max_length=60)
    labels: list[Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]] | None = Field(default=None, max_length=50)
    due_date: date | None = None
    story_points: float | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def _title_not_null(self):
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title can't be empty")
        return self


class MoveWorkItem(BaseModel):
    column_id: str
    # the cards it lands between; both None for an empty column
    before_id: str | None = None
    after_id: str | None = None


class SetSprint(BaseModel):
    #: an open sprint of the item's board; null moves it to the backlog
    sprint_id: str | None
    # where it lands in the sprint's (or backlog's) order
    before_id: str | None = None
    after_id: str | None = None


class Lane(BaseModel):
    #: an assignee id or priority name; "none" for items without one
    key: str
    label: str | None
    total: int
