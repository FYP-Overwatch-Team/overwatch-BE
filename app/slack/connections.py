"""Which Slack connections Bearings still acts on: a live workspace, a live connection, and a token that works.

A deleted workspace keeps its connections (deleting is a soft delete), and an app removed in Slack
leaves the connection active with a token that needs reconnecting; the Slack loops skip both, so
nothing is read or posted for them.
"""

from collections.abc import AsyncIterator, Iterable

from app.connectors.repository import ACTIVE
from app.core.constants import ConnectionStatus, Provider
from app.db import mongo
from app.workspaces.repository import LIVE


async def live_workspace_ids(workspace_ids: Iterable[str]) -> set[str]:
    ids = list(workspace_ids)
    if not ids:
        return set()
    return {doc["_id"] async for doc in mongo.workspaces().find({"_id": {"$in": ids}, **LIVE}, {"_id": 1})}


async def serving() -> AsyncIterator[dict]:
    """Every Slack connection that should still be read and posted for."""
    conns = [conn async for conn in mongo.connections().find({"provider": Provider.SLACK, **ACTIVE})]
    live = await live_workspace_ids(conn["workspace_id"] for conn in conns)
    tokens = {
        doc["owner_id"]: doc.get("status")
        async for doc in mongo.oauth_tokens().find({"provider": Provider.SLACK, "owner_id": {"$in": list(live)}}, {"owner_id": 1, "status": 1})
    }
    for conn in conns:
        if conn["workspace_id"] in live and tokens.get(conn["workspace_id"]) == ConnectionStatus.ACTIVE:
            yield conn
