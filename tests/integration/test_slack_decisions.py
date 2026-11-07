"""Finding decisions in Slack conversations: quiet discussions read once, saved as Decisions proposals."""

import json
import time

import pytest

from app.connectors import repository as connections
from app.core.config import get_settings
from app.core.time import utcnow
from app.db import mongo
from app.slack import decisions as slack_decisions
from app.slack.decisions import Conversation, Message, conversations
from tests.integration.test_work_items_api import signed_in

CHANNEL = "C0GENERAL"
OLD = time.time() - 20 * 60  # quiet for 20 minutes


def ts(offset: float) -> str:
    return f"{OLD + offset:.6f}"


SETTLED = [
    ("U0ALICE", "Should sessions last 7 days or 30?", None),
    ("U0BOB", "7 is safer for shared laptops.", 0),
    ("U0ALICE", "OK, 7 days then", 0),
]


def reply(*decisions: tuple[str, str]) -> str:
    return json.dumps({"decisions": [
        {"title": title, "statement": f"{title}.", "quotes": [{"source": "S1", "text": quote}]} for title, quote in decisions
    ]})


@pytest.fixture
async def workspace(client):
    headers = await signed_in(client)
    workspace_id = headers["X-Workspace-Id"]
    await connections.save_connection(
        workspace_id, "slack", auth_backend="native_oauth", scopes=[], connected_by="u-1",
        external_account={"id": "T0ACME", "name": "Acme", "url": "https://acme.slack.com/"},
    )
    await connections.add_target(workspace_id, "slack", CHANNEL, "#general")
    return headers


async def post(workspace_id: str, rows: list[tuple[str, str, int | None]], start: float = 0) -> list[str]:
    """Store messages as the buffer would: a reply's thread is the index of the message it answers."""
    stamps = [ts(start + i) for i in range(len(rows))]
    await mongo.channel_messages().insert_many([
        {
            "workspace_id": workspace_id, "channel": CHANNEL, "ts": stamps[i], "user": user, "text": text,
            "thread_ts": stamps[parent] if parent is not None else None, "received_at": utcnow(),
        }
        for i, (user, text, parent) in enumerate(rows)
    ])
    return stamps


async def decision(client, headers: dict, decision_id: str) -> dict:
    return (await client.get(f"/decisions/{decision_id}", headers=headers)).json()["data"]


def test_messages_are_grouped_into_threads_and_runs():
    root, reply_1, reply_2 = Message("100.0", "A", "q", None), Message("101.0", "B", "a", "100.0"), Message("102.0", "A", "ok", "100.0")
    early, near, far = Message("200.0", "A", "x", None), Message("500.0", "B", "y", None), Message("2000.0", "A", "z", None)

    found = conversations(CHANNEL, [far, reply_2, early, root, near, reply_1])

    assert [[m.text for m in c.messages] for c in found] == [["q", "a", "ok"], ["x", "y"], ["z"]]


@pytest.mark.parametrize(("rows", "age", "ready"), [
    ([("A", "1"), ("B", "2"), ("A", "3")], 20 * 60, True),
    ([("A", "1"), ("B", "2"), ("A", "3")], 5 * 60, False),  # still being talked about
    ([("A", "1"), ("A", "2"), ("A", "3")], 20 * 60, False),  # one person
    ([("A", "1"), ("B", "2")], 20 * 60, False),  # too short to settle anything
])
def test_only_quiet_discussions_are_read(rows, age, ready):
    now = 10_000.0
    messages = tuple(Message(f"{now - age + i:.1f}", user, text, None) for i, (user, text) in enumerate(rows))

    assert Conversation(CHANNEL, messages, messages[0].ts, is_thread=False).is_ready(now) is ready


def test_the_link_opens_the_conversation_in_slack():
    assert slack_decisions.permalink("https://acme.slack.com/", CHANNEL, "1700000000.000100") == (
        "https://acme.slack.com/archives/C0GENERAL/p1700000000000100"
    )


