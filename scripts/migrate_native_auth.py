"""One-off migration for email and password sign-in.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: only users without a username and invites without a kind are touched.

- the old `github_id` unique index is replaced by a partial one, so users without GitHub can exist
- every GitHub user gets their lowercased login as username, oldest account first, when it fits the
  rules and is free; the rest choose one the next time they sign in
- existing invites get their kind: locked to a GitHub account, or open to anyone with the link
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import usernames  # noqa: E402
from app.core.constants import InviteKind  # noqa: E402
from app.db import mongo  # noqa: E402


async def migrate(apply: bool) -> list[str]:
    report: list[str] = []
    if apply:
        # drops the old github_id index and builds the new ones
        await mongo.ensure_indexes()

    taken = {doc["username"] async for doc in mongo.users().find({"username": {"$type": "string"}}, {"username": 1})}
    given, skipped = 0, 0
    async for user in mongo.users().find({"username": {"$in": [None]}}).sort("created_at", 1):
        candidate = usernames.default_from(user.get("github_login"))
        if candidate is None or candidate in taken:
            skipped += 1
            continue
        taken.add(candidate)
        given += 1
        if apply:
            await mongo.users().update_one({"_id": user["_id"]}, {"$set": {"username": candidate}})
    report.append(f"usernames from GitHub logins: {given}")
    report.append(f"users left to choose a username: {skipped}")

    for kind, query in (
        (InviteKind.GITHUB, {"kind": {"$exists": False}, "github_login": {"$type": "string"}}),
        (InviteKind.LINK, {"kind": {"$exists": False}}),
    ):
        count = await mongo.workspace_invites().count_documents(query)
        if apply and count:
            count = (await mongo.workspace_invites().update_many(query, {"$set": {"kind": kind}})).modified_count
        report.append(f"invites marked {kind}: {count}")
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
