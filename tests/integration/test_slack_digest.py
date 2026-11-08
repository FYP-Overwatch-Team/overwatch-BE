"""Unsettled questions from Slack: kept from the chat read, listed in a daily digest, acted on from its buttons."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
import respx
from httpx import Response

from app.connectors import repository as connections
from app.core.config import get_settings
from app.core.time import utcnow
from app.db import mongo, oauth_tokens
from app.slack import decisions as slack_decisions
from app.slack import digest, questions
from tests.integration.test_slack_decisions import CHANNEL, post, ts
from tests.integration.test_work_items_api import signed_in

SECRET = "test-slack-signing-secret"
TEAM = "T0ACME"
EMAIL = "octo@example.com"
POST_URL = "https://slack.com/api/chat.postMessage"
USERS_URL = "https://slack.com/api/users.info"
RESPONSE_URL = "https://hooks.slack.com/actions/T0ACME/1/abc"

UNSETTLED = [
    ("U0ALICE", "Should billing use Postgres or Mongo?", None),
    ("U0BOB", "not sure, both could work", 0),
    ("U0ALICE", "let's think about it", 0),
]


def reply(decisions=(), open_questions=()) -> str:
    return json.dumps({
        "decisions": [{"title": t, "statement": f"{t}.", "quotes": [{"source": "S1", "text": q}]} for t, q in decisions],
        "questions": [{"question": q, "quote": quote_} for q, quote_ in open_questions],
    })


@pytest.fixture
async def workspace(client, monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    get_settings.cache_clear()
    headers = await signed_in(client)
    workspace_id = headers["X-Workspace-Id"]
    await mongo.users().update_one({"_id": workspace_id}, {"$set": {"email": EMAIL}})
    await connections.save_connection(
        workspace_id, "slack", auth_backend="native_oauth", scopes=[], connected_by=workspace_id,
        external_account={"id": TEAM, "name": "Acme", "url": "https://acme.slack.com/"},
    )
    await connections.add_target(workspace_id, "slack", CHANNEL, "#general")
    await oauth_tokens.save(workspace_id, "slack", {"access_token": "xoxb-test"})
    return headers


async def answer_in_thread(workspace_id: str, user: str, text: str) -> None:
    """A later reply in the thread UNSETTLED started (its first message is at offset 0)."""
    await mongo.channel_messages().insert_one({
        "workspace_id": workspace_id, "channel": CHANNEL, "ts": ts(3), "user": user, "text": text,
        "thread_ts": ts(0), "received_at": utcnow(),
    })


async def read_unsettled(fake_llm, workspace_id: str, found=(("Postgres or Mongo for billing?", "Should billing use Postgres or Mongo?"),)):
    await post(workspace_id, UNSETTLED)
    fake_llm.stream_response = reply(open_questions=found)
    await slack_decisions.scan_workspace(workspace_id, "https://acme.slack.com/")


async def send_digest(workspace_id: str, runs: int = 1) -> respx.Route:
    """Send today's digest; with `runs` > 1, that many runs race from the same read of the connection."""
    conn = await mongo.connections().find_one({"workspace_id": workspace_id, "provider": "slack"})
    with respx.mock as router:
        route = router.post(POST_URL).mock(return_value=Response(200, json={"ok": True, "ts": "1800000000.000100"}))
        for _ in range(runs):
            await digest.send(conn, "2026-11-07", utcnow())
    return route


def click(action_id: str, question_id: str, *, secret: str = SECRET, user: str = "U0ALICE") -> tuple[bytes, dict]:
    payload = {
        "type": "block_actions", "team": {"id": TEAM}, "user": {"id": user}, "response_url": RESPONSE_URL,
        "actions": [{"action_id": action_id, "value": question_id}],
        "message": {"text": "digest", "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "header"}},
            {"type": "actions", "block_id": digest.question_block_id(question_id), "elements": []},
        ]},
    }
    raw = f"payload={quote(json.dumps(payload))}".encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": f"v0={signature}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def press(client, action_id: str, question_id: str, *, email: str | None = EMAIL, **signing) -> tuple[Response, dict | None]:
    raw, headers = click(action_id, question_id, **signing)
    with respx.mock as router:
        router.get(USERS_URL).mock(return_value=Response(200, json={"ok": True, "user": {"id": "U0ALICE", "profile": {"email": email}}}))
        answer = router.post(RESPONSE_URL).mock(return_value=Response(200))
        resp = await client.post("/webhooks/slack/interactions", content=raw, headers=headers)
    return resp, json.loads(answer.calls[0].request.content) if answer.calls else None


async def only_question() -> dict:
    [question] = [doc async for doc in mongo.chat_questions().find({})]
    return question


