"""Request limits for the AI chat: per member per hour, per workspace and overall per day.

Fixed windows in `usage_counters`. A take is one atomic update that only matches while
the window is under its limit; once full, the upsert collides with the existing counter.
"""

from datetime import timedelta

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.rate_limit import take_one
from app.core.time import utcnow
from app.db import mongo


async def take(workspace_id: str, user_id: str) -> None:
    """Count one question against every limit, or raise without counting any."""
    settings = get_settings()
    now = utcnow()
    hour, day = now.strftime("%Y%m%d%H"), now.strftime("%Y%m%d")
    windows = [
        (f"chat:user:{workspace_id}:{user_id}:{hour}", settings.chat_user_hourly_limit, timedelta(hours=2),
         AppError("You've asked a lot of questions this hour; try again later", error_code="rate_limited", status_code=429)),
        (f"chat:workspace:{workspace_id}:{day}", settings.chat_workspace_daily_limit, timedelta(days=2),
         AppError("This workspace has reached today's question limit", error_code="rate_limited", status_code=429)),
        (f"chat:global:{day}", settings.chat_daily_budget, timedelta(days=2),
         AppError("The AI quota is used up for today", error_code="llm_quota_exhausted", status_code=503)),
    ]

    taken: list[str] = []
    for key, limit, lifetime, error in windows:
        if not await take_one(key, limit, now + lifetime):
            if taken:
                await mongo.usage_counters().update_many({"_id": {"$in": taken}}, {"$inc": {"count": -1}})
            raise error
        taken.append(key)

