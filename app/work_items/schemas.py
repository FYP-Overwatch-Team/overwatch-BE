from datetime import date, datetime

from typing import Annotated, Literal

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
    #: the tracker's status id; null for native items
    status_id: str | None = None
    status_category: StatusCategory
    #: the board column it's in; null for a tracker status no column holds
    column_id: str | None = None
    #: its open sprint; null is the backlog
    sprint_id: str | None = None
    rank: float
    assignee: WorkItemAssignee | None = None
    priority: str | None = None
    #: the workspace's work item type (Task, Bug, ...); null for a native item from before types
    type_id: str | None = None
    issue_type: str | None = None
    issue_type_id: str | None = None
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
    # the workspace's Task when omitted
    type_id: str | None = None


class UpdateWorkItem(BaseModel):
    """Only the fields sent are changed; send `assignee_id: null` to unassign."""

    title: str | None = Field(default=None, **TITLE)
    description: str | None = Field(default=None, **DESCRIPTION)
    assignee_id: str | None = None
    priority: str | None = Field(default=None, max_length=60)
    labels: list[Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]] | None = Field(default=None, max_length=50)
    due_date: date | None = None
    story_points: float | None = Field(default=None, ge=0, le=1000)
    type_id: str | None = None

    @model_validator(mode="after")
    def _title_not_null(self):
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title can't be empty")
        if "type_id" in self.model_fields_set and self.type_id is None:
            raise ValueError("an item always has a type")
        return self


class MoveWorkItem(BaseModel):
    column_id: str
    # which of the column's statuses, when it holds several the item can reach
    status_id: str | None = None
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


class AllowedTypes(BaseModel):
    #: the types this item can be changed to; null when it can take any of the workspace's
    type_ids: list[str] | None


class MoveTarget(BaseModel):
    column_id: str
    #: the tracker status it lands in; null on native boards
    status_id: str | None = None
    status: str | None = None
    #: the tracker's name for the move, when it has one
    transition_name: str | None = None


class AllowedMoves(BaseModel):
    """Where an item can move: `open` boards allow any column; `live` is the tracker's answer for this item now."""

    basis: Literal["open", "board", "live"]
    targets: list[MoveTarget]
