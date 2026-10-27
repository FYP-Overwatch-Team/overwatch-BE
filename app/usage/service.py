"""AI usage, one record per model call, kept per workspace so each one's usage can be shown and billed."""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Hashable
from datetime import timedelta

from app.core.constants import UsageFeature
from app.core.time import utcnow
from app.db import mongo
from app.integrations.llm import CallUsage
from app.usage.schemas import DailyUsage, FeatureUsage, MemberUsage, UsageSummary, UsageTotals


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


async def summary(workspace_id: str, days: int) -> UsageSummary:
    since = utcnow() - timedelta(days=days)
    rows = await mongo.llm_usage().find({"workspace_id": workspace_id, "created_at": {"$gte": since}}).to_list(None)
    by_day = _grouped(rows, lambda row: row["created_at"].date())
    by_feature = _grouped(rows, lambda row: row["feature"])
    by_member = _grouped(rows, lambda row: row["user_id"])
    return UsageSummary(
        days=days,
        total=_totals(rows),
        by_day=[DailyUsage(day=day, **_totals(by_day[day]).model_dump()) for day in sorted(by_day, reverse=True)],
        by_feature=[FeatureUsage(feature=feature, **_totals(group).model_dump()) for feature, group in by_feature.items()],
        by_member=[MemberUsage(user_id=user_id, **_totals(group).model_dump()) for user_id, group in by_member.items()],
    )
