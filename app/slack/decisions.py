"""Finding decisions in the Slack channels a workspace watches.

Every few minutes, the messages waiting in `channel_messages` are grouped into conversations: a
thread (its first message and the replies), or messages outside threads that follow each other with
no gap longer than `WINDOW_GAP_SECONDS`. A conversation is read once it has gone quiet for
`QUIET_SECONDS` and is a real discussion (`MIN_MESSAGES` from at least `MIN_PEOPLE`); one already
read and unchanged since is skipped for free.

The read is the Decisions suggest flow (`app.decisions.suggest`), with a prompt for chat: what it
finds is saved as `proposed`, quotes checked word for word, exactly as for a ticket. A Decisions
source has to stay readable for as long as its decision lives, so the conversation is copied to
`chat_threads` as that source, and the copy is kept only if a decision was found in it; every other
conversation expires with its messages.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import structlog

from app.chat.limits import daily_budget
from app.connectors.repository import ACTIVE
from app.core.config import get_settings
from app.core.constants import EvidenceKind, Provider
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.rate_limit import Window, take_all
from app.core.time import utcnow
from app.db import mongo
from app.decisions import quotes, suggest
from app.decisions.checks import SYSTEM_USER

logger = structlog.get_logger("app.slack")

CHAT_PROMPT = (Path(__file__).parent.parent / "decisions" / "prompts" / "chat.md").read_text(encoding="utf-8")
QUIET_SECONDS = 15 * 60
WINDOW_GAP_SECONDS = 10 * 60
MIN_MESSAGES = 3
MIN_PEOPLE = 2
# a workspace's messages read per scan; they only live a few hours, so this is a guard, not a page size
MAX_MESSAGES = 5000
_IDS = uuid.UUID("6f0c3c0e-3b1a-4f5e-9d47-1a9a5c1d2b77")


@dataclass(frozen=True)
class Message:
    ts: str
    user: str
    text: str
    thread_ts: str | None


@dataclass(frozen=True)
class Conversation:
    channel: str
    messages: tuple[Message, ...]

    @property
    def first_ts(self) -> str:
        return self.messages[0].ts

    def is_ready(self, now: float) -> bool:
        """Quiet long enough, and a discussion rather than one person talking."""
        quiet = now - float(self.messages[-1].ts) >= QUIET_SECONDS
        return quiet and len(self.messages) >= MIN_MESSAGES and len({m.user for m in self.messages}) >= MIN_PEOPLE

    def text(self) -> str:
        return "\n".join(f"{m.user}: {m.text}" for m in self.messages)


def conversations(channel: str, messages: list[Message]) -> list[Conversation]:
    """A channel's messages as conversations: each thread, then runs of messages outside threads."""
    by_root: dict[str, list[Message]] = defaultdict(list)
    for message in sorted(messages, key=lambda m: float(m.ts)):
        by_root[message.thread_ts or message.ts].append(message)
    threads, loose = [], []
    for root, group in by_root.items():
        # a reply whose first message has expired still belongs to its thread
        if len(group) > 1 or group[0].thread_ts:
            threads.append(Conversation(channel, tuple(group)))
        else:
            loose.append(group[0])
    runs: list[list[Message]] = []
    for message in loose:
        if runs and float(message.ts) - float(runs[-1][-1].ts) <= WINDOW_GAP_SECONDS:
            runs[-1].append(message)
        else:
            runs.append([message])
    return threads + [Conversation(channel, tuple(run)) for run in runs]


def thread_id(workspace_id: str, conversation: Conversation) -> str:
    """The same conversation keeps its id as it grows, so a reread is recognised as the same source."""
    return uuid.uuid5(_IDS, f"{workspace_id}:{conversation.channel}:{conversation.first_ts}").hex


def permalink(workspace_url: str | None, channel: str, ts: str) -> str | None:
    """Where the conversation opens in Slack, built from the workspace's address (no API call)."""
    if not workspace_url:
        return None
    return f"{workspace_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"


async def take_scan(workspace_id: str) -> None:
    """One conversation read, against the workspace's daily Slack limit and the AI's daily budget."""
    now = utcnow()
    await take_all([
        Window(
            f"decisions:slack:{workspace_id}:{now.strftime('%Y%m%d')}",
            get_settings().slack_decision_scans_per_day, now + timedelta(days=2),
            AppError("This workspace has used today's Slack decision reads", error_code="rate_limited", status_code=429),
        ),
        daily_budget(now),
    ])


async def run_due() -> None:
    """Read the ready conversations of every workspace that listens to Slack."""
    async for conn in mongo.connections().find({"provider": Provider.SLACK, **ACTIVE}, {"workspace_id": 1, "external_account": 1}):
        try:
            await scan_workspace(conn["workspace_id"], (conn.get("external_account") or {}).get("url"))
        except Exception:
            logger.exception("slack_decision_scan_failed", workspace_id=conn["workspace_id"])


