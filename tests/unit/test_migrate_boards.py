import importlib.util
from pathlib import Path

from app.db import mongo

spec = importlib.util.spec_from_file_location("migrate_boards", Path(__file__).resolve().parents[2] / "scripts" / "migrate_boards.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def seed():
    await mongo.work_items().insert_many([
        {"_id": "a", "workspace_id": "ws1", "source": "jira", "external_id": "1", "status_category": "in_progress"},
        {"_id": "b", "workspace_id": "ws1", "source": "jira", "external_id": "2", "status_category": "done"},
        {"_id": "c", "workspace_id": "ws1", "source": "jira", "external_id": "3", "status_category": "todo", "column_id": "col_custom"},
    ])
    await mongo.sync_targets().insert_one({"workspace_id": "ws1", "provider": "jira", "key": "OVR", "status": "active", "last_full_sync_at": 1})


async def test_a_dry_run_writes_nothing():
    await seed()

    report = await migration.migrate(apply=False)

    assert report == ["1 in_progress items -> column in_progress", "1 done items -> column done", "1 targets set to run a full sync next"]
    assert await mongo.work_items().count_documents({"column_id": {"$exists": True}}) == 1


async def test_apply_backfills_only_items_without_a_column_and_is_repeatable():
    await seed()

    await migration.migrate(apply=True)
    again = await migration.migrate(apply=True)

    columns = {doc["_id"]: doc["column_id"] async for doc in mongo.work_items().find({})}
    assert columns == {"a": "in_progress", "b": "done", "c": "col_custom"}
    assert (await mongo.sync_targets().find_one({}))["last_full_sync_at"] is None
    assert again == ["1 targets set to run a full sync next"]
