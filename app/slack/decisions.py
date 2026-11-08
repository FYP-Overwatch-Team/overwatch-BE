"""Finding decisions in the Slack channels a workspace watches.

Every few minutes, the messages waiting in `channel_messages` are grouped into conversations: a
thread (its first message and the replies), or messages outside threads that follow each other with
no gap longer than `WINDOW_GAP_SECONDS`. A conversation is read once it has gone quiet for
`QUIET_SECONDS` (or has waited `MAX_WAIT_SECONDS`), is a real discussion (`MIN_MESSAGES`, or `MIN_THREAD_MESSAGES` in a thread, from at
least `MIN_PEOPLE`), and has messages not read yet: each message read is marked with its conversation's id, so a conversation is only read
again when something new is said in it, never because older messages expired.

A conversation keeps one id for its whole life: a thread is named by its first message (which
replies carry even after it expires), and a run of messages by the id its earliest read message was
given. So a reread is recognised as the same source, and what was found in it isn't found twice.

The read is the Decisions suggest flow (`app.decisions.suggest`), with a prompt for chat: what it
finds is saved as `proposed`, quotes checked word for word, exactly as for a ticket. A Decisions
source has to stay readable for as long as its decision lives, so the conversation is copied to
`chat_threads` as that source, and the copy is kept only while a decision or an unsettled question
found in it uses it (`app.decisions.chat_sources`); every other conversation expires with its messages.

The same read lists important questions the conversation raised but didn't settle; they're kept for
the channel's daily digest (`app.slack.questions`, `app.slack.digest`).

When the AI is down or out of quota, the scan stops (for every workspace) and the read it spent is
given back; a conversation whose read keeps failing for other reasons is retried with backoff, then
left alone until something new is said in it.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from pymongo import UpdateMany

from app.chat.limits import daily_budget
from app.connectors.repository import ACTIVE
from app.core.config import get_settings
from app.core.constants import EvidenceKind, Provider
from app.core.exceptions import AppError, ConflictError, ExternalServiceError
from app.core.rate_limit import Window, count, take_all
from app.core.time import utcnow
from app.db import mongo
from app.decisions import chat_sources, quotes, suggest
from app.decisions.checks import SYSTEM_USER
from app.decisions.suggest import Reply
from app.slack import connections, questions

logger = structlog.get_logger("app.slack")

CHAT_PROMPT = (Path(__file__).parent.parent / "decisions" / "prompts" / "chat.md").read_text(encoding="utf-8")
QUIET_SECONDS = 15 * 60
WINDOW_GAP_SECONDS = 10 * 60
MIN_MESSAGES = 3
# a thread is one topic already, so a question and someone's answer are enough
MIN_THREAD_MESSAGES = 2
MIN_PEOPLE = 2
# a conversation that never goes quiet is read anyway, with an hour to spare before its messages expire
MAX_WAIT_SECONDS = 2 * 3600
# background reads stop here, so people asking the AI something always have the rest of the daily budget
SLACK_BUDGET_SHARE = 0.6
# a workspace's messages read per scan, newest first; they only live a few hours, so this is a guard
MAX_MESSAGES = 5000
# the AI is unavailable for everyone: stop until the next tick, and give the read back
OUTAGE_ERRORS = {"llm_quota_exhausted", "llm_unavailable"}
# a read failing for another reason (a reply that can't be read) is retried later, then given up
MAX_FAILURES = 3
RETRY_BASE = timedelta(minutes=10)
_IDS = uuid.UUID("6f0c3c0e-3b1a-4f5e-9d47-1a9a5c1d2b77")


@dataclass(frozen=True)
class Message:
    ts: str
    user: str
    text: str
    thread_ts: str | None
    #: the conversation it was read in, once read
    read_ref: str | None = None


@dataclass(frozen=True)
class Conversation:
    channel: str
    messages: tuple[Message, ...]
    #: what names it for good: a thread's first message, or a run's earliest message
    root: str
    is_thread: bool
    #: the id a run was read under before, which it keeps as it grows and its oldest messages expire
    read_before: str | None = None

    @property
    def first_ts(self) -> str:
        return self.messages[0].ts

    @property
    def is_discussion(self) -> bool:
        min_messages = MIN_THREAD_MESSAGES if self.is_thread else MIN_MESSAGES
        return len(self.messages) >= min_messages and len({m.user for m in self.messages}) >= MIN_PEOPLE

    def _is_quiet(self, now: float) -> bool:
        return now - float(self.messages[-1].ts) >= QUIET_SECONDS

    def is_ready(self, now: float) -> bool:
        """A discussion with something unread, that's gone quiet or has waited as long as it can."""
        unread = [float(m.ts) for m in self.messages if m.read_ref is None]
        if not unread or not self.is_discussion:
            return False
        return self._is_quiet(now) or now - min(unread) >= MAX_WAIT_SECONDS


