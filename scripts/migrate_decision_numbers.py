"""One-off migration to numbered decisions: each decision gets its short id's number (D-1, D-2...).

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: only decisions without a `number` are touched.

Each workspace's decisions are numbered oldest first, from the same counter new decisions
count on, so no number is given twice. Run it right before deploying the backend that numbers
decisions and again right after: a decision recorded in between is numbered ahead of older
ones still waiting, so numbers stay unique but may not follow the order they were recorded in.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import mongo  # noqa: E402
from app.decisions.service import next_number  # noqa: E402

UNNUMBERED = {"number": {"$exists": False}}


async def migrate(apply: bool) -> list[str]:
    numbered = 0
    cursor = mongo.decisions().find(UNNUMBERED, {"workspace_id": 1}).sort([("created_at", 1), ("_id", 1)])
    async for decision in cursor:
        numbered += 1
        if apply:
            number = await next_number(decision["workspace_id"])
            await mongo.decisions().update_one({"_id": decision["_id"], **UNNUMBERED}, {"$set": {"number": number}})
    return [f"{numbered} decisions numbered"]


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
