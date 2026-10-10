"""Connected Jira projects (the `jira_projects` collection)."""

from pymongo import ReturnDocument

from app.core.time import utcnow
from app.db import mongo

ACTIVE = {"status": {"$ne": "disconnected"}}


async def find_active(user_id: str, project_key: str) -> dict | None:
    return await mongo.jira_projects().find_one({"user_id": user_id, "project_key": project_key, **ACTIVE})


async def latest_active(user_id: str) -> dict | None:
    return await mongo.jira_projects().find_one({"user_id": user_id, **ACTIVE}, sort=[("connected_at", -1)])


async def connect(user_id: str, project_key: str, cloud_id: str) -> dict:
    """Connect (or reconnect) a project, ready for its first ticket sync."""
    now = utcnow()
    return await mongo.jira_projects().find_one_and_update(
        {"user_id": user_id, "project_key": project_key},
        {"$set": {
            "cloud_id": cloud_id,
            "status": "active",
            "sync_status": "pending",
            "sync_error": None,
            "last_synced_at": None,
            "disconnected_at": None,
            "connected_at": now,
            "updated_at": now,
        }},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def disconnect_all(user_id: str) -> None:
    """Soft-disconnect: imported projects and tickets are kept."""
    now = utcnow()
    await mongo.jira_projects().update_many(
        {"user_id": user_id, **ACTIVE},
        {"$set": {"status": "disconnected", "disconnected_at": now, "updated_at": now}},
    )
