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


def file_parses():
    return get_db()["file_parses"]
