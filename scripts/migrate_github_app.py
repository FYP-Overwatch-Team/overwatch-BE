"""One-off migration from the GitHub OAuth app to the GitHub App.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.

- old OAuth tokens (no refresh token) are marked needs_reauth, so users sign in again
- per-repo webhook fields are removed (the App has one webhook)
- repos without an installation are marked needs_install; the user reconnects them

Delete the old per-repo webhooks on GitHub *before* running this (they were made
with the old OAuth tokens) or they keep firing and fail signature checks.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.constants import AccessStatus, ConnectionStatus, Provider  # noqa: E402
from app.db import mongo  # noqa: E402

STEPS = [
    ("oauth_tokens", {"provider": Provider.GITHUB, "refresh_token_encrypted": {"$exists": False}}, {"$set": {"status": ConnectionStatus.NEEDS_REAUTH}}),
    ("repos", {"webhook_status": {"$exists": True}}, {"$unset": {"webhook_id": "", "webhook_secret_encrypted": "", "webhook_status": "", "webhook_error": ""}}),
    ("repos", {"installation_id": {"$exists": False}}, {"$set": {"access_status": AccessStatus.NEEDS_INSTALL}}),
]


async def main(apply: bool) -> None:
    db = mongo.connect()
    try:
        for collection, query, update in STEPS:
            count = await db[collection].count_documents(query)
            if apply and count:
                count = (await db[collection].update_many(query, update)).modified_count
            print(f"{'updated' if apply else 'would update'} {count} in {collection}: {update}")
    finally:
        mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
