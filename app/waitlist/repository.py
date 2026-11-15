from pymongo.errors import DuplicateKeyError

from app.db import mongo


async def add(doc: dict) -> bool:
    """Add someone to the waitlist; False when their email is already on it."""
    try:
        result = await mongo.waitlist().update_one({"email": doc["email"]}, {"$setOnInsert": doc}, upsert=True)
    except DuplicateKeyError:
        return False
    return result.upserted_id is not None
