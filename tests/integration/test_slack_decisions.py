"""Finding decisions in Slack conversations: quiet discussions read once, saved as Decisions proposals."""

import json
import time

import pytest

from app.chat import limits
from app.chat.limits import daily_budget
from app.connectors import repository as connections
from app.core import rate_limit
from app.core.config import get_settings
from app.core.time import utcnow
from app.db import mongo
from app.slack import decisions as slack_decisions
from app.slack.decisions import Conversation, Message, conversations
from app.usage.allowance import monthly_key
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


@pytest.mark.parametrize(("users", "ready"), [
    (["A", "B"], True),  # a question and someone's answer
    (["A", "A"], False),  # one person
    (["A"], False),
])
def test_a_short_thread_is_read_once_two_people_are_in_it(users, ready):
    now = 10_000.0
    root = f"{now - 20 * 60:.1f}"
    messages = tuple(Message(f"{now - 20 * 60 + i:.1f}", user, "text", root if i else None) for i, user in enumerate(users))

    assert Conversation(CHANNEL, messages, root, is_thread=True).is_ready(now) is ready


def test_a_discussion_that_never_goes_quiet_is_read_before_it_can_expire():
    now = 100_000.0
    busy = tuple(Message(f"{now - 2.5 * 3600 + i * 600:.1f}", "AB"[i % 2], "text", None) for i in range(15))
    assert now - float(busy[-1].ts) < slack_decisions.QUIET_SECONDS

    assert Conversation(CHANNEL, busy, busy[0].ts, is_thread=False).is_ready(now) is True
    young = tuple(Message(f"{now - 1800 + i * 300:.1f}", "AB"[i % 2], "text", None) for i in range(5))
    assert Conversation(CHANNEL, young, young[0].ts, is_thread=False).is_ready(now) is False


async def test_nothing_is_loaded_or_read_at_the_daily_limit(fake_llm, workspace, monkeypatch):
    await post(workspace["X-Workspace-Id"], SETTLED)
    [window, *_] = slack_decisions._scan_windows(workspace["X-Workspace-Id"], utcnow())
    await mongo.usage_counters().insert_one({"_id": window.key, "count": window.limit})
    loaded = []
    real = mongo.channel_messages

    def spy():
        loaded.append(True)
        return real()

    monkeypatch.setattr(mongo, "channel_messages", spy)

    assert await slack_decisions.scan_workspace(workspace["X-Workspace-Id"]) == 0
    assert (loaded, fake_llm.calls, await mongo.chat_threads().count_documents({})) == ([], [], 0)


async def test_scanning_resumes_once_a_read_is_given_back(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    [window, *_] = slack_decisions._scan_windows(workspace_id, utcnow())
    await mongo.usage_counters().insert_one({"_id": window.key, "count": window.limit})
    assert await slack_decisions.scan_workspace(workspace_id) == 0
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))

    await slack_decisions._give_back_scan(workspace_id)

    assert await slack_decisions.scan_workspace(workspace_id) == 1


class CountsLoads:
    """The messages collection, counting how often they're loaded rather than just asked about."""

    def __init__(self, real):
        self.real = real
        self.loads = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def find(self, *args, **kwargs):
        self.loads += 1
        return self.real.find(*args, **kwargs)


def stored(workspace_id: str, stamp: float, user: str = "U0ALICE", **fields) -> dict:
    return {"workspace_id": workspace_id, "channel": CHANNEL, "ts": f"{stamp:.6f}", "user": user, "text": "x", "thread_ts": None, "received_at": utcnow(), **fields}


@pytest.mark.parametrize(("age", "fields"), [
    (60, {}),  # unread, but too new to be ready or settled
    (40 * 60, {"read_ref": "seen"}),  # old, but already read
])
async def test_messages_are_not_loaded_when_nothing_unread_is_old_enough(fake_llm, workspace, monkeypatch, age, fields):
    workspace_id = workspace["X-Workspace-Id"]
    now = time.time() - age
    await mongo.channel_messages().insert_many([stored(workspace_id, now + i, "AB"[i % 2], **fields) for i in range(3)])
    collection = CountsLoads(mongo.channel_messages())
    monkeypatch.setattr(mongo, "channel_messages", lambda: collection)

    assert await slack_decisions.scan_workspace(workspace_id) == 0

    assert (collection.loads, fake_llm.calls) == (0, [])


