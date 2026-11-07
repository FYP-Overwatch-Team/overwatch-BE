"""One-off migration: stored embeddings become packed float32 vectors instead of lists of doubles.

A list of 1024 doubles takes about 13 KB in BSON; packed float32 takes about 4 KB, which matters
on a small Atlas tier. Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME
from .env. Safe to run again: only vectors still stored as lists are rewritten.

Run it before deploying the backend that writes packed vectors, so the vector index never holds both.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from pymongo import UpdateOne

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import mongo  # noqa: E402
from app.embeddings.service import encode  # noqa: E402

BATCH = 500


async def migrate(apply: bool) -> list[str]:
    converted = 0
    batch: list[UpdateOne] = []
    async for doc in mongo.embeddings().find({"vector": {"$type": "array"}}, {"vector": 1}):
        converted += 1
        batch.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"vector": encode(doc["vector"])}}))
        if apply and len(batch) >= BATCH:
            await mongo.embeddings().bulk_write(batch, ordered=False)
            batch = []
    if apply and batch:
        await mongo.embeddings().bulk_write(batch, ordered=False)
    return [f"vectors to pack: {converted}"]


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
