from datetime import date

from pydantic import BaseModel

from app.core.constants import UsageFeature


class UsageTotals(BaseModel):
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    #: what the provider charged, summed over the calls it reported a cost for
    cost_usd: float


class DailyUsage(UsageTotals):
    day: date


class FeatureUsage(UsageTotals):
    feature: UsageFeature


class MemberUsage(UsageTotals):
    user_id: str


class UsageSummary(BaseModel):
    """A workspace's AI usage over the last `days`, in total and split by day, feature and member."""

    days: int
    total: UsageTotals
    by_day: list[DailyUsage]
    by_feature: list[FeatureUsage]
    by_member: list[MemberUsage]