def settled_without_a_read(conversation: Conversation, now: float) -> bool:
    """Quiet, unread and not a discussion: nothing to read, so it needn't be looked at again."""
    return (
        not conversation.is_discussion
        and conversation._is_quiet(now)
        and any(m.read_ref is None for m in conversation.messages)
    )


def conversations(channel: str, messages: list[Message]) -> list[Conversation]:
    """A channel's messages as conversations: each thread, then runs of messages outside threads."""
    by_root: dict[str, list[Message]] = defaultdict(list)
    for message in sorted(messages, key=lambda m: float(m.ts)):
        by_root[message.thread_ts or message.ts].append(message)
    threads, loose = [], []
    for root, group in by_root.items():
        # a reply whose first message has expired still belongs to its thread
        if len(group) > 1 or group[0].thread_ts:
            threads.append(Conversation(channel, tuple(group), root, is_thread=True))
        else:
            loose.append(group[0])
    runs: list[list[Message]] = []
    for message in loose:
        if runs and float(message.ts) - float(runs[-1][-1].ts) <= WINDOW_GAP_SECONDS:
            runs[-1].append(message)
        else:
            runs.append([message])
    found = [
        Conversation(channel, tuple(run), run[0].ts, is_thread=False, read_before=next((m.read_ref for m in run if m.read_ref), None))
        for run in runs
    ]
    return threads + found


def thread_id(workspace_id: str, conversation: Conversation) -> str:
    """The conversation's id for good: the one it was read under before, or one made from its root."""
    return conversation.read_before or uuid.uuid5(_IDS, f"{workspace_id}:{conversation.channel}:{conversation.root}").hex


def permalink(workspace_url: str | None, channel: str, ts: str) -> str | None:
    """Where the conversation opens in Slack, built from the workspace's address (no API call)."""
    if not workspace_url:
        return None
    return f"{workspace_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"


def _scan_windows(workspace_id: str, now: datetime) -> list[Window]:
    budget = daily_budget(now)
    return [
        Window(
            f"decisions:slack:{workspace_id}:{now.strftime('%Y%m%d')}",
            get_settings().slack_decision_scans_per_day, now + timedelta(days=2),
            AppError("This workspace has used today's Slack decision reads", error_code="rate_limited", status_code=429),
        ),
        budget._replace(limit=int(budget.limit * SLACK_BUDGET_SHARE)),
    ]


async def _at_limit(workspace_id: str) -> bool:
    """Whether this workspace has no reads left today; a spent AI budget stops every workspace."""
    workspace_reads, budget = _scan_windows(workspace_id, utcnow())
    if await count(budget.key) >= budget.limit:
        raise AIUnavailable
    return await count(workspace_reads.key) >= workspace_reads.limit


async def take_scan(workspace_id: str, _user_id: str) -> None:
    """One conversation read, against the workspace's daily Slack limit and Slack's share of the AI's daily budget.

    Not an action from the monthly allowance: nobody chose to spend it.
    """
    await take_all(_scan_windows(workspace_id, utcnow()))


async def _give_back_scan(workspace_id: str) -> None:
    """A read the AI never answered doesn't count against either limit."""
    keys = [window.key for window in _scan_windows(workspace_id, utcnow())]
    await mongo.usage_counters().update_many({"_id": {"$in": keys}, "count": {"$gt": 0}}, {"$inc": {"count": -1}})


class AIUnavailable(Exception):
    """The model can't be reached or is out of quota: nothing more is read until the next tick."""


async def run_due() -> None:
    """Read the ready conversations of every workspace that listens to Slack."""
    async for conn in connections.serving():
        try:
            await scan_workspace(conn["workspace_id"], (conn.get("external_account") or {}).get("url"))
        except AIUnavailable:
            logger.info("slack_decision_scans_paused")
            return
        except Exception:
            logger.exception("slack_decision_scan_failed", workspace_id=conn["workspace_id"])


