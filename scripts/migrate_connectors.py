"""One-off migration from the Jira-only collections to the connector framework.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: everything is an upsert keyed the way the new collections are.

- each workspace's Jira token gets a `connections` document (read-only scopes: the
  grant predates write-back, so the board asks an admin to reconnect before editing)
- `jira_projects` become `sync_targets`, set to run a full sync next
- `tickets` become `work_items`, normalised by the Jira adapter
- `--drop-legacy` removes `jira_projects` and `tickets` once you're happy

Run it with the API stopped, before deploying the connector-aware backend.
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors.jira import JiraAdapter  # noqa: E402
from app.core.constants import AuthBackendName, ConnectionStatus, JobStatus, Provider, Role  # noqa: E402
from app.core.time import utcnow  # noqa: E402
from app.db import mongo  # noqa: E402
from app.work_items import repository as work_items  # noqa: E402

LEGACY_SCOPES = ["read:jira-work", "read:jira-user", "offline_access"]


async def _first_owner(workspace_id: str) -> str | None:
    member = await mongo.workspace_members().find_one({"workspace_id": workspace_id, "role": Role.OWNER}, sort=[("joined_at", 1)])
    return member and member["user_id"]


async def migrate(apply: bool, drop_legacy: bool = False) -> list[str]:
    report = []
    now = utcnow()
    # a ticket of a token without a site is unreachable anyway
    sites: dict[str, dict] = {}

    tokens = [t async for t in mongo.oauth_tokens().find({"provider": Provider.JIRA, "cloud_id": {"$exists": True}})]
    report.append(f"connections to write: {len(tokens)}")
    for token in tokens:
        workspace_id = token["owner_id"]
        existing = await mongo.connections().find_one({"workspace_id": workspace_id, "provider": Provider.JIRA})
        account = (existing or {}).get("external_account") or {"id": token["cloud_id"], "name": None, "url": None}
        sites[workspace_id] = account
        if apply and not existing:
            await mongo.connections().insert_one({
                "_id": uuid.uuid4().hex, "workspace_id": workspace_id, "provider": Provider.JIRA, "auth_backend": AuthBackendName.NATIVE_OAUTH,
                "status": ConnectionStatus.DISCONNECTED if token.get("status") == ConnectionStatus.DISCONNECTED else ConnectionStatus.ACTIVE,
                "scopes": LEGACY_SCOPES, "external_account": account, "connected_by": await _first_owner(workspace_id),
                "connected_at": token.get("updated_at", now), "disconnected_at": None, "updated_at": now,
            })

    projects = [p async for p in mongo.jira_projects().find({})]
    report.append(f"sync targets to write: {len(projects)}")
    for project in projects:
        if apply:
            await mongo.sync_targets().update_one(
                {"workspace_id": project["workspace_id"], "provider": Provider.JIRA, "key": project["project_key"]},
                {"$setOnInsert": {
                    "name": None, "status": project.get("status", ConnectionStatus.ACTIVE),
                    "sync_status": JobStatus.PENDING if project.get("sync_status") == JobStatus.IN_PROGRESS else project.get("sync_status", JobStatus.PENDING),
                    "sync_error": project.get("sync_error"), "last_synced_at": None, "last_full_sync_at": None,
                    "connected_at": project.get("connected_at", now), "disconnected_at": project.get("disconnected_at"),
                    "updated_at": now,
                }},
                upsert=True,
            )

    adapter = JiraAdapter()
    tickets = [t async for t in mongo.tickets().find({"raw.id": {"$exists": True}})]
    report.append(f"work items to write: {len(tickets)}")
    for ticket in tickets:
        account = sites.get(ticket["workspace_id"])
        if apply and account:
            raw = ticket["raw"]
            # old syncs didn't fetch `created`; `updated` keeps a stable board order until the next sync fills it
            raw["fields"].setdefault("created", raw["fields"].get("updated"))
            fields = adapter.normalise({"external_account": account}, ticket["project_key"], raw)
            await work_items.upsert_synced(ticket["workspace_id"], fields)

    if apply:
        await mongo.ensure_indexes()
        if drop_legacy:
            await mongo.jira_projects().drop()
            await mongo.tickets().drop()
            report.append("dropped jira_projects and tickets")
    return report


async def main(apply: bool, drop_legacy: bool) -> None:
    mongo.connect()
    try:
        for line in await migrate(apply, drop_legacy):
            print(("" if apply else "[dry run] ") + line)
    finally:
        mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--drop-legacy", action="store_true", help="with --apply, drop jira_projects and tickets")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.drop_legacy))
