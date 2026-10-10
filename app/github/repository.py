"""Connected repositories (the `repos` collection).

A repo doc belongs to one user. Several users can connect the same repo; the
checkout, facts and graph are keyed by repo name and shared between them.
"""

from datetime import datetime

from pymongo import ReturnDocument

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.repo_identity import validate_repo_full_name
from app.core.time import utcnow
from app.db import mongo


async def find(user_id: str, repo_full_name: str) -> dict | None:
    return await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})


async def get_connected(user_id: str, repo_full_name: str) -> dict:
    validate_repo_full_name(repo_full_name)
    repo = await find(user_id, repo_full_name)
    if repo is None:
        raise NotFoundError("repository is not connected", error_code="repo_not_connected")
    return repo


async def get_readable(user_id: str, repo_full_name: str) -> dict:
    """A connected repo whose GitHub access still stands; its graph is not served after a revoke."""
    repo = await get_connected(user_id, repo_full_name)
    if repo.get("access_status", "active") != "active":
        raise ForbiddenError("GitHub access to this repository was revoked", error_code="repo_access_revoked")
    return repo


async def list_for_user(user_id: str) -> list[dict]:
    return [doc async for doc in mongo.repos().find({"user_id": user_id}).sort("connected_at", 1)]


async def insert(doc: dict) -> None:
    await mongo.repos().insert_one(doc)


async def update(user_id: str, repo_full_name: str, fields: dict) -> None:
    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
        {"$set": {**fields, "updated_at": utcnow()}},
    )


async def mark_pending(user_id: str, repo_full_name: str, stale_before: datetime) -> dict | None:
    """Queue a re-index unless one is already queued or running; returns None if it is.

    A queue or run older than `stale_before` was lost to a restart, so it can be retried; that's the
    same rule the indexing lock uses to reclaim it.
    """
    return await mongo.repos().find_one_and_update(
        {
            "user_id": user_id,
            "repo_full_name": repo_full_name,
            "$or": [
                {"parse_status": {"$nin": ["pending", "in_progress"]}},
                {"parse_status": "pending", "updated_at": {"$lt": stale_before}},
                {"parse_status": "in_progress", "parse_started_at": {"$not": {"$gte": stale_before}}},
            ],
        },
        {"$set": {"parse_status": "pending", "parse_error": None, "updated_at": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )


async def delete(user_id: str, repo_full_name: str) -> None:
    await mongo.repos().delete_one({"user_id": user_id, "repo_full_name": repo_full_name})


async def connected_by_others(repo_full_name: str, user_id: str) -> bool:
    return await mongo.repos().count_documents(
        {"repo_full_name": repo_full_name, "user_id": {"$ne": user_id}}, limit=1,
    ) > 0


async def find_by_github_repo(github_repo_id: int, installation_id: int) -> list[dict]:
    """Every user's connection of one repo through one installation."""
    return [doc async for doc in mongo.repos().find({"github_repo_id": github_repo_id, "installation_id": installation_id})]


async def reattach(installation_id: int, github_repo_ids: list[int]) -> int:
    """Point repos at a (new) installation that can read them again, e.g. after the App is reinstalled."""
    result = await mongo.repos().update_many(
        {"github_repo_id": {"$in": github_repo_ids}},
        {"$set": {"installation_id": installation_id, "access_status": "active", "updated_at": utcnow()}},
    )
    return result.modified_count


async def set_access_status(installation_id: int, status: str, *, github_repo_ids: list[int] | None = None) -> int:
    """Mark repos reachable (or not) after an installation changes; returns how many changed."""
    query: dict = {"installation_id": installation_id}
    if github_repo_ids is not None:
        query["github_repo_id"] = {"$in": github_repo_ids}
    result = await mongo.repos().update_many(query, {"$set": {"access_status": status, "updated_at": utcnow()}})
    return result.modified_count


async def all_repo_names() -> list[str]:
    return await mongo.repos().distinct("repo_full_name")
