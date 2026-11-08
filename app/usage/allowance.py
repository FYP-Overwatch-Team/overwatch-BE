"""A workspace's monthly AI allowance: one action per chat question, suggestion run or source a check reads.

The counter in `usage_counters` enforces it; each action taken is also kept in `ai_actions`
so the usage page can chart them by day and member.
"""

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.constants import UsageFeature
from app.core.exceptions import AppError
from app.core.rate_limit import Window, take_all
from app.core.time import utcnow
from app.db import mongo


def next_month(now: datetime) -> datetime:
    """Midnight UTC on the 1st of the month after `now`'s."""
    return datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=UTC)


def monthly_key(workspace_id: str, now: datetime) -> str:
    return f"ai:month:{workspace_id}:{now.strftime('%Y%m')}"


async def take_action(workspace_id: str, user_id: str, feature: UsageFeature, windows: list[Window]) -> None:
    """Count one action against the month and every other window, or raise without counting any."""
    now = utcnow()
    month = Window(
        monthly_key(workspace_id, now), get_settings().ai_workspace_monthly_limit, next_month(now) + timedelta(days=1),
        AppError("This workspace has used this month's AI allowance", error_code="ai_allowance_used_up", status_code=429),
    )
    # the month first: once it's used up, that's the limit worth telling people about
    await take_all([month, *windows])
    await mongo.ai_actions().insert_one({
        "workspace_id": workspace_id, "user_id": user_id, "feature": feature, "created_at": now,
    })
