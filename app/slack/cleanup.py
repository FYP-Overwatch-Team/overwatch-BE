"""Forgetting what a workspace holds from Slack, when it disconnects or the app is uninstalled there.

Disconnecting in Bearings keeps only the conversations a decision quotes (a decision's evidence is
the team's own record); removing the app in Slack drops those too, and the evidence then shows its
source is gone.
"""

from app.core.constants import Provider
from app.db import mongo
from app.decisions import chat_sources
from app.slack import messages


async def forget_workspace(workspace_id: str, *, keep_decision_sources: bool) -> None:
    await messages.delete_workspace(workspace_id)
    await mongo.chat_questions().delete_many({"workspace_id": workspace_id, "provider": Provider.SLACK})
    threads = {"workspace_id": workspace_id, "provider": Provider.SLACK}
    if not keep_decision_sources:
        await mongo.chat_threads().delete_many(threads)
        return
    async for thread in mongo.chat_threads().find(threads, {"_id": 1}):
        await chat_sources.release_if_unused(workspace_id, thread["_id"])


async def forget_on_disconnect(workspace_id: str) -> None:
    await forget_workspace(workspace_id, keep_decision_sources=True)
