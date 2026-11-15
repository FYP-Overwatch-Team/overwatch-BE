"""One-off migration: work item types for existing workspaces and items.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env. Safe to run again.

- every workspace gets its default types (Task, Bug, Research)
- native items without a type become Tasks
- tracker targets are set to sync in full next, which gives their items a type and hides
  items in statuses their tracker's board leaves out
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.constants import NATIVE_SOURCE  # noqa: E402
from app.db import mongo  # noqa: E402
from app.work_item_types import service as work_item_types  # noqa: E402


async def migrate(apply: bool) -> list[str]:
    workspaces = [doc["_id"] async for doc in mongo.workspaces().find({}, {"_id": 1})]
    untyped = {"source": NATIVE_SOURCE, "type_id": None}
    items = await mongo.work_items().count_documents(untyped)
    targets = await mongo.sync_targets().count_documents({})
    if apply:
        for workspace_id in workspaces:
            task = await work_item_types.default_type_id(workspace_id)
            await mongo.work_items().update_many({**untyped, "workspace_id": workspace_id}, {"$set": {"type_id": task}})
        await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})
        await mongo.ensure_indexes()
    return [f"workspaces to give types: {len(workspaces)}", f"native items to make Tasks: {items}", f"targets to sync in full: {targets}"]


async def main(apply: bool) -> None:
    mongo.connect()
    try:
        for line in await migrate(apply):
            print(("" if apply else "[dry run] ") + line)
    finally:
        mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
