from datetime import date, datetime

from pydantic import BaseModel

from app.core.constants import UsageLimitKind


class UsageTotals(BaseModel):
    #: what was taken from the monthly allowance: chat questions, suggestion runs, sources checked
    actions: int
    #: model calls, embeddings included; one action may make several
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    #: what the provider charged, summed over the calls it reported a cost for
    cost_usd: float


class DailyUsage(UsageTotals):
    #: in the time zone the summary was asked for
    day: date


class MemberUsage(UsageTotals):
    user_id: str


class UsageSummary(BaseModel):
    """A workspace's AI usage over the last `days` (today included), in total and split by day and member."""

    days: int
    #: the time zone the days are counted in
    time_zone: str
    total: UsageTotals
    by_day: list[DailyUsage]
    by_member: list[MemberUsage]


class UsageLimit(BaseModel):
    """How much of one limit is used, in the limit's own unit (questions, runs, sources)."""

    kind: UsageLimitKind
    used: int
    limit: int
    resets_at: datetime


class UsageLimits(BaseModel):
    """The workspace's limits as they stand now; the member's own hourly one included."""

    limits: list[UsageLimit]
