from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.core.constants import (
    CheckMatch,
    DecisionCheckInterval,
    DecisionOrigin,
    DecisionStatus,
    EvidenceKind,
    EvidenceStance,
    EvidenceState,
    InboxTab,
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


class EvidenceMatch(BaseModel):
    """Why an automatic check read the source it added this evidence from."""

    reason: CheckMatch
    #: what it matched on: the key or page it names, or how close in meaning it read (0..1)
    detail: str | None = None


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
    #: set on evidence an automatic check added: why it read the source
    match: EvidenceMatch | None = None
    #: when the tracker or docs source it's mirrored from was last read, so `state` is as of then; null when written here
    source_synced_at: datetime | None = None


class HistoryEntry(BaseModel):
    status: DecisionStatus
    by: str
    at: datetime
    note: str | None = None


class DecisionSource(BaseModel):
    """An item or page a decision rests on, numbered so its background can cite it as [S1]."""

    id: str
    kind: EvidenceKind | None
    ref_id: str | None
    #: as it was when first cited, kept in case the source goes away
    title: str


class CodeContext(BaseModel):
    """Where in the code an AI tool was working when it recorded a decision, as the tool reported it."""

    repo: str | None = Field(default=None, max_length=200)
    branch: str | None = Field(default=None, max_length=200)
    commit: str | None = Field(default=None, max_length=64)
    files: list[Annotated[str, Field(max_length=300)]] = Field(default=[], max_length=20)
    pr_url: str | None = Field(default=None, max_length=500)


class Capture(BaseModel):
    """How a decision recorded by an AI tool came about. The tool's name is the connection's; the rest is what it reported."""

    #: the connected tool it came through, as its owner named it
    client: str
    #: the person asked for it to be recorded, or agreed when the tool asked
    user_approved: bool
    #: what the person said, as the tool reported it; not checked against anything
    user_words: str | None
    #: the model the tool said it runs on
    agent_model: str | None
    #: a ticket key the work was for
    ticket: str | None
    code: CodeContext | None
    recorded_at: datetime


class Decision(BaseModel):
    id: str
    #: its short id in the workspace ("D-31"); None until an older decision is numbered
    key: str | None = None
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
    #: the items and pages it rests on (its evidence's, and any its background cites), S1 first
    sources: list[DecisionSource] = []
    #: every status it has had, oldest first
    history: list[HistoryEntry]
    #: accepted, but its evidence broke or was contradicted since it was settled: it's in Needs you
    needs_attention: bool = False
    #: set on a decision an AI tool recorded
    capture: Capture | None = None
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


class OverviewPage(BaseModel):
    #: the docs page the product overview was written to
    page_id: str


class InboxCounts(BaseModel):
    needs_you: int
    open: int
    decided: int