async def scan_workspace(workspace_id: str, workspace_url: str | None = None) -> int:
    """Read this workspace's ready conversations; returns how many decisions were suggested."""
    names = {
        target["key"]: target.get("name") or target["key"]
        async for target in mongo.sync_targets().find({"workspace_id": workspace_id, "provider": Provider.SLACK, **ACTIVE})
    }
    by_channel: dict[str, list[Message]] = defaultdict(list)
    query = {"workspace_id": workspace_id, "channel": {"$in": list(names)}}
    async for doc in mongo.channel_messages().find(query, {"channel": 1, "ts": 1, "user": 1, "text": 1, "thread_ts": 1}).limit(MAX_MESSAGES):
        by_channel[doc["channel"]].append(Message(doc["ts"], doc["user"], doc["text"], doc.get("thread_ts")))

    now = utcnow().timestamp()
    ready = [c for channel, messages in by_channel.items() for c in conversations(channel, messages) if c.is_ready(now)]
    suggested = 0
    for conversation in ready:
        try:
            suggested += await _read(workspace_id, workspace_url, names[conversation.channel], conversation)
        except AppError as exc:
            if exc.error_code in {"rate_limited", "llm_quota_exhausted"}:
                logger.info("slack_decision_scan_limited", workspace_id=workspace_id, error_code=exc.error_code)
                break
            raise
    return suggested


async def _read(workspace_id: str, workspace_url: str | None, channel_name: str, conversation: Conversation) -> int:
    ref_id = thread_id(workspace_id, conversation)
    text = conversation.text()
    scan = await mongo.decision_scans().find_one({"workspace_id": workspace_id, "ref_id": ref_id}, {"fingerprint": 1}) or {}
    if scan.get("fingerprint") == quotes.fingerprint(text):
        return 0  # read before and nothing new was said
    await _keep(workspace_id, ref_id, channel_name, workspace_url, conversation, text)
    try:
        created = await suggest.suggest(
            {"workspace_id": workspace_id, "user_id": SYSTEM_USER}, EvidenceKind.CHAT_THREAD, ref_id,
            system=CHAT_PROMPT, take_limit=take_scan,
        )
    except ConflictError:
        return 0  # another read holds it (and its copy) right now, or it's unchanged
    except ExternalServiceError as exc:
        await _release_if_unused(workspace_id, ref_id)
        logger.warning("slack_decision_read_failed", workspace_id=workspace_id, error_code=exc.error_code)
        return 0  # not marked as read, so it's tried again next time
    except AppError:
        await _release_if_unused(workspace_id, ref_id)
        raise  # out of reads for today: the caller stops
    await _release_if_unused(workspace_id, ref_id)
    logger.info("slack_decisions_suggested", workspace_id=workspace_id, channel=conversation.channel, count=len(created))
    return len(created)


async def _keep(
    workspace_id: str, ref_id: str, channel_name: str, workspace_url: str | None, conversation: Conversation, text: str,
) -> None:
    now = utcnow()
    await mongo.chat_threads().update_one({"_id": ref_id}, {
        "$set": {
            "workspace_id": workspace_id, "provider": Provider.SLACK, "channel": conversation.channel,
            "title": f"Slack {channel_name}", "external_url": permalink(workspace_url, conversation.channel, conversation.first_ts),
            "messages": [{"ts": m.ts, "user": m.user, "text": m.text} for m in conversation.messages],
            "text": text, "updated_at": now,
        },
        "$setOnInsert": {"created_at": now},
    }, upsert=True)


async def _release_if_unused(workspace_id: str, ref_id: str) -> None:
    """Drop the copy unless a decision rests on it: only conversations a decision came from are kept."""
    if not await mongo.decisions().find_one({"workspace_id": workspace_id, "evidence.ref_id": ref_id}, {"_id": 1}):
        await mongo.chat_threads().delete_one({"_id": ref_id})


async def follow_edit(workspace_id: str, channel: str, ts: str, text: str | None) -> None:
    """Carry an edit (new text) or a delete (None) in Slack into the kept conversations that hold the message,
    so a decision's evidence shows it changed or is gone, as it would for an edited ticket."""
    async for thread in mongo.chat_threads().find({"workspace_id": workspace_id, "channel": channel, "messages.ts": ts}):
        messages = [
            {**m, "text": text} if m["ts"] == ts else m
            for m in thread["messages"] if m["ts"] != ts or text is not None
        ]
        if not messages:
            await mongo.chat_threads().delete_one({"_id": thread["_id"]})
            continue
        lines = "\n".join(f"{m['user']}: {m['text']}" for m in messages)
        await mongo.chat_threads().update_one(
            {"_id": thread["_id"]}, {"$set": {"messages": messages, "text": lines, "updated_at": utcnow()}},
        )
