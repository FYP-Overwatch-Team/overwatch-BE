"""How often a workspace may ask the AI to suggest decisions; each run also counts against the AI's daily budget."""

from datetime import timedelta

from app.chat.limits import daily_budget
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.rate_limit import Window, take_all
from app.core.time import utcnow


async def take_suggestion(workspace_id: str) -> None:
    now = utcnow()
    await take_all([
        Window(
            f"decisions:suggest:{workspace_id}:{now.strftime('%Y%m%d')}",
            get_settings().decision_suggest_workspace_daily_limit, now + timedelta(days=2),
            AppError("This workspace has used today's decision suggestions", error_code="rate_limited", status_code=429),
        ),
        daily_budget(now),
    ])
