"""Fixed-window request limits counted in `usage_counters`, and resend cooldowns.

A take is one atomic update that only matches while the window is under its limit;
once full, the upsert collides with the existing counter.
"""

import math
from datetime import datetime, timedelta
from typing import NamedTuple

from pymongo.errors import DuplicateKeyError

from app.core.exceptions import AppError
from app.core.time import utcnow
from app.db import mongo

TOO_MANY = "Too many attempts. Wait a moment and try again."


class Limit(NamedTuple):
    """At most `count` requests per subject in each `window`, counted under `name`."""

    name: str
    count: int
    window: timedelta


class Window(NamedTuple):
    """One fixed-window counter to take from, and the error raised when it's full."""

    key: str
    limit: int
    expires_at: datetime
    error: AppError


async def take_all(windows: list[Window]) -> None:
    """Count one against every window, or raise the first full one's error without counting any."""
    taken: list[str] = []
    for window in windows:
        if not await take_one(window.key, window.limit, window.expires_at):
            if taken:
                await mongo.usage_counters().update_many({"_id": {"$in": taken}}, {"$inc": {"count": -1}})
            raise window.error
        taken.append(window.key)


async def take_one(key: str, limit: int, expires_at: datetime) -> bool:
    if limit <= 0:
        return False
    try:
        await mongo.usage_counters().update_one(
            {"_id": key, "count": {"$lt": limit}},
            {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": expires_at}},
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return True


async def allow(limit: Limit, subject: str) -> bool:
    """Count one request by `subject` (an IP, a hashed email) in the current window; False once it's full."""
    now = utcnow()
    bucket = int(now.timestamp() // limit.window.total_seconds())
    return await take_one(f"{limit.name}:{subject}:{bucket}", limit.count, now + 2 * limit.window)


async def take(limit: Limit, subject: str) -> None:
    """`allow`, or raise 429."""
    if not await allow(limit, subject):
        raise AppError(TOO_MANY, error_code="rate_limited", status_code=429)


def retry_after(sent_at: datetime, cooldown: timedelta) -> int:
    """Whole seconds until `cooldown` after `sent_at` has passed; 0 once it has."""
    return max(0, math.ceil((sent_at + cooldown - utcnow()).total_seconds()))


def cooldown_error(message: str, error_code: str, seconds: int) -> AppError:
    return AppError(message, error_code=error_code, status_code=429, meta={"retry_after_seconds": seconds})
