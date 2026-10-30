from datetime import date, datetime

from pydantic import BaseModel

from app.core.constants import UsageFeature, UsageLimitKind


class UsageTotals(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    #: what the provider charged, summed over the calls it reported a cost for
    cost_usd: float


class DailyUsage(UsageTotals):
    #: in the time zone the summary was asked for
    day: date
    #: the day's calls per feature and per member, for charting either way
    calls_by_feature: dict[UsageFeature, int]
    calls_by_member: dict[str, int]


class FeatureUsage(UsageTotals):
    feature: UsageFeature


class MemberUsage(UsageTotals):
    user_id: str


class UsageSummary(BaseModel):
    """A workspace's AI usage over the last `days` (today included), in total and split by day, feature and member."""

    days: int
    #: the time zone the days are counted in
    time_zone: str
    total: UsageTotals
    by_day: list[DailyUsage]
    by_feature: list[FeatureUsage]
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