async def test_an_unsettled_question_is_kept_once_and_made_up_ones_are_dropped(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id, found=(
        ("Postgres or Mongo for billing?", "Should billing use Postgres or Mongo?"),
        ("Invented?", "we should rewrite everything in Rust"),
    ))
    await answer_in_thread(workspace_id, "U0BOB", "still unsure")  # the thread grows, and is read again
    fake_llm.stream_response = reply(open_questions=(("Postgres or Mongo?", "Should billing use Postgres or Mongo?"),))
    await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == 2
    question = await only_question()
    assert (question["question"], question["status"], question["channel"]) == ("Postgres or Mongo for billing?", "pending", CHANNEL)
    assert question["expires_at"] - question["created_at"] == timedelta(days=3)
    # the conversation is kept for the question, though no decision came from it
    assert await mongo.chat_threads().count_documents({"_id": question["thread_ref"]}) == 1


async def test_a_later_decision_in_the_thread_settles_its_question(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    await answer_in_thread(workspace_id, "U0BOB", "OK, Postgres it is")
    fake_llm.stream_response = reply(decisions=(("Billing uses Postgres", "OK, Postgres it is"),))

    await slack_decisions.scan_workspace(workspace_id)

    assert (await only_question())["status"] == "settled"


async def test_a_question_found_with_a_decision_stays_open(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, UNSETTLED)
    fake_llm.stream_response = reply(
        decisions=(("Think about billing", "let's think about it"),),
        open_questions=(("Postgres or Mongo for billing?", "Should billing use Postgres or Mongo?"),),
    )

    await slack_decisions.scan_workspace(workspace_id, "https://acme.slack.com/")

    assert (await only_question())["status"] == "pending"



async def test_a_question_that_cant_be_tracked_leaves_the_threads_other_questions(client, fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, [
        ("U0ALICE", "Should billing use Postgres or Mongo?", None),
        ("U0BOB", "Should we cache sessions in Redis?", 0),
        ("U0ALICE", "let's think about both", 0),
    ])
    fake_llm.stream_response = reply(open_questions=(
        ("Postgres or Mongo for billing?", "Should billing use Postgres or Mongo?"),
        ("Redis for sessions?", "Should we cache sessions in Redis?"),
    ))
    await slack_decisions.scan_workspace(workspace_id)
    billing, redis = [q async for q in mongo.chat_questions().find({}).sort("created_at", 1)]
    await send_digest(workspace_id)
    thread = {"_id": billing["thread_ref"]}
    # the billing question was edited away in Slack; the Redis one still stands
    await mongo.chat_threads().update_one(thread, {"$set": {"text": "U0BOB: Should we cache sessions in Redis?"}})

    _, answer = await press(client, "question_create", billing["_id"])

    assert "changed or deleted" in answer["text"]
    statuses = {q["_id"]: q["status"] async for q in mongo.chat_questions().find({})}
    assert statuses == {billing["_id"]: "withdrawn", redis["_id"]: "posted"}

@pytest.mark.parametrize(("settings", "sent_on", "at_utc", "due"), [
    ({}, None, "10:05", "2026-11-07"),
    ({}, None, "09:55", None),  # not 10:00 yet
    ({}, "2026-11-07", "18:00", None),  # already sent today
    ({"enabled": False}, None, "18:00", None),
    ({"time": "10:00", "time_zone": "Asia/Karachi"}, None, "05:01", "2026-11-07"),  # 10:01 in Karachi
    ({"time": "10:00", "time_zone": "Asia/Karachi"}, None, "04:59", None),
])
def test_the_digest_is_due_once_a_day_at_the_workspaces_time(settings, sent_on, at_utc, due):
    hour, minute = map(int, at_utc.split(":"))
    now = datetime(2026, 11, 7, hour, minute, tzinfo=timezone.utc)

    assert digest.due_day({"digest": settings, "digest_sent_on": sent_on}, now) == due


@pytest.mark.parametrize(("connected", "time", "due"), [
    ("18:20", "10:00", None),  # connected after today's time: the first digest is tomorrow's
    ("18:20", "18:55", "2026-11-07"),  # a time later today still comes today
    ("09:00", "10:00", "2026-11-07"),
])
def test_connecting_after_the_digest_time_waits_for_the_next_one(connected, time, due):
    hour, minute = map(int, connected.split(":"))
    conn = {"digest": {"time": time}, "connected_at": datetime(2026, 11, 7, hour, minute, tzinfo=timezone.utc)}

    assert digest.due_day(conn, datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)) == due


