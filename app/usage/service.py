"""AI usage, one record per model call, kept per workspace so each one's usage can be shown and billed."""

from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Hashable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.chat import limits as chat_limits
from app.core.config import get_settings
from app.core.constants import UsageFeature, UsageLimitKind
from app.core.rate_limit import count
from app.core.time import utcnow
from app.db import mongo
from app.decisions import limits as decision_limits
from app.integrations.llm import CallUsage
from app.usage.schemas import (
    DailyUsage,
    FeatureUsage,
    MemberUsage,
    UsageLimit,
    UsageLimits,
    UsageSummary,
    UsageTotals,
)


def recorder(workspace_id: str, user_id: str, feature: UsageFeature) -> Callable[[CallUsage], Awaitable[None]]:
    """What to pass as a model call's `on_usage`: records the call against this member and feature."""

    async def record(usage: CallUsage) -> None:
        await mongo.llm_usage().insert_one({
            "workspace_id": workspace_id,
            "user_id": user_id,
            "feature": feature,
            "model": usage.model,
            "provider": usage.provider,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "cached_tokens": usage.cached_tokens,
            "cost_usd": usage.cost_usd,
            "created_at": utcnow(),
        })

    return record


def _totals(rows: list[dict]) -> UsageTotals:
    return UsageTotals(
        calls=len(rows),
        prompt_tokens=sum(row["prompt_tokens"] for row in rows),
        completion_tokens=sum(row["completion_tokens"] for row in rows),
        cached_tokens=sum(row["cached_tokens"] for row in rows),
        cost_usd=round(sum(row["cost_usd"] or 0 for row in rows), 6),
    )


def _grouped(rows: list[dict], key: Callable[[dict], Hashable]) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def _day(rows: list[dict], day) -> DailyUsage:
    return DailyUsage(
        day=day,
        calls_by_feature=Counter(row["feature"] for row in rows),
        calls_by_member=Counter(row["user_id"] for row in rows),
        **_totals(rows).model_dump(),
    )


async def summary(workspace_id: str, days: int, tz: ZoneInfo) -> UsageSummary:
    """Usage over the last `days` days in `tz`, today included, so a chart of them has one bar per day."""
    today = utcnow().astimezone(tz).date()
    since = datetime.combine(today - timedelta(days=days - 1), time(), tz)
    rows = await mongo.llm_usage().find({"workspace_id": workspace_id, "created_at": {"$gte": since}}).to_list(None)
    by_day = _grouped(rows, lambda row: row["created_at"].astimezone(tz).date())
    by_feature = _grouped(rows, lambda row: row["feature"])
    by_member = _grouped(rows, lambda row: row["user_id"])
    return UsageSummary(
        days=days,
        time_zone=tz.key,
        total=_totals(rows),
        by_day=[_day(by_day[day], day) for day in sorted(by_day, reverse=True)],
        by_feature=[FeatureUsage(feature=feature, **_totals(group).model_dump()) for feature, group in by_feature.items()],
        by_member=[MemberUsage(user_id=user_id, **_totals(group).model_dump()) for user_id, group in by_member.items()],
    )


async def limits(workspace_id: str, user_id: str) -> UsageLimits:
    """Each limit's count as it stands, read from the same counters the limits take from (UTC hours and days)."""
    settings = get_settings()
    now = utcnow()
    next_day = datetime.combine(now.date() + timedelta(days=1), time(), now.tzinfo)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    counters = [
        (UsageLimitKind.CHAT_QUESTIONS, chat_limits.workspace_daily_key(workspace_id, now),
         settings.chat_workspace_daily_limit, next_day),
        (UsageLimitKind.MEMBER_CHAT_QUESTIONS, chat_limits.member_hourly_key(workspace_id, user_id, now),
         settings.chat_user_hourly_limit, next_hour),
        (UsageLimitKind.DECISION_SUGGESTIONS, decision_limits.suggestion_key(workspace_id, now),
         settings.decision_suggest_workspace_daily_limit, next_day),
        (UsageLimitKind.DECISION_CHECKS, decision_limits.check_key(workspace_id, now),
         settings.decision_check_workspace_daily_limit, next_day),
    ]
    return UsageLimits(limits=[
        UsageLimit(kind=kind, used=await count(key), limit=limit, resets_at=resets_at)
        for kind, key, limit, resets_at in counters
    ])
