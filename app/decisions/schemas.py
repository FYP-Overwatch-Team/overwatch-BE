from datetime import date, datetime

from pydantic import BaseModel, Field

from app.core.constants import (
    DecisionCheckInterval,
    DecisionOrigin,
    DecisionStatus,
    EvidenceKind,
    EvidenceStance,
    EvidenceState,
)

TITLE = {"min_length": 1, "max_length": 200}
STATEMENT = {"min_length": 1, "max_length": 2000}
CONTEXT = {"max_length": 20_000}
AREA = {"min_length": 1, "max_length": 60}
QUOTE = {"min_length": 1, "max_length": 2000}
NOTE = {"max_length": 500}
MAX_EVIDENCE = 20


class NewEvidence(BaseModel):
    """A passage from a board item or docs page; it must appear in that source as written."""

    kind: EvidenceKind
    ref_id: str
    quote: str = Field(**QUOTE)
    stance: EvidenceStance = EvidenceStance.SUPPORTS
    #: why it supports or contradicts the decision, e.g. what an automatic check found
    note: str | None = Field(default=None, **NOTE)


class Evidence(NewEvidence):
    id: str
    #: the source's title and link when it was quoted, kept in case the source goes away
    title: str
    external_url: str | None
    #: a board item's board and key, to open it there; null for pages
    board_id: str | None = None
    item_key: str | None = None
    added_by: str
    added_at: datetime
    state: EvidenceState


class HistoryEntry(BaseModel):
    status: DecisionStatus
    by: str
    at: datetime
    note: str | None = None


class Decision(BaseModel):
    id: str
    title: str
    statement: str
    context_md: str | None
    #: the part of the product it's about ("Billing", "Auth"), to group decisions by
    area: str | None
    status: DecisionStatus
    origin: DecisionOrigin
    owner_id: str | None
    #: when an open decision should be looked at again
    review_by: date | None
    #: the decision that replaced this one
    superseded_by: str | None
    evidence: list[Evidence]
    #: every status it has had, oldest first
    history: list[HistoryEntry]
    created_by: str
    created_at: datetime
    updated_at: datetime


class CreateDecision(BaseModel):
    title: str = Field(**TITLE)
    statement: str = Field(**STATEMENT)
    context_md: str | None = Field(default=None, **CONTEXT)
    area: str | None = Field(default=None, **AREA)
    status: DecisionStatus = DecisionStatus.OPEN
    owner_id: str | None = None
    review_by: date | None = None
    evidence: list[NewEvidence] = Field(default=[], max_length=MAX_EVIDENCE)


class UpdateDecision(BaseModel):
    """Only the fields sent are changed; send null to clear one."""

    title: str | None = Field(default=None, **TITLE)
    statement: str | None = Field(default=None, **STATEMENT)
    context_md: str | None = Field(default=None, **CONTEXT)
    area: str | None = Field(default=None, **AREA)
    owner_id: str | None = None
    review_by: date | None = None


class ChangeStatus(BaseModel):
    status: DecisionStatus
    note: str | None = Field(default=None, **NOTE)
    #: required with `superseded`: the decision that replaces this one
    superseded_by: str | None = None


class SuggestRequest(BaseModel):
    kind: EvidenceKind
    ref_id: str


class CheckResult(BaseModel):
    #: changed items and pages read
    checked: int
    #: contradicting passages added to decisions
    conflicts: int
    #: the day's checks ran out before everything changed was read
    capped: bool


class CheckSettings(BaseModel):
    #: whether accepted decisions are checked against what changed; on by default
    enabled: bool
    #: how often, in minutes
    interval_minutes: DecisionCheckInterval
    last_run_at: datetime | None
    last_result: CheckResult | None


class ChangeCheckSettings(BaseModel):
    """Only the fields sent are changed."""

    enabled: bool | None = None
    interval_minutes: DecisionCheckInterval | None = None
