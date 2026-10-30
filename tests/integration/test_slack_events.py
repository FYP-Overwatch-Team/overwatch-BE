"""Slack events: signed requests in, messages from watched channels buffered and written in batches."""

import hashlib
import hmac
import json
import time

import pytest

from app.connectors import repository as connections
from app.core.config import get_settings
from app.db import mongo, oauth_tokens
from app.slack import events, messages

SECRET = "test-slack-signing-secret"
TEAM = "T0ACME"
WATCHED, UNWATCHED = "C0GENERAL", "C0RANDOM"
WORKSPACE = "ws-1"


@pytest.fixture(autouse=True)
async def slack(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    get_settings.cache_clear()
    monkeypatch.setattr(messages, "buffer", messages.MessageBuffer())
    events.forget_listeners()
    await connections.save_connection(
        WORKSPACE, "slack", auth_backend="native_oauth", scopes=[], connected_by="u-1",
        external_account={"id": TEAM, "name": "Acme", "url": None},
    )
    await connections.add_target(WORKSPACE, "slack", WATCHED, "#general")
    await oauth_tokens.save(WORKSPACE, "slack", {"access_token": "xoxb-test"})
    yield
    events.forget_listeners()


def signed(body: dict, *, secret: str = SECRET, at: int | None = None) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    timestamp = str(at if at is not None else int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": f"v0={digest}", "Content-Type": "application/json"}


async def send(client, event: dict, *, team: str = TEAM, **signing):
    raw, headers = signed({"type": "event_callback", "team_id": team, "event_id": "Ev1", "event": event}, **signing)
    return await client.post("/webhooks/slack/events", content=raw, headers=headers)


def message(text: str, ts: str = "1700000000.000100", channel: str = WATCHED, **extra) -> dict:
    return {"type": "message", "channel": channel, "user": "U0ALICE", "text": text, "ts": ts, **extra}


async def stored() -> list[dict]:
    return [doc async for doc in mongo.channel_messages().find({}, {"_id": 0}).sort("ts", 1)]


async def test_answers_slacks_url_check(client):
    raw, headers = signed({"type": "url_verification", "challenge": "abc123"})

    resp = await client.post("/webhooks/slack/events", content=raw, headers=headers)

    assert (resp.status_code, resp.text) == (200, "abc123")


@pytest.mark.parametrize("signing", [{"secret": "someone-else"}, {"at": int(time.time()) - 301}])
async def test_rejects_unsigned_and_replayed_requests(client, signing):
    resp = await send(client, message("we decided"), **signing)

    assert (resp.status_code, resp.json()["meta"]["error_code"]) == (401, "slack_bad_signature")
    assert len(messages.buffer) == 0


def test_without_a_signing_secret_nothing_is_trusted(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    get_settings.cache_clear()
    raw, headers = signed({"type": "event_callback"})

    assert not events.verify_signature(raw, headers["X-Slack-Request-Timestamp"], headers["X-Slack-Signature"])


async def test_messages_wait_in_memory_and_are_written_once(client):
    reply = message("Postgres it is", ts="1700000000.000200", thread_ts="1700000000.000100")

    for event in (message("Postgres or Mongo?"), reply, reply):  # Slack redelivers
        assert (await send(client, event)).status_code == 200
    assert await stored() == []

    written = await messages.buffer.flush()

    docs = await stored()
    assert written == 2
    assert [(d["ts"], d["text"], d["thread_ts"], d["user"]) for d in docs] == [
        ("1700000000.000100", "Postgres or Mongo?", None, "U0ALICE"),
        ("1700000000.000200", "Postgres it is", "1700000000.000100", "U0ALICE"),
    ]
    assert {(d["workspace_id"], d["channel"], d["team_id"], d["provider"]) for d in docs} == {(WORKSPACE, WATCHED, TEAM, "slack")}
    assert all(d["received_at"] for d in docs)


async def test_only_peoples_messages_in_watched_channels_are_kept(client):
    for event in (
        message("noise", channel=UNWATCHED),
        message("deploy done", bot_id="B0BOT"),
        message("<@U0ALICE> has joined the channel", subtype="channel_join"),
        message("   "),
        {"type": "reaction_added", "item": {"channel": WATCHED}},
    ):
        await send(client, event)
    await send(client, message("from another Slack workspace"), team="T0OTHER")

    await messages.buffer.flush()

    assert await stored() == []


async def test_edits_and_deletes_follow_the_message(client):
    await send(client, message("use Mongo"))
    await messages.buffer.flush()

    await send(client, {
        "type": "message", "subtype": "message_changed", "channel": WATCHED,
        "message": {"user": "U0ALICE", "text": "use Postgres", "ts": "1700000000.000100"},
    })
    await messages.buffer.flush()
    edited = await stored()
    await send(client, {"type": "message", "subtype": "message_deleted", "channel": WATCHED, "deleted_ts": "1700000000.000100"})
    await messages.buffer.flush()

    assert [d["text"] for d in edited] == ["use Postgres"]
    assert await stored() == []


async def test_uninstalling_asks_to_reconnect_and_drops_the_chat_text(client):
    await send(client, message("written"))
    await messages.buffer.flush()
    await send(client, message("waiting", ts="1700000000.000300"))

    resp = await send(client, {"type": "app_uninstalled"})
    await messages.buffer.flush()

    assert resp.status_code == 200
    assert await stored() == []
    assert (await oauth_tokens.get(WORKSPACE, "slack"))["status"] == "needs_reauth"


async def test_a_failed_write_keeps_the_batch_for_the_next_flush(client, monkeypatch):
    await send(client, message("kept"))
    real = mongo.channel_messages

    class Down:
        async def bulk_write(self, *args, **kwargs):
            raise RuntimeError("mongo down")

    monkeypatch.setattr(mongo, "channel_messages", lambda: Down())
    with pytest.raises(RuntimeError):
        await messages.buffer.flush()
    monkeypatch.setattr(mongo, "channel_messages", real)

    assert await messages.buffer.flush() == 1
    assert [d["text"] for d in await stored()] == ["kept"]


async def test_chat_text_expires_and_is_stored_once_per_message():
    indexes = await mongo.channel_messages().index_information()

    assert indexes["received_at_1"]["expireAfterSeconds"] == 3 * 3600
    assert indexes["workspace_id_1_channel_1_ts_1"]["unique"]
