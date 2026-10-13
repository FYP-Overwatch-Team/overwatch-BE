import importlib.util
from pathlib import Path

import pytest

from app.db import mongo, oauth_tokens

spec = importlib.util.spec_from_file_location(
    "migrate_connectors", Path(__file__).resolve().parents[2] / "scripts" / "migrate_connectors.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

RAW = {"id": "10001", "key": "OVR-1", "fields": {
    "summary": "Set up CI", "status": {"name": "Done", "statusCategory": {"key": "done"}},
    "updated": "2026-07-10T12:00:00.000+0000",
}}


@pytest.fixture
async def legacy_db():
    """A workspace with Jira connected the old way: token with cloud_id, jira_projects, tickets."""
    await mongo.workspace_members().insert_one({"workspace_id": "ws1", "user_id": "u1", "role": "owner", "joined_at": 1})
    await oauth_tokens.save("ws1", "jira", {"access_token": "a", "refresh_token": "r"}, cloud_id="cloud-1")
    await mongo.jira_projects().insert_one({
        "workspace_id": "ws1", "project_key": "OVR", "cloud_id": "cloud-1", "status": "active",
        "sync_status": "in_progress", "connected_at": 1,
    })
    await mongo.tickets().insert_one({"workspace_id": "ws1", "project_key": "OVR", "ticket_key": "OVR-1", "raw": RAW})


async def test_a_dry_run_changes_nothing(legacy_db):
    report = await migration.migrate(apply=False)

    assert report == ["connections to write: 1", "sync targets to write: 1", "work items to write: 1"]
    assert await mongo.connections().count_documents({}) == 0
    assert await mongo.work_items().count_documents({}) == 0


async def test_jira_moves_onto_the_connector_collections(legacy_db):
    await migration.migrate(apply=True)

    conn = await mongo.connections().find_one({})
    assert (conn["workspace_id"], conn["status"], conn["connected_by"]) == ("ws1", "active", "u1")
    assert conn["external_account"]["id"] == "cloud-1"
    assert "write:jira-work" not in conn["scopes"]  # reconnect before editing from the board

    target = await mongo.sync_targets().find_one({})
    assert (target["key"], target["sync_status"], target["last_full_sync_at"]) == ("OVR", "pending", None)

    item = await mongo.work_items().find_one({})
    assert (item["key"], item["external_id"], item["status_category"], item["title"]) == ("OVR-1", "10001", "done", "Set up CI")
    assert await mongo.tickets().count_documents({}) == 1  # kept until --drop-legacy


async def test_running_it_twice_is_harmless_and_legacy_can_be_dropped(legacy_db):
    await migration.migrate(apply=True)
    await migration.migrate(apply=True, drop_legacy=True)

    assert [await mongo.connections().count_documents({}), await mongo.sync_targets().count_documents({}),
            await mongo.work_items().count_documents({})] == [1, 1, 1]
    assert await mongo.tickets().count_documents({}) == 0
    assert await mongo.jira_projects().count_documents({}) == 0
