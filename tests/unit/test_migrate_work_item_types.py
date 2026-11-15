import importlib.util
from pathlib import Path

from app.db import mongo

spec = importlib.util.spec_from_file_location(
    "migrate_work_item_types", Path(__file__).resolve().parents[2] / "scripts" / "migrate_work_item_types.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def seed() -> None:
    await mongo.workspaces().insert_one({"_id": "ws1", "name": "Acme"})
    await mongo.work_items().insert_many([
        {"_id": "a", "workspace_id": "ws1", "source": "native", "key": "OW-1"},
        {"_id": "b", "workspace_id": "ws1", "source": "jira", "key": "OVR-1"},
    ])
    await mongo.sync_targets().insert_one({"workspace_id": "ws1", "provider": "jira", "key": "OVR", "last_full_sync_at": 1})


async def test_a_dry_run_changes_nothing():
    await seed()
    assert await migration.migrate(apply=False) == ["workspaces to give types: 1", "native items to make Tasks: 1", "targets to sync in full: 1"]
    assert await mongo.work_item_types().count_documents({}) == 0


async def test_native_items_become_tasks_and_trackers_resync_in_full():
    await seed()
    await migration.migrate(apply=True)
    await migration.migrate(apply=True)

    task = await mongo.work_item_types().find_one({"name_key": "task"})
    assert await mongo.work_item_types().count_documents({}) == 3
    assert (await mongo.work_items().find_one({"_id": "a"}))["type_id"] == task["_id"]
    assert "type_id" not in await mongo.work_items().find_one({"_id": "b"})
    assert (await mongo.sync_targets().find_one({}))["last_full_sync_at"] is None
