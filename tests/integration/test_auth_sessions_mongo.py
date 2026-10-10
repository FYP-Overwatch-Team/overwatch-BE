"""Races that mongomock can't reproduce (it never interleaves operations).

Runs against a real Mongo (`docker compose up -d mongo`) and skips otherwise.
"""

import asyncio
import os
import uuid

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from app.auth import service
from app.core.exceptions import UnauthorizedError
from app.db import mongo

PROFILE = {"id": 42, "login": "octocat", "name": "Octo Cat", "avatar_url": "https://img"}


@pytest.fixture
async def real_mongo():
    client = AsyncIOMotorClient(
        os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=500,
    )
    try:
        await client.admin.command("ping")
    except Exception as exc:
        client.close()
        pytest.skip(f"Mongo unavailable: {type(exc).__name__}")
    db_name = f"overwatch_test_{uuid.uuid4().hex[:8]}"
    mongo.use_db(client[db_name])
    await mongo.ensure_indexes()
    yield
    await client.drop_database(db_name)
    client.close()


async def test_concurrent_rotation_of_one_token_succeeds_once(real_mongo):
    raw = await service.create_session("user-1")
    results = await asyncio.gather(*(service.rotate(raw) for _ in range(5)), return_exceptions=True)

    succeeded = [r for r in results if not isinstance(r, Exception)]
    errors = {r.error_code for r in results if isinstance(r, UnauthorizedError)}
    assert len(succeeded) == 1
    assert errors == {"refresh_token_reused"}


async def test_concurrent_first_sign_ins_create_one_user(real_mongo):
    await asyncio.gather(*(service.sign_in_with_github(PROFILE, {"access_token": "ghu"}) for _ in range(5)), return_exceptions=True)
    assert await mongo.users().count_documents({"github_id": 42}) == 1
