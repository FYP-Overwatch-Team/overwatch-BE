import importlib.util
from pathlib import Path

import pytest

from app.db import mongo

spec = importlib.util.spec_from_file_location(
    "migrate_workspaces", Path(__file__).resolve().parents[2] / "scripts" / "migrate_workspaces.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture
async def legacy_db():
    """Data and indexes as they were before workspaces."""
    db = mongo.get_db()
    for collection in ("repos", "jira_projects", "tickets", "oauth_tokens"):
        await db[collection].drop_indexes()
    await mongo.repos().create_index([("user_id", 1), ("repo_full_name", 1)], unique=True)
    await mongo.tickets().create_index([("user_id", 1), ("ticket_key", 1)], unique=True)
    await mongo.oauth_tokens().create_index([("user_id", 1), ("provider", 1)], unique=True)

    for uid, login in (("u1", "alice"), ("u2", "bob")):
        await mongo.users().insert_one({"_id": uid, "github_id": len(login), "github_login": login, "name": None})
        await mongo.repos().insert_one({"user_id": uid, "repo_full_name": "acme/api", "parse_status": "done"})
        await mongo.oauth_tokens().insert_one({"user_id": uid, "provider": "github"})
        await mongo.oauth_tokens().insert_one({"user_id": uid, "provider": "jira", "cloud_id": "c"})
    await mongo.jira_projects().insert_one({"user_id": "u1", "project_key": "OVR"})
    await mongo.tickets().insert_one({"user_id": "u1", "project_key": "OVR", "ticket_key": "OVR-1"})
    return db


async def test_a_dry_run_changes_nothing(legacy_db):
    report = await migration.migrate(apply=False)

    assert "personal workspaces to create: 2" in report
    assert "repos to move: 2" in report
    assert await mongo.workspaces().count_documents({}) == 0
    assert await mongo.repos().count_documents({"user_id": {"$exists": True}}) == 2


async def test_everything_moves_onto_each_users_personal_workspace(legacy_db):
    await migration.migrate(apply=True)

    assert {(w["_id"], w["name"]) async for w in mongo.workspaces().find({"personal": True})} == {("u1", "alice"), ("u2", "bob")}
    assert await mongo.workspace_members().count_documents({"role": "owner"}) == 2
    repo = await mongo.repos().find_one({"workspace_id": "u1"})
    assert (repo["connected_by"], "user_id" in repo) == ("u1", False)
    assert await mongo.jira_projects().find_one({"workspace_id": "u1", "project_key": "OVR"})
    assert await mongo.tickets().find_one({"workspace_id": "u1", "ticket_key": "OVR-1"})
    # the personal workspace shares the user's id, so both tokens keep their owner
    assert {(t["owner_id"], t["provider"]) async for t in mongo.oauth_tokens().find({})} == {
        ("u1", "github"), ("u1", "jira"), ("u2", "github"), ("u2", "jira"),
    }


async def test_old_indexes_are_replaced(legacy_db):
    await migration.migrate(apply=True)

    assert "user_id_1_repo_full_name_1" not in await mongo.repos().index_information()
    assert "workspace_id_1_repo_full_name_1" in await mongo.repos().index_information()
    # the same repo can now be connected by a second workspace
    await mongo.repos().insert_one({"workspace_id": "team", "repo_full_name": "acme/api"})


async def test_running_it_twice_is_harmless(legacy_db):
    await migration.migrate(apply=True)
    report = await migration.migrate(apply=True)

    assert "personal workspaces to create: 0" in report
    assert "repos to move: 0" in report
    assert await mongo.workspaces().count_documents({}) == 2