async def scan_workspace(workspace_id: str, workspace_url: str | None = None) -> int:
    """Read this workspace's ready conversations; returns how many decisions were suggested.

    Raises `AIUnavailable` when the model is down, so the caller can stop every workspace.
    """
    if await _at_limit(workspace_id):
        return 0
    names = {
        target["key"]: target.get("name") or target["key"]
        async for target in mongo.sync_targets().find({"workspace_id": workspace_id, "provider": Provider.SLACK, **ACTIVE})
    }
    query = {"workspace_id": workspace_id, "channel": {"$in": list(names)}}
    # a conversation can only be ready, or be settled, with an unread message at least this old
    oldest_allowed = f"{utcnow().timestamp() - QUIET_SECONDS:.6f}"
    if not await mongo.channel_messages().find_one({**query, "read_ref": None, "ts": {"$lte": oldest_allowed}}, {"_id": 1}):
        return 0
    by_channel: dict[str, list[Message]] = defaultdict(list)
    fields = {"channel": 1, "ts": 1, "user": 1, "text": 1, "thread_ts": 1, "read_ref": 1}
    async for doc in mongo.channel_messages().find(query, fields).sort("ts", -1).limit(MAX_MESSAGES):
        by_channel[doc["channel"]].append(Message(doc["ts"], doc["user"], doc["text"], doc.get("thread_ts"), doc.get("read_ref")))

    now = utcnow()
    found = [c for channel, messages in by_channel.items() for c in conversations(channel, messages)]
    await _mark_settled(workspace_id, [c for c in found if settled_without_a_read(c, now.timestamp())])
    ready = [c for c in found if c.is_ready(now.timestamp())]
    if not ready:
        return 0
    by_ref = {thread_id(workspace_id, c): c for c in ready}
    # one lookup for every ready conversation: what was read before, and what's waiting to be retried
    scans = {
        doc["ref_id"]: doc
        async for doc in mongo.decision_scans().find(
            {"workspace_id": workspace_id, "ref_id": {"$in": list(by_ref)}},
            {"ref_id": 1, "fingerprint": 1, "retry_after": 1, "failures": 1},
        )
    }
    suggested = 0
    for ref_id, conversation in by_ref.items():
        scan = scans.get(ref_id, {})
        if scan.get("retry_after") and scan["retry_after"] > now:
            continue
        try:
            suggested += await _read(workspace_id, workspace_url, names[conversation.channel], conversation, ref_id, scan)
        except AppError as exc:
            if exc.error_code == "rate_limited":
                logger.info("slack_decision_scan_limited", workspace_id=workspace_id)
                break
            if exc.error_code in OUTAGE_ERRORS:
                # the AI's daily budget is spent, for every workspace: the same pause as an outage
                raise AIUnavailable from exc
            raise
    return suggested


async def _read(
    workspace_id: str, workspace_url: str | None, channel_name: str, conversation: Conversation, ref_id: str, scan: dict,
) -> int:
    messages, text = await _with_kept(workspace_id, ref_id, conversation)
    if scan.get("fingerprint") == quotes.fingerprint(text):
        await _mark_read(workspace_id, conversation, ref_id)  # read before and nothing new was said
        return 0
    external_url = permalink(workspace_url, conversation.channel, conversation.root)
    await _keep(workspace_id, ref_id, channel_name, external_url, conversation.channel, messages, text)
    # asked before this read: a decision it finds settles these, never the questions it finds itself
    asked = await questions.open_in(workspace_id, ref_id)

    async def keep_questions(reply: Reply, source_text: str) -> None:
        await questions.keep_from_reply(workspace_id, conversation.channel, ref_id, external_url, reply, source_text)

    try:
        created = await suggest.suggest(
            {"workspace_id": workspace_id, "user_id": SYSTEM_USER}, EvidenceKind.CHAT_THREAD, ref_id,
            system=CHAT_PROMPT, take_limit=take_scan, on_reply=keep_questions,
        )
    except ConflictError:
        return 0  # another read holds it right now, or it's unchanged
    except ExternalServiceError as exc:
        await _give_back_scan(workspace_id)
        if exc.error_code in OUTAGE_ERRORS:
            raise AIUnavailable from exc
        await _failed(workspace_id, conversation, ref_id, scan, exc.error_code)
        return 0
    finally:
        # whatever happened (a failure, a deploy), the copy stays only if something now rests on it
        await chat_sources.release_if_unused(workspace_id, ref_id)
    if created:
        # a question asked earlier in this conversation has now been answered by a decision
        await questions.settle_conversation(workspace_id, ref_id, asked)
    await _mark_read(workspace_id, conversation, ref_id)
    logger.info("slack_decisions_suggested", workspace_id=workspace_id, channel=conversation.channel, count=len(created))
    return len(created)


