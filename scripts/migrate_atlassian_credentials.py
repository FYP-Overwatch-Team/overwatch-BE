"""One-off migration: each workspace's Jira token becomes the shared Atlassian token.

Jira and Confluence share one OAuth app, so their token is stored once under `atlassian`.
Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: a workspace that already has an Atlassian token keeps it.

Run it with the API stopped, before deploying the backend that reads `atlassian`.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.constants import CredentialGroup, Provider  # noqa: E402
from app.db import mongo  # noqa: E402


async def migrate(apply: bool) -> list[str]:
    tokens = [t async for t in mongo.oauth_tokens().find({"provider": Provider.JIRA, "owner_id": {"$exists": True}})]
    renamed = skipped = 0
    for token in tokens:
        if await mongo.oauth_tokens().find_one({"owner_id": token["owner_id"], "provider": CredentialGroup.ATLASSIAN}):
            skipped += 1
            continue
        renamed += 1
        if apply:
            await mongo.oauth_tokens().update_one({"_id": token["_id"]}, {"$set": {"provider": CredentialGroup.ATLASSIAN}})
    return [f"jira tokens to rename: {renamed}", f"already moved: {skipped}"]


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
