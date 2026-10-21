"""A workspace's connections (one per provider) and the targets each one syncs."""

import uuid
from datetime import timedelta

from pymongo import ReturnDocument

from app.core.constants import ConnectionStatus, JobStatus
from app.core.time import utcnow
from app.db import mongo

ACTIVE = {"status": {"$ne": ConnectionStatus.DISCONNECTED}}


async def get_connection(workspace_id: str, provider: str) -> dict | None:
    return await mongo.connections().find_one({"workspace_id": workspace_id, "provider": provider})


async def active_connection(workspace_id: str, provider: str) -> dict | None:
    return await mongo.connections().find_one({"workspace_id": workspace_id, "provider": provider, **ACTIVE})


async def list_connections(workspace_id: str) -> list[dict]:
    return [doc async for doc in mongo.connections().find({"workspace_id": workspace_id})]


async def save_connection(workspace_id: str, provider: str, **fields) -> None:
    """Connect (or reconnect) a provider; a reconnect keeps the connection's id."""
    now = utcnow()
    await mongo.connections().update_one(
        {"workspace_id": workspace_id, "provider": provider},
        {
            "$set": {**fields, "status": ConnectionStatus.ACTIVE, "connected_at": now, "disconnected_at": None, "updated_at": now},
            "$setOnInsert": {"_id": uuid.uuid4().hex},
        },
        upsert=True,
    )


async def set_scopes(workspace_id: str, provider: str, scopes: list[str]) -> None:
    await mongo.connections().update_one(
        {"workspace_id": workspace_id, "provider": provider}, {"$set": {"scopes": scopes, "updated_at": utcnow()}},
    )


async def disconnect(workspace_id: str, provider: str) -> None:
    """Soft-disconnect the connection and its targets: synced items are kept."""
    now = utcnow()
    gone = {"$set": {"status": ConnectionStatus.DISCONNECTED, "disconnected_at": now, "updated_at": now}}
    await mongo.connections().update_one({"workspace_id": workspace_id, "provider": provider}, gone)
    await mongo.sync_targets().update_many({"workspace_id": workspace_id, "provider": provider, **ACTIVE}, gone)


async def find_target(workspace_id: str, provider: str, key: str) -> dict | None:
    return await mongo.sync_targets().find_one({"workspace_id": workspace_id, "provider": provider, "key": key, **ACTIVE})


async def latest_target(workspace_id: str, provider: str) -> dict | None:
    return await mongo.sync_targets().find_one(
        {"workspace_id": workspace_id, "provider": provider, **ACTIVE}, sort=[("connected_at", -1)],
    )


async def list_targets(workspace_id: str, provider: str | None = None) -> list[dict]:
    query = {"workspace_id": workspace_id, **ACTIVE, **({"provider": provider} if provider else {})}
    return [doc async for doc in mongo.sync_targets().find(query).sort("connected_at", 1)]


def all_targets():
    return mongo.sync_targets().find(ACTIVE)


async def add_target(workspace_id: str, provider: str, key: str, name: str | None) -> dict:
    """Connect (or reconnect) a target, ready for its first, full sync."""
    now = utcnow()
    return await mongo.sync_targets().find_one_and_update(
        {"workspace_id": workspace_id, "provider": provider, "key": key},
        {"$set": {
            "name": name,
            "status": ConnectionStatus.ACTIVE,
            "sync_status": JobStatus.PENDING,
            "sync_error": None,
            "last_synced_at": None,
            "last_full_sync_at": None,
            "disconnected_at": None,
            "connected_at": now,
            "updated_at": now,
        }},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def remove_target(workspace_id: str, provider: str, key: str) -> bool:
    now = utcnow()
    result = await mongo.sync_targets().update_one(
        {"workspace_id": workspace_id, "provider": provider, "key": key, **ACTIVE},
        {"$set": {"status": ConnectionStatus.DISCONNECTED, "disconnected_at": now, "updated_at": now}},
    )
    return result.matched_count > 0


async def claim_target(workspace_id: str, provider: str, key: str, stale_after: timedelta) -> dict | None:
    """Mark the target as syncing; None if it's gone or already syncing (the overlap lock).

    A sync running longer than `stale_after` was lost (a restart mid-sync) and is taken over;
    so is one marked running before syncs recorded when they started.
    """
    now = utcnow()
    running_too_long = {"$or": [{"sync_started_at": {"$lt": now - stale_after}}, {"sync_started_at": None}]}
    return await mongo.sync_targets().find_one_and_update(
        {
            "workspace_id": workspace_id, "provider": provider, "key": key, **ACTIVE,
            "$or": [{"sync_status": {"$ne": JobStatus.IN_PROGRESS}}, running_too_long],
        },
        {"$set": {"sync_status": JobStatus.IN_PROGRESS, "sync_started_at": now, "updated_at": now}},
    )


async def finish_target(workspace_id: str, provider: str, key: str, **fields) -> None:
    await mongo.sync_targets().update_one(
        {"workspace_id": workspace_id, "provider": provider, "key": key},
        {"$set": {**fields, "updated_at": utcnow()}},
    )


async def end_sync(workspace_id: str, provider: str, key: str, **fields) -> None:
    """Record how a sync went and release its lock."""
    await finish_target(workspace_id, provider, key, **fields, sync_started_at=None)
