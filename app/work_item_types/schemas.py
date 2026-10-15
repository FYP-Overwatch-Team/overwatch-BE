from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import ColumnColor, WorkItemIcon

NAME = {"min_length": 1, "max_length": 40}


class WorkItemType(BaseModel):
    id: str
    name: str
    icon: WorkItemIcon
    color: ColumnColor
    #: NATIVE_SOURCE for the workspace's own; a tracker's provider for its types, whose names it owns
    source: str
    created_at: datetime


class CreateWorkItemType(BaseModel):
    name: str = Field(**NAME)
    icon: WorkItemIcon
    color: ColumnColor


class UpdateWorkItemType(BaseModel):
    """Only the fields sent are changed; a tracker's type keeps its name."""

    name: str | None = Field(default=None, **NAME)
    icon: WorkItemIcon | None = None
    color: ColumnColor | None = None
