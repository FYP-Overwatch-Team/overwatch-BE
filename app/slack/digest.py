"""The daily digest: once a day, at the time a workspace picks, the bot posts in each watched channel
the questions raised there but not settled (with buttons to track or settle them), and the decisions
found there since the last digest, waiting for review in Bearings.

A workspace's digest is claimed for the day with one conditional update before anything is posted,
so a restart or a second process never posts it twice; a crash after the claim skips that day
rather than repeating it. Questions beyond a digest's room wait for the next one, until they expire.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from app.connectors import registry
from app.connectors.repository import ACTIVE
from app.core.config import get_settings
from app.core.constants import ChatQuestionStatus, DecisionStatus, Provider
from app.core.time import utcnow
from app.db import mongo
from app.slack import questions

logger = structlog.get_logger("app.slack")

DEFAULT_TIME = "10:00"
MAX_QUESTIONS = 5
MAX_DECISIONS = 5
# a first digest looks back this far for decisions
FIRST_LOOKBACK = timedelta(days=1)
# Slack caps a block's text at 3000 characters; a question or title is far shorter, this is a guard
MAX_LINE_CHARS = 300

CREATE_ACTION = "question_create"
SETTLE_ACTION = "question_settle"


def settings_of(conn: dict) -> dict:
    """The workspace's digest settings, defaulted: on, at 10:00, in UTC until an admin's time zone is saved."""
    stored = conn.get("digest") or {}
    return {
        "enabled": stored.get("enabled", True),
        "time": stored.get("time", DEFAULT_TIME),
        "time_zone": stored.get("time_zone"),
    }


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def due_day(conn: dict, now: datetime) -> str | None:
    """Today's date where the workspace is, if its digest time has passed and it hasn't been sent today."""
    settings = settings_of(conn)
    if not settings["enabled"]:
        return None
    local = now.astimezone(zone(settings["time_zone"]))
    today = local.date().isoformat()
    if local.strftime("%H:%M") < settings["time"] or conn.get("digest_sent_on") == today:
        return None
    return today


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 1] + "…"


def _escape(text: str) -> str:
    """Slack's mrkdwn treats &, < and > as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def question_block_id(question_id: str) -> str:
    return f"q:{question_id}"


def blocks(channel_name: str, open_questions: list[dict], found: list[dict], frontend_origin: str) -> list[dict]:
    """The digest message: unsettled questions, each with its buttons, then the decisions found."""
    out: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": f":clipboard: *Bearings: what came up in {_escape(channel_name)}*"}}]
    if open_questions:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Still unsettled*"}})
    for question in open_questions:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f":grey_question: {_escape(_clip(question['question']))}"}})
        buttons = [
            {"type": "button", "action_id": CREATE_ACTION, "value": question["_id"], "style": "primary",
             "text": {"type": "plain_text", "text": "Create a decision"}},
            {"type": "button", "action_id": SETTLE_ACTION, "value": question["_id"],
             "text": {"type": "plain_text", "text": "We'll settle it here"}},
        ]
        if question.get("external_url"):
            buttons.append({"type": "button", "action_id": "question_open", "url": question["external_url"],
                            "text": {"type": "plain_text", "text": "Open thread"}})
        out.append({"type": "actions", "block_id": question_block_id(question["_id"]), "elements": buttons})
    if found:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Decisions found* (waiting for review in Bearings)"}})
    for decision in found:
        link = f"{frontend_origin.rstrip('/')}/decisions/{decision['_id']}"
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f":pushpin: <{link}|{_escape(_clip(decision['title']))}>"}})
    return out


async def run_due() -> None:
    """Expire overdue questions, then post every workspace's digest whose time has come."""
    try:
        await questions.expire_due()
    except Exception:
        logger.exception("slack_questions_expiry_failed")
    now = utcnow()
    async for conn in mongo.connections().find({"provider": Provider.SLACK, **ACTIVE}):
        today = due_day(conn, now)
        if today is None:
            continue
        try:
            await send(conn, today, now)
        except Exception:
            logger.exception("slack_digest_failed", workspace_id=conn["workspace_id"])


async def send(conn: dict, today: str, now: datetime) -> int:
    """Claim today's digest for the workspace and post it in each watched channel; returns messages posted."""
    since = conn.get("digest_last_at") or now - FIRST_LOOKBACK
    claimed = await mongo.connections().update_one(
        {"_id": conn["_id"], "digest_sent_on": {"$ne": today}},
        {"$set": {"digest_sent_on": today, "digest_last_at": now}},
    )
    if claimed.modified_count == 0:
        return 0  # another run got here first
    workspace_id = conn["workspace_id"]
    connector = registry.get(Provider.SLACK)
    auth = registry.auth_for(conn)
    posted = 0
    async for target in mongo.sync_targets().find({"workspace_id": workspace_id, "provider": Provider.SLACK, **ACTIVE}):
        channel, name = target["key"], target.get("name") or target["key"]
        waiting = await _waiting_questions(workspace_id, channel, now)
        found = await _found_decisions(workspace_id, channel, since)
        if not waiting and not found:
            continue
        text = f"Bearings: {len(waiting)} unsettled question(s) and {len(found)} decision(s) found in {name}"
        try:
            ts = await connector.adapter.post_message(auth, conn, channel, text, blocks(name, waiting, found, get_settings().frontend_origin))
        except Exception:
            logger.exception("slack_digest_post_failed", workspace_id=workspace_id, channel=channel)
            continue
        if waiting:
            await mongo.chat_questions().update_many(
                {"_id": {"$in": [q["_id"] for q in waiting]}, "status": ChatQuestionStatus.PENDING},
                {"$set": {"status": ChatQuestionStatus.POSTED, "posted": {"channel": channel, "ts": ts, "at": now}, "updated_at": now}},
            )
        posted += 1
    logger.info("slack_digest_sent", workspace_id=workspace_id, day=today, messages=posted)
    return posted


async def _waiting_questions(workspace_id: str, channel: str, now: datetime) -> list[dict]:
    query = {"workspace_id": workspace_id, "channel": channel, "status": ChatQuestionStatus.PENDING, "expires_at": {"$gt": now}}
    cursor = mongo.chat_questions().find(query, {"question": 1, "external_url": 1}).sort("created_at", 1).limit(MAX_QUESTIONS)
    return [doc async for doc in cursor]


async def _found_decisions(workspace_id: str, channel: str, since: datetime) -> list[dict]:
    """Suggestions found in this channel's conversations since the last digest that are still waiting for review."""
    threads = [doc["_id"] async for doc in mongo.chat_threads().find({"workspace_id": workspace_id, "channel": channel}, {"_id": 1})]
    if not threads:
        return []
    query = {
        "workspace_id": workspace_id, "evidence.ref_id": {"$in": threads},
        "status": DecisionStatus.PROPOSED, "created_at": {"$gte": since},
    }
    cursor = mongo.decisions().find(query, {"title": 1}).sort("created_at", 1).limit(MAX_DECISIONS)
    return [doc async for doc in cursor]


async def update_settings(workspace_id: str, changes: dict) -> dict:
    """Change the workspace's digest settings; returns them all."""
    fields = {f"digest.{key}": value for key, value in changes.items()}
    if fields:
        await mongo.connections().update_one({"workspace_id": workspace_id, "provider": Provider.SLACK}, {"$set": fields})
    conn = await mongo.connections().find_one({"workspace_id": workspace_id, "provider": Provider.SLACK}) or {}
    return settings_of(conn)
