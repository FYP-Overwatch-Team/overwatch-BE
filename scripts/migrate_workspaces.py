"""One-off migration from per-user data to workspaces.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: only documents still keyed by user are touched.

- every user gets a personal workspace (sharing their id) and owns it
- repos, Jira projects and tickets move onto that workspace
- OAuth tokens are keyed by `owner_id`: the user for GitHub, the workspace for Jira
- the old per-user unique indexes are dropped, then the new ones are created

Run it with the API stopped, before deploying the workspace-aware backend: the new
unique indexes can't be built while documents are still keyed by user.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import mongo  # noqa: E402
from app.workspaces import repository as workspaces  # noqa: E402

OLD_INDEXES = [
    ("repos", "user_id_1_repo_full_name_1"),
    ("jira_projects", "user_id_1_project_key_1"),
    ("tickets", "user_id_1_ticket_key_1"),
    ("tickets", "user_id_1_project_key_1"),
    ("oauth_tokens", "user_id_1_provider_1"),
]

# a user's personal workspace shares their id, so the owner field is just renamed
RENAMES = [
    ("jira_projects", "workspace_id"),
    ("tickets", "workspace_id"),
    ("oauth_tokens", "owner_id"),
]


async def migrate(apply: bool) -> list[str]:
    db = mongo.get_db()
    report = []

    missing = [user async for user in mongo.users().find({}) if not await workspaces.get(user["_id"])]
    report.append(f"personal workspaces to create: {len(missing)}")

    if apply:
        existing = {name for collection in {c for c, _ in OLD_INDEXES} for name in await db[collection].index_information()}
        for collection, name in OLD_INDEXES:
            if name in existing:
                await db[collection].drop_index(name)
                report.append(f"dropped index {collection}.{name}")
        for user in missing:
            await workspaces.ensure_personal(user)

    repos = [doc async for doc in mongo.repos().find({"user_id": {"$exists": True}}, {"user_id": 1})]
    report.append(f"repos to move: {len(repos)}")
    if apply:
        for doc in repos:
            await mongo.repos().update_one(
                {"_id": doc["_id"]},
                {"$set": {"workspace_id": doc["user_id"], "connected_by": doc["user_id"]}, "$unset": {"user_id": ""}},
            )

    for collection, field in RENAMES:
        query = {"user_id": {"$exists": True}}
        count = await db[collection].count_documents(query)
        if apply and count:
            count = (await db[collection].update_many(query, {"$rename": {"user_id": field}})).modified_count
        report.append(f"{collection} to move: {count}")

    if apply:
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
