"""Short leases, so one AI read of a source (or one workspace's check) runs at a time and isn't paid for twice.

A lease is an atomic claim on a record that's free or whose lease ran out; it lapses by itself if
the process holding it dies.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.core.exceptions import ConflictError
from app.core.time import utcnow


@asynccontextmanager
async def lease(collection, key: dict, lasts: timedelta, busy: ConflictError) -> AsyncIterator[None]:
    """Hold the record at `key` while the block runs; raise `busy` if someone else holds it."""
    now = utcnow()
    free = {"$or": [{"running_until": None}, {"running_until": {"$lt": now}}]}
    try:
        # a held record doesn't match, so the upsert tries to add a second one and the unique key refuses it
        await collection.update_one({**key, **free}, {"$set": {"running_until": now + lasts}}, upsert=True)
    except DuplicateKeyError as exc:
        raise busy from exc
    try:
        yield
    finally:
        await collection.update_one(key, {"$set": {"running_until": None}})
