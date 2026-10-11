from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.core.constants import StatusCategory

TITLE = {"min_length": 1, "max_length": 255}
DESCRIPTION = {"max_length": 20_000}


class WorkItemAssignee(BaseModel):
    id: str | None
    name: str | None


class WorkItem(BaseModel):
    id: str
    source: str  # NATIVE_SOURCE or the tracker's provider
    key: str
    title: str
    description: str | None = None
    status: str | None = None
    status_category: StatusCategory
    rank: float
    assignee: WorkItemAssignee | None = None
    priority: str | None = None
    issue_type: str | None = None
    target_key: str | None = None
    external_url: str | None = None
    # false when the tracker grant predates write-back; reconnect to edit
    editable: bool
    created_at: datetime
    updated_at: datetime


class CreateWorkItem(BaseModel):
    title: str = Field(**TITLE)
    description: str | None = Field(default=None, **DESCRIPTION)
    status_category: StatusCategory = StatusCategory.TODO
    # a workspace member for native items, the tracker's account id otherwise
    assignee_id: str | None = None
    # create in a connected tracker target instead of natively
    provider: str | None = None
    target_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def _destination(self):
        if (self.provider is None) != (self.target_key is None):
            raise ValueError("provider and target_key go together")
        return self


class UpdateWorkItem(BaseModel):
    """Only the fields sent are changed; send `assignee_id: null` to unassign."""

    title: str | None = Field(default=None, **TITLE)
    description: str | None = Field(default=None, **DESCRIPTION)
    assignee_id: str | None = None

    @model_validator(mode="after")
    def _title_not_null(self):
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title can't be empty")
        return self


class MoveWorkItem(BaseModel):
    status_category: StatusCategory
    # the cards it lands between; both None for an empty column
    before_id: str | None = None
    after_id: str | None = None
