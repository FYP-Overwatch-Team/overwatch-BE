"""Messages from the Slack channels a workspace watches: held in memory, written to Mongo in batches.

Every message posted in a watched channel arrives as its own event, so writing each one would
cost a database round trip per message. Instead they wait here and go out as one bulk write
every `FLUSH_SECONDS` (sooner once `FLUSH_AT` are waiting), and on shutdown. A crash loses at
most what hasn't been written yet: Slack doesn't redeliver an event we already acknowledged.

A message is keyed by (workspace, channel, ts), so a redelivered event or a repeated edit writes
the same document again: no separate deduplication is needed. Mongo deletes the text after
`mongo.CHANNEL_MESSAGE_TTL_SECONDS`; only what's needed to find decisions is kept (author id,
text, thread), never files.
"""

import asyncio
from dataclasses import dataclass

from pymongo import DeleteOne, UpdateOne

from app.core.constants import Provider
from app.core.time import utcnow
from app.db import mongo

FLUSH_SECONDS = 30
FLUSH_AT = 500
# Slack allows 40,000 characters; past this a message is long enough to read for decisions
MAX_TEXT_CHARS = 8000
# posted by a person, with text: a thread reply sent to the channel too, or a message with files
KEPT_SUBTYPES = {None, "thread_broadcast", "file_share"}


@dataclass(frozen=True)
class Change:
    """What one Slack message event does to a stored message: `fields` is None when it was deleted."""

    ts: str
    fields: dict | None


def change_from(event: dict) -> Change | None:
    """The stored change a `message` event makes, or None for one that isn't kept (bots, joins, empty)."""
    if event.get("type") != "message":
        return None
    subtype = event.get("subtype")
    if subtype == "message_deleted":
        return Change(event["deleted_ts"], None) if event.get("deleted_ts") else None
    if subtype == "message_changed":
        return _posted(event.get("message") or {})
    return _posted(event) if subtype in KEPT_SUBTYPES else None


def _posted(message: dict) -> Change | None:
    text = (message.get("text") or "").strip()
    if not text or not message.get("user") or message.get("bot_id") or not message.get("ts"):
        return None
    return Change(message["ts"], {
        "user": message["user"],
        "text": text[:MAX_TEXT_CHARS],
        # a reply names its thread's first message; a top-level message has none
        "thread_ts": message.get("thread_ts") if message.get("thread_ts") != message["ts"] else None,
    })


@dataclass(frozen=True)
class _Key:
    workspace_id: str
    channel: str
    ts: str


class MessageBuffer:
    """Pending writes by message: the latest change to each one wins."""

    def __init__(self) -> None:
        self._pending: dict[_Key, dict | None] = {}
        self._due = asyncio.Event()
        self._writing = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._pending)

    def apply(self, workspace_id: str, team_id: str, channel: str, change: Change) -> None:
        fields = change.fields and {**change.fields, "team_id": team_id, "provider": Provider.SLACK}
        self._pending[_Key(workspace_id, channel, change.ts)] = fields
        if len(self._pending) >= FLUSH_AT:
            self._due.set()

    def drop_workspace(self, workspace_id: str) -> None:
        """Forget what a workspace has waiting, e.g. once its Slack install is gone."""
        self._pending = {key: fields for key, fields in self._pending.items() if key.workspace_id != workspace_id}

    async def wait_until_due(self, seconds: float) -> None:
        """Return after `seconds`, or as soon as enough messages are waiting."""
        try:
            await asyncio.wait_for(self._due.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def flush(self) -> int:
        """Write everything waiting in one bulk write; returns how many messages it wrote.

        On failure the batch is kept (behind anything newer), so the next flush retries it.
        """
        async with self._writing:
            batch, self._pending = self._pending, {}
            self._due.clear()
            if not batch:
                return 0
            try:
                await mongo.channel_messages().bulk_write([_operation(key, fields) for key, fields in batch.items()], ordered=False)
            except Exception:
                self._pending = {**batch, **self._pending}
                raise
            return len(batch)


def _operation(key: _Key, fields: dict | None) -> UpdateOne | DeleteOne:
    match = {"workspace_id": key.workspace_id, "channel": key.channel, "ts": key.ts}
    if fields is None:
        return DeleteOne(match)
    # the expiry clock starts when a message is first seen: an edit doesn't keep the text longer
    return UpdateOne(match, {"$set": fields, "$setOnInsert": {"received_at": utcnow()}}, upsert=True)


async def delete_workspace(workspace_id: str) -> None:
    """Remove a workspace's chat text, waiting and stored."""
    buffer.drop_workspace(workspace_id)
    await mongo.channel_messages().delete_many({"workspace_id": workspace_id})


buffer = MessageBuffer()
