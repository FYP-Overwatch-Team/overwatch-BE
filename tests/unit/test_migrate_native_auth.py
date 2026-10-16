import importlib.util
from datetime import timedelta
from pathlib import Path

from app.core.time import utcnow
from app.db import mongo

spec = importlib.util.spec_from_file_location(
    "migrate_native_auth", Path(__file__).resolve().parents[2] / "scripts" / "migrate_native_auth.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def seed() -> None:
    now = utcnow()
    for i, login in enumerate(("Alice", "admin", "bob", "x")):
        await mongo.users().insert_one({"_id": f"u{i}", "github_id": i, "github_login": login, "created_at": now + timedelta(seconds=i)})
    await mongo.users().insert_one({"_id": "u9", "github_id": 9, "github_login": "carol", "username": "carol"})
    await mongo.workspace_invites().insert_one({"_id": "i1", "github_login": "bob", "token_hash": "a"})
    await mongo.workspace_invites().insert_one({"_id": "i2", "github_login": None, "token_hash": "b"})


async def test_a_dry_run_changes_nothing():
    await seed()
    report = await migration.migrate(apply=False)
    assert "usernames from GitHub logins: 2" in report
    assert await mongo.users().count_documents({"username": {"$type": "string"}}) == 1


async def test_usernames_and_invite_kinds_are_filled_in_once():
    await seed()
    report = await migration.migrate(apply=True)
    again = await migration.migrate(apply=True)

    names = {doc["_id"]: doc.get("username") async for doc in mongo.users().find()}
    kinds = {doc["_id"]: doc["kind"] async for doc in mongo.workspace_invites().find()}
    assert names == {"u0": "alice", "u1": None, "u2": "bob", "u3": None, "u9": "carol"}
    assert kinds == {"i1": "github", "i2": "link"}
    assert "users left to choose a username: 2" in report
    assert "usernames from GitHub logins: 0" in again
