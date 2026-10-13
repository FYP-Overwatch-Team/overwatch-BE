from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import SprintState

NAME = {"min_length": 1, "max_length": 120}
GOAL = {"max_length": 2_000}


class Sprint(BaseModel):
    id: str
    board_id: str
    name: str
    state: SprintState
    goal: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    completed_at: datetime | None = None


class CreateSprint(BaseModel):
    #: "<board> Sprint N" when omitted
    name: str | None = Field(default=None, **NAME)
    goal: str | None = Field(default=None, **GOAL)
    start_at: datetime | None = None
    end_at: datetime | None = None


class UpdateSprint(BaseModel):
    """Only the fields sent are changed."""

    name: str = Field(default=None, **NAME)
    goal: str | None = Field(default=None, **GOAL)
    start_at: datetime | None = None
    end_at: datetime | None = None


class StartSprint(BaseModel):
    #: now, and two weeks from the start, when omitted (and not planned on the sprint already)
    start_at: datetime | None = None
    end_at: datetime | None = None


class CompleteSprint(BaseModel):
    #: an open sprint of the same board for the unfinished items; the backlog when omitted
    move_open_to: str | None = None