async def test_the_digest_lists_questions_with_buttons_once_a_day(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)

    first = await send_digest(workspace_id)
    again = await send_digest(workspace_id)

    [sent] = first.calls
    body = json.loads(sent.request.content)
    question = await only_question()
    assert body["channel"] == CHANNEL
    [buttons] = [block for block in body["blocks"] if block["type"] == "actions"]
    assert [e["action_id"] for e in buttons["elements"]] == ["question_create", "question_settle", "question_open"]
    assert {e.get("value") for e in buttons["elements"][:2]} == {question["_id"]}
    assert (question["status"], question["posted"]["ts"]) == ("posted", "1800000000.000100")
    assert again.calls == []  # claimed for the day


async def test_the_digest_lists_decisions_found_since_the_last_one(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, [("U0ALICE", "7 or 30 day sessions?", None), ("U0BOB", "7", 0), ("U0ALICE", "OK, 7 days then", 0)])
    fake_llm.stream_response = reply(decisions=(("Sessions last 7 days", "OK, 7 days then"),))
    await slack_decisions.scan_workspace(workspace_id)

    route = await send_digest(workspace_id, runs=2)  # two processes saw it due at once

    [sent] = route.calls
    text = json.dumps(json.loads(sent.request.content)["blocks"])
    assert "Decisions found" in text and "Sessions last 7 days" in text and "/decisions/" in text
    assert "question_create" not in text


async def test_creating_a_decision_from_the_digest_tracks_it_as_open_for_the_clicker(client, fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    question = await only_question()

    resp, answer = await press(client, "question_create", question["_id"])
    _, again = await press(client, "question_create", question["_id"])

    assert resp.status_code == 200
    [made] = [d async for d in mongo.decisions().find({"workspace_id": workspace_id})]
    shown = (await client.get(f"/decisions/{made['_id']}", headers=workspace)).json()["data"]
    assert (shown["status"], shown["origin"], shown["owner_id"], shown["created_by"]) == ("open", "suggested", workspace_id, workspace_id)
    assert (shown["evidence"][0]["kind"], shown["evidence"][0]["quote"]) == ("chat_thread", "Should billing use Postgres or Mongo?")
    assert ((await only_question())["status"], (await only_question())["decision_id"]) == ("tracked", made["_id"])
    assert answer["replace_original"] is True
    assert answer["blocks"][1] == {
        "type": "context", "block_id": digest.question_block_id(question["_id"]),
        "elements": [{"type": "mrkdwn", "text": ":memo: Tracked in Bearings as an open decision by <@U0ALICE>"}],
    }
    assert again["response_type"] == "ephemeral"  # acted on once


async def test_settling_in_the_thread_marks_it_and_says_who(client, fake_llm, workspace):
    await read_unsettled(fake_llm, workspace["X-Workspace-Id"])
    question = await only_question()

    _, answer = await press(client, "question_settle", question["_id"])

    assert (await only_question())["status"] == "settling"
    assert "<@U0ALICE> will settle this in the thread" in json.dumps(answer["blocks"])
    assert await mongo.decisions().count_documents({}) == 0


async def test_someone_not_in_bearings_is_told_privately(client, fake_llm, workspace):
    await read_unsettled(fake_llm, workspace["X-Workspace-Id"])
    question = await only_question()

    _, answer = await press(client, "question_create", question["_id"], email="stranger@example.com")

    assert (answer["response_type"], answer["replace_original"]) == ("ephemeral", False)
    assert "Sign in to Bearings" in answer["text"]
    assert (await only_question())["status"] == "pending"


async def test_unsigned_clicks_are_refused(client, fake_llm, workspace):
    await read_unsettled(fake_llm, workspace["X-Workspace-Id"])

    resp, answer = await press(client, "question_create", (await only_question())["_id"], secret="someone-else")

    assert (resp.status_code, answer) == (401, None)


async def test_a_question_nobody_acts_on_expires_and_its_conversation_goes(fake_llm, workspace, monkeypatch):
    await read_unsettled(fake_llm, workspace["X-Workspace-Id"])
    question = await only_question()
    await mongo.chat_questions().update_one({"_id": question["_id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})

    assert await questions.expire_due() == 1
    assert (await only_question())["status"] == "expired"
    assert await mongo.chat_threads().count_documents({}) == 0


async def test_admins_set_the_digest_time_and_zone(client, workspace):
    default = (await client.get("/slack/digest", headers=workspace)).json()["data"]
    changed = await client.patch("/slack/digest", json={"time": "09:30", "time_zone": "Asia/Karachi"}, headers=workspace)
    bad = await client.patch("/slack/digest", json={"time_zone": "Mars/Base"}, headers=workspace)

    assert default == {"enabled": True, "time": "10:00", "time_zone": None}
    assert changed.json()["data"] == {"enabled": True, "time": "09:30", "time_zone": "Asia/Karachi"}
    assert bad.status_code == 422
