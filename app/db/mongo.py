from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import get_settings

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def connect() -> AsyncIOMotorDatabase:
    global _client, _db
    settings = get_settings()
    _client = AsyncIOMotorClient(settings.mongo_uri)
    _db = _client[settings.mongo_db_name]
    return _db


def close() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Mongo is not connected; call connect() on startup")
    return _db


def use_db(db: AsyncIOMotorDatabase) -> None:
    """Inject a database instance (tests use mongomock-motor here)."""
    global _db
    _db = db


# Collection accessors — single place to change names/indexes later.
def users():
    return get_db()["users"]


def oauth_tokens():
    return get_db()["oauth_tokens"]


def refresh_tokens():
    return get_db()["refresh_tokens"]


def repos():
    return get_db()["repos"]


def jira_projects():
    return get_db()["jira_projects"]


def tickets():
    return get_db()["tickets"]


def webhook_deliveries():
    return get_db()["webhook_deliveries"]


def file_facts():
    """Knowledge-graph extraction facts, one document per file."""
    return get_db()["file_facts"]


async def ensure_indexes() -> None:
    """Create the indexes the hot paths depend on. Idempotent — safe on every boot.

    The two TTL indexes are what keep the session store from growing without
    bound: Mongo drops refresh-token rows once they pass `expires_at`, and
    webhook delivery ids 30 days after receipt (GitHub never retries that late,
    so anything older can no longer cause a duplicate re-parse).
    """
    # Session lookup on every /auth/refresh, plus family-wide revocation.
    await refresh_tokens().create_index("token_hash", unique=True)
    await refresh_tokens().create_index("family_id")
    await refresh_tokens().create_index("expires_at", expireAfterSeconds=0)

    await users().create_index("github_id", unique=True)
    await oauth_tokens().create_index([("user_id", 1), ("provider", 1)], unique=True)
    await repos().create_index([("user_id", 1), ("repo_full_name", 1)], unique=True)
    await repos().create_index("repo_full_name")
    await jira_projects().create_index([("user_id", 1), ("project_key", 1)], unique=True)
    # Must match the sync upsert filter exactly — that keys on ticket_key alone,
    # so a ticket moved between projects updates rather than duplicating.
    await tickets().create_index([("user_id", 1), ("ticket_key", 1)], unique=True)
    await tickets().create_index([("user_id", 1), ("project_key", 1)])

    # Extraction cache: read on every sync to decide what changed, and always
    # scoped to one repository.
    await file_facts().create_index([("repo_full_name", 1), ("path", 1)], unique=True)

    # Webhook dedup: looked up on every delivery, expired 30 days after receipt.
    await webhook_deliveries().create_index("delivery_id", unique=True)
    await webhook_deliveries().create_index("received_at", expireAfterSeconds=30 * 24 * 3600)