async def _mark_settled(workspace_id: str, settled: list[Conversation]) -> None:
    if not settled:
        return
    await mongo.channel_messages().bulk_write([
        UpdateMany(
            {"workspace_id": workspace_id, "channel": c.channel, "ts": {"$in": [m.ts for m in c.messages]}},
            {"$set": {"read_ref": thread_id(workspace_id, c)}},
        )
        for c in settled
    ], ordered=False)


async def _mark_read(workspace_id: str, conversation: Conversation, ref_id: str) -> None:
    await mongo.channel_messages().update_many(
        {"workspace_id": workspace_id, "channel": conversation.channel, "ts": {"$in": [m.ts for m in conversation.messages]}},
        {"$set": {"read_ref": ref_id}},
    )


async def _failed(workspace_id: str, conversation: Conversation, ref_id: str, scan: dict, error_code: str) -> None:
    failures = scan.get("failures", 0) + 1
    logger.warning("slack_decision_read_failed", workspace_id=workspace_id, error_code=error_code, failures=failures)
    if failures >= MAX_FAILURES:
        # given up: only something new said in it brings it back
        await _mark_read(workspace_id, conversation, ref_id)
        update = {"failures": 0, "retry_after": None}
    else:
        update = {"failures": failures, "retry_after": utcnow() + RETRY_BASE * (2 ** (failures - 1))}
    await mongo.decision_scans().update_one({"workspace_id": workspace_id, "ref_id": ref_id}, {"$set": update}, upsert=True)


async def _with_kept(workspace_id: str, ref_id: str, conversation: Conversation) -> tuple[list[dict], str]:
    """The conversation with the messages its kept copy still holds, oldest first, and its text.

    A late reply to an old thread arrives after its first messages expired: what a decision found earlier
    quotes lives on only in the copy, so the copy is added to, never replaced. Edits and deletes reach the
    copy through `follow_edit`.
    """
    kept = await mongo.chat_threads().find_one({"_id": ref_id, "workspace_id": workspace_id}, {"messages": 1}) or {}
    merged = {m["ts"]: m for m in kept.get("messages", [])}
    merged |= {m.ts: {"ts": m.ts, "user": m.user, "text": m.text} for m in conversation.messages}
    messages = sorted(merged.values(), key=lambda m: float(m["ts"]))
    return messages, "\n".join(f"{m['user']}: {m['text']}" for m in messages)


async def _keep(
    workspace_id: str, ref_id: str, channel_name: str, external_url: str | None, channel: str, messages: list[dict], text: str,
) -> None:
    now = utcnow()
    await mongo.chat_threads().update_one({"_id": ref_id}, {
        "$set": {
            "workspace_id": workspace_id, "provider": Provider.SLACK, "channel": channel,
            "title": f"Slack {channel_name}", "external_url": external_url,
            "messages": messages, "text": text, "updated_at": now,
        },
        "$setOnInsert": {"created_at": now},
    }, upsert=True)


async def follow_edit(workspace_id: str, channel: str, ts: str, text: str | None) -> None:
    """Carry an edit (new text) or a delete (None) in Slack into the kept conversations that hold the message,
    so a decision's evidence shows it changed or is gone, as it would for an edited ticket, and a question
    its message no longer raises is withdrawn rather than posted."""
    async for thread in mongo.chat_threads().find({"workspace_id": workspace_id, "channel": channel, "messages.ts": ts}):
        messages = [
            {**m, "text": text} if m["ts"] == ts else m
            for m in thread["messages"] if m["ts"] != ts or text is not None
        ]
        if not messages:
            await mongo.chat_threads().delete_one({"_id": thread["_id"]})
            await questions.withdraw_unquoted(workspace_id, thread["_id"], None)
            continue
        lines = "\n".join(f"{m['user']}: {m['text']}" for m in messages)
        await mongo.chat_threads().update_one(
            {"_id": thread["_id"]}, {"$set": {"messages": messages, "text": lines, "updated_at": utcnow()}},
        )
        await questions.withdraw_unquoted(workspace_id, thread["_id"], lines)