async def test_only_a_quiet_message_is_settled(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    old, young = time.time() - 40 * 60, time.time() - 60
    await mongo.channel_messages().insert_many([stored(workspace_id, old), stored(workspace_id, young)])

    await slack_decisions.scan_workspace(workspace_id)

    assert (await mongo.channel_messages().find_one({"ts": f"{old:.6f}"})).get("read_ref")
    assert (await mongo.channel_messages().find_one({"ts": f"{young:.6f}"})).get("read_ref") is None


@pytest.mark.parametrize(("used", "reads"), [(5, 1), (6, 0)])
async def test_slack_leaves_people_their_share_of_the_ai_budget(fake_llm, workspace, monkeypatch, used, reads):
    monkeypatch.setenv("CHAT_DAILY_BUDGET", "10")
    get_settings.cache_clear()
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    await mongo.usage_counters().insert_one({"_id": daily_budget(utcnow()).key, "count": used})
    fake_llm.stream_response = json.dumps({"decisions": []})

    if reads:
        await slack_decisions.scan_workspace(workspace_id)
    else:
        with pytest.raises(slack_decisions.AIUnavailable):
            await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == reads
    assert await limits.take_model_call() is True


async def test_a_slack_read_does_not_spend_the_monthly_allowance(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = json.dumps({"decisions": []})

    await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == 1
    assert await mongo.ai_actions().count_documents({"workspace_id": workspace_id}) == 0
    assert await rate_limit.count(monthly_key(workspace_id, utcnow())) == 0


async def test_a_lone_thread_is_settled_then_a_later_reply_gets_it_read(fake_llm, workspace):
    workspace_id = workspace["X-Workspace-Id"]
    [root] = await post(workspace_id, [("U0ALICE", "Should sessions last 7 days or 30?", None)])

    await slack_decisions.scan_workspace(workspace_id)

    assert fake_llm.calls == []
    stored = await mongo.channel_messages().find_one({"ts": root})
    assert stored["read_ref"]

    fake_llm.stream_response = reply(("Sessions last 7 days", "7 days, agreed"))
    await mongo.channel_messages().insert_one({
        "workspace_id": workspace_id, "channel": CHANNEL, "ts": ts(1), "user": "U0BOB", "text": "7 days, agreed", "thread_ts": root, "received_at": utcnow(),
    })

    assert await slack_decisions.scan_workspace(workspace_id) == 1
    assert len(fake_llm.calls) == 1
    assert await mongo.channel_messages().count_documents({"read_ref": {"$type": "string"}}) == 2


async def test_a_forced_read_keeps_its_id_when_the_discussion_is_read_again(fake_llm, workspace, monkeypatch):
    workspace_id = workspace["X-Workspace-Id"]
    start = time.time() - 2.5 * 3600
    stamps = [f"{start + i * 600:.6f}" for i in range(15)]
    await mongo.channel_messages().insert_many([
        {"workspace_id": workspace_id, "channel": CHANNEL, "ts": s, "user": "AB"[i % 2], "text": f"point {i}", "thread_ts": None, "received_at": utcnow()}
        for i, s in enumerate(stamps)
    ])
    fake_llm.stream_response = reply(("First call", "point 3"))
    assert await slack_decisions.scan_workspace(workspace_id) == 1
    [first_ref] = await mongo.channel_messages().distinct("read_ref")
    # the run's first message expires, so only the id it was read under can say which conversation this is
    await mongo.channel_messages().delete_one({"ts": stamps[0]})

    await mongo.channel_messages().insert_one({
        "workspace_id": workspace_id, "channel": CHANNEL, "ts": f"{start + 9000:.6f}", "user": "A", "text": "point 15", "thread_ts": None, "received_at": utcnow(),
    })
    monkeypatch.setattr(slack_decisions, "QUIET_SECONDS", 0)
    fake_llm.stream_response = json.dumps({"decisions": []})

    await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == 2
    assert await mongo.channel_messages().distinct("read_ref") == [first_ref]
    assert await mongo.decisions().count_documents({"workspace_id": workspace_id}) == 1


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
