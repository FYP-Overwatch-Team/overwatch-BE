"""One-off migration to boards: every work item gets the column it sits in.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: only items without a `column_id` are touched.

- items stored before boards get the default column of their category (the default
  columns' ids are the categories, so this is what the board already shows)
- boards themselves are created on first sight, and each target's next full sync reads
  its statuses and offers the tracker's columns, so nothing else needs writing here
- the full sync is forced, so status ids and the column offer arrive right away

Run it before or right after deploying the board-aware backend; until then the API
reads a missing `column_id` as the item's category anyway.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.constants import StatusCategory  # noqa: E402
from app.db import mongo  # noqa: E402

MISSING = {"column_id": {"$exists": False}}


async def migrate(apply: bool) -> list[str]:
    report = []
    for category in StatusCategory:
        query = {**MISSING, "status_category": category.value}
        count = await mongo.work_items().count_documents(query)
        if count:
            report.append(f"{count} {category.value} items -> column {category.value}")
            if apply:
                await mongo.work_items().update_many(query, {"$set": {"column_id": category.value}})
    targets = await mongo.sync_targets().count_documents({"status": "active"})
    report.append(f"{targets} targets set to run a full sync next")
    if apply:
        await mongo.sync_targets().update_many({"status": "active"}, {"$set": {"last_full_sync_at": None}})
        await mongo.ensure_indexes()
    return report


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