async def test_a_settled_discussion_becomes_a_proposal_quoting_slack(client, fake_llm, workspace):
    stamps = await post(workspace["X-Workspace-Id"], SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"), ("Invented", "Sessions last a year"))

    suggested = await slack_decisions.scan_workspace(workspace["X-Workspace-Id"], "https://acme.slack.com/")

    assert suggested == 1
    [made] = [d async for d in mongo.decisions().find({"workspace_id": workspace["X-Workspace-Id"]})]
    shown = await decision(client, workspace, made["_id"])
    assert (shown["title"], shown["status"], shown["origin"], shown["created_by"]) == ("Sessions last 7 days", "proposed", "suggested", "system")
    [evidence] = shown["evidence"]
    assert (evidence["kind"], evidence["quote"], evidence["state"]) == ("chat_thread", "OK, 7 days then", "current")
    assert evidence["title"] == "Slack #general"
    assert evidence["external_url"] == f"https://acme.slack.com/archives/C0GENERAL/p{stamps[0].replace('.', '')}"
    # the conversation is kept as the decision's source, and was fenced as data with the chat prompt
    assert await mongo.chat_threads().count_documents({}) == 1
    assert fake_llm.calls[0]["system"] == slack_decisions.CHAT_PROMPT
    assert fake_llm.calls[0]["prompt"].startswith('<source id="S1">\nU0ALICE: Should sessions last 7 days or 30?\n')


async def test_chit_chat_is_read_once_and_not_kept(fake_llm, workspace):
    await post(workspace["X-Workspace-Id"], [("U0ALICE", "should i go to uni today?", None), ("U0BOB", "yes", 0), ("U0ALICE", "ok", 0)])
    fake_llm.stream_response = reply()

    first = await slack_decisions.scan_workspace(workspace["X-Workspace-Id"])
    again = await slack_decisions.scan_workspace(workspace["X-Workspace-Id"])

    assert (first, again) == (0, 0)
    assert len(fake_llm.calls) == 1  # unchanged, so not paid for twice
    assert await mongo.chat_threads().count_documents({}) == 0


async def test_a_conversation_still_going_or_unwatched_is_left_alone(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    fresh = [("U0ALICE", "Postgres or Mongo?", None), ("U0BOB", "Postgres", 0), ("U0ALICE", "agreed", 0)]
    await post(workspace_id, fresh, start=19 * 60)  # last message a minute ago
    await connections.remove_target(workspace_id, "slack", CHANNEL)
    await post(workspace_id, SETTLED, start=-600)

    assert await slack_decisions.scan_workspace(workspace_id) == 0
    assert fake_llm.calls == []



async def test_a_late_reply_to_an_old_thread_keeps_what_was_found_in_it(client, fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)
    made = await mongo.decisions().find_one({})
    await mongo.channel_messages().delete_many({})  # hours pass: the thread's messages expire
    late = [("U0CAROL", "late to this, why 7?", 0), ("U0BOB", "shared laptops", 0), ("U0CAROL", "fair", 0)]
    await mongo.channel_messages().insert_many([
        {"workspace_id": workspace_id, "channel": CHANNEL, "ts": ts(60 + i), "user": user, "text": text,
         "thread_ts": ts(0), "received_at": utcnow()}
        for i, (user, text, _) in enumerate(late)
    ])
    fake_llm.stream_response = reply()

    await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == 2  # the late replies were read
    copy = await mongo.chat_threads().find_one({"_id": made["evidence"][0]["ref_id"]})
    assert [m["text"] for m in copy["messages"]] == [text for _, text, _ in SETTLED + late]
    # the conversation grew but still says what was quoted, as an edited ticket would (not quote_missing)
    assert (await decision(client, workspace, made["_id"]))["evidence"][0]["state"] == "changed"


async def test_a_spent_ai_budget_pauses_the_reads_like_an_outage(fake_llm, workspace, monkeypatch):
    monkeypatch.setenv("CHAT_DAILY_BUDGET", "0")
    get_settings.cache_clear()
    await post(workspace["X-Workspace-Id"], SETTLED)

    with pytest.raises(slack_decisions.AIUnavailable):
        await slack_decisions.scan_workspace(workspace["X-Workspace-Id"])
    assert fake_llm.calls == []

async def test_edits_and_deletes_in_slack_reach_the_decisions_evidence(client, fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    stamps = await post(workspace_id, SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)
    [made] = [d async for d in mongo.decisions().find({"workspace_id": workspace_id})]

    await slack_decisions.follow_edit(workspace_id, CHANNEL, stamps[1], "7 is safer, I think.")
    edited = (await decision(client, workspace, made["_id"]))["evidence"][0]["state"]
    await slack_decisions.follow_edit(workspace_id, CHANNEL, stamps[2], None)
    deleted = (await decision(client, workspace, made["_id"]))["evidence"][0]["state"]

    assert (edited, deleted) == ("changed", "quote_missing")


async def test_dismissing_the_suggestion_drops_the_kept_conversation(client, fake_llm, workspace):
    await post(workspace["X-Workspace-Id"], SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace["X-Workspace-Id"])
    [made] = [d async for d in mongo.decisions().find({"workspace_id": workspace["X-Workspace-Id"]})]

    resp = await client.post(f"/decisions/{made['_id']}/dismiss", headers=workspace)

    assert resp.status_code == 200
    assert await mongo.chat_threads().count_documents({}) == 0


async def test_reads_stop_at_the_daily_limit(fake_llm, workspace, monkeypatch):
    monkeypatch.setenv("SLACK_DECISION_SCANS_PER_DAY", "1")
    get_settings.cache_clear()
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    await post(workspace_id, [("U0ALICE", "Dark mode for v1?", None), ("U0BOB", "No, later", 0), ("U0ALICE", "fine", 0)], start=-900)
    fake_llm.stream_response = reply()

    await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == 1
    assert await mongo.chat_threads().count_documents({}) == 0
