"""Request limits for the AI chat: per member per hour, per workspace and overall per day.

A question counts once against every limit. Each further model call it makes while
looking things up counts against the overall daily budget only, which is what the
provider bills by.

Fixed windows in `usage_counters`. A take is one atomic update that only matches while
the window is under its limit; once full, the upsert collides with the existing counter.
"""

from datetime import datetime, timedelta

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.rate_limit import Window, take_all, take_one
from app.core.time import utcnow


def member_hourly_key(workspace_id: str, user_id: str, now: datetime) -> str:
    return f"chat:user:{workspace_id}:{user_id}:{now.strftime('%Y%m%d%H')}"


def workspace_daily_key(workspace_id: str, now: datetime) -> str:
    return f"chat:workspace:{workspace_id}:{now.strftime('%Y%m%d')}"


def daily_budget(now: datetime) -> Window:
    """Every model call in a day, whatever it's for: what the provider bills by."""
    return Window(
        f"chat:global:{now.strftime('%Y%m%d')}", get_settings().chat_daily_budget, now + timedelta(days=2),
        AppError("The AI quota is used up for today", error_code="llm_quota_exhausted", status_code=503),
    )


async def take(workspace_id: str, user_id: str) -> None:
    """Count one question against every limit, or raise without counting any."""
    settings = get_settings()
    now = utcnow()
    await take_all([
        Window(member_hourly_key(workspace_id, user_id, now), settings.chat_user_hourly_limit, now + timedelta(hours=2),
               AppError("You've asked a lot of questions this hour; try again later", error_code="rate_limited", status_code=429)),
        Window(workspace_daily_key(workspace_id, now), settings.chat_workspace_daily_limit, now + timedelta(days=2),
               AppError("This workspace has reached today's question limit", error_code="rate_limited", status_code=429)),
        daily_budget(now),
    ])


async def take_model_call() -> bool:
    """Count one more model call for a question already counted, against the daily budget only.

    False when the budget is spent; the answer is then written from what was found so far.
    """
    budget = daily_budget(utcnow())
    return await take_one(budget.key, budget.limit, budget.expires_at)
