"""The Slack integration's guard rails: one Bearings workspace per Slack workspace, conversations read once for
good, digests that one workspace can't break, deleted chat honoured, and what Disconnect and uninstall remove."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from app.connectors import repository as connections
from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError
from app.core.time import utcnow
from app.db import mongo, oauth_tokens
from app.slack import connections as slack_connections
from app.slack import decisions as slack_decisions
from app.slack import digest, events, messages, questions
from app.slack.decisions import Message, conversations, thread_id
from tests.integration.test_slack_connection import CHANNELS_URL, connect, slack
from tests.integration.test_slack_decisions import CHANNEL, SETTLED, decision, post, reply
from tests.integration.test_slack_digest import (
    UNSETTLED, answer_in_thread, only_question, press, read_unsettled, send_digest,
)
from tests.integration.test_slack_digest import workspace as workspace  # noqa: F401  (fixture)
from tests.integration.test_workspaces import bearer, in_workspace, team, user

REVOKE_URL = "https://slack.com/api/auth.revoke"
POST_URL = "https://slack.com/api/chat.postMessage"


# -- one Bearings workspace per Slack workspace (S-1) ------------------------------------------------------------

@pytest.fixture
def slack_app(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-slack-client-id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "test-slack-client-secret")
    get_settings.cache_clear()


async def test_a_slack_workspace_connects_to_one_bearings_workspace_at_a_time(client, slack_app):
    alice, bob = await user(client, "alice"), await user(client, "bob")

    first = await connect(client, alice)
    second = await connect(client, bob)

    assert first.headers["location"].endswith(f"slack=connected&w={alice['X-Workspace-Id']}")
    # the admin is told why, not just that it failed
    assert second.headers["location"].endswith("/settings/integrations?slack=taken")
    assert await slack(client, bob) is not None and (await slack(client, bob))["status"] == "disconnected"
    # a reconnect by the workspace that holds it still works
    assert (await connect(client, alice)).headers["location"].endswith("slack=connected&w=" + alice["X-Workspace-Id"])



async def test_deleting_a_workspace_frees_its_slack_workspace(client, slack_app):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    await connect(client, in_workspace(alice, acme))

    with respx.mock as router:
        revoke = router.get(REVOKE_URL).mock(return_value=Response(200, json={"ok": True, "revoked": True}))
        deleted = await client.delete(f"/workspaces/{acme}", headers=bearer(alice))
    elsewhere = await connect(client, in_workspace(alice, await team(client, alice, "Other")))

    assert (deleted.status_code, len(revoke.calls)) == (200, 1)  # its bot token is given back
    assert "slack=connected" in elsewhere.headers["location"]

async def test_only_channels_the_app_is_in_can_be_watched(client, slack_app):
    alice = await user(client, "alice")
    await connect(client, alice)
    listing = {"ok": True, "channels": [
        {"id": "C0GENERAL", "name": "general", "is_member": True},
        {"id": "C0SECRET", "name": "leadership", "is_member": False},
    ]}

    with respx.mock as router:
        router.get(CHANNELS_URL).mock(return_value=Response(200, json=listing))
        joined = await client.post("/connections/slack/targets", json={"key": "C0GENERAL", "name": "renamed"}, headers=alice)
        not_joined = await client.post("/connections/slack/targets", json={"key": "C0SECRET"}, headers=alice)
        unknown = await client.post("/connections/slack/targets", json={"key": "C0NOWHERE"}, headers=alice)

    assert (joined.status_code, joined.json()["data"]["name"]) == (201, "#general")  # Slack's name, not the client's
    assert not_joined.json()["meta"]["error_code"] == "channel_not_joined"
    assert unknown.json()["meta"]["error_code"] == "target_not_available"


# -- a conversation keeps its id for good (S-2) ------------------------------------------------------------------

def test_a_thread_keeps_its_id_after_its_first_message_expires():
    root = Message("100.0", "A", "q", None)
    replies = [Message(f"10{i}.0", "B", "a", "100.0") for i in range(1, 4)]

    whole, = conversations("C", [root, *replies])
    trimmed, = conversations("C", replies)

    assert thread_id("w", whole) == thread_id("w", trimmed)


def test_a_run_keeps_the_id_it_was_read_under():
    read = [Message(f"{100 + i}.0", "AB"[i % 2], "x", None, read_ref="ref-1") for i in range(4)]

    trimmed, = conversations("C", read[2:] + [Message("200.0", "A", "new", None)])

    assert thread_id("w", trimmed) == "ref-1"


async def test_a_conversation_isnt_read_again_when_its_oldest_messages_expire(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    stamps = await post(workspace_id, SETTLED + [("U0BOB", "yes, 7 is fine", 0)])
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)

    await mongo.channel_messages().delete_one({"ts": stamps[0]})  # the TTL takes the first message
    again = await slack_decisions.scan_workspace(workspace_id)

    assert again == 0
    assert len(fake_llm.calls) == 1
    assert await mongo.decisions().count_documents({}) == 1


# -- one workspace's settings can't stop the digests (S-3), and the digest's timing (S-11) -----------------------

async def test_null_settings_are_refused(client, workspace):  # noqa: F811
    for body in ({"time": None}, {"enabled": None}):
        assert (await client.patch("/slack/digest", json=body, headers=workspace)).status_code == 422


@pytest.mark.parametrize("stored", [{"time": None}, {"enabled": None, "time": None, "time_zone": None}, {"time_zone": "Mars/Base"}])
def test_unreadable_stored_settings_fall_back_to_the_defaults(stored):
    now = datetime(2026, 11, 7, 10, 5, tzinfo=timezone.utc)

    assert digest.due_day({"digest": stored}, now) == "2026-11-07"


def test_a_digest_set_just_before_midnight_is_caught_after_it():
    conn = {"digest": {"time": "23:58", "time_zone": "UTC"}}

    caught = digest.due_day(conn, datetime(2026, 11, 8, 0, 3, tzinfo=timezone.utc))
    too_late = digest.due_day(conn, datetime(2026, 11, 8, 1, 30, tzinfo=timezone.utc))
    sent = digest.due_day({**conn, "digest_sent_on": "2026-11-07"}, datetime(2026, 11, 8, 0, 3, tzinfo=timezone.utc))

    assert (caught, too_late, sent) == ("2026-11-07", None, None)


async def test_one_workspaces_failure_doesnt_stop_the_others(monkeypatch):
    sent: list[str] = []

    async def serving():
        for conn in ({"workspace_id": "broken", "digest": {}}, {"workspace_id": "fine", "digest": {}}):
            yield conn

    async def send(conn, today, now):
        if conn["workspace_id"] == "broken":
            raise RuntimeError("boom")
        sent.append(conn["workspace_id"])

    monkeypatch.setattr(slack_connections, "serving", serving)
    monkeypatch.setattr(digest, "send", send)
    # after the default digest time, so both workspaces are due whenever this runs
    monkeypatch.setattr(digest, "utcnow", lambda: datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))

    await digest.run_due()

    assert sent == ["fine"]


# -- deleted chat is honoured (S-4) and the digest links its workspace (S-12) -------------------------------------

async def test_a_question_whose_message_was_deleted_is_withdrawn_not_posted(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    first = (await only_question())["thread_ref"]
    thread = await mongo.chat_threads().find_one({"_id": first})

    await slack_decisions.follow_edit(workspace_id, CHANNEL, thread["messages"][0]["ts"], None)
    route = await send_digest(workspace_id)

    question = await only_question()
    assert (question["status"], "quote" in question) == ("withdrawn", False)
    assert route.calls == []
    assert await mongo.chat_threads().count_documents({}) == 0


async def test_the_digest_rechecks_a_questions_message_before_posting_it(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    question = await only_question()
    # the kept conversation no longer says it, however that came about (an edit missed while the app was down)
    await mongo.chat_threads().update_one({"_id": question["thread_ref"]}, {"$set": {"text": "U0ALICE: something else"}})

    route = await send_digest(workspace_id)

    assert route.calls == []
    assert (await only_question())["status"] == "withdrawn"


async def test_tracking_a_question_whose_message_went_says_so(client, fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    question = await only_question()
    await mongo.chat_threads().update_one({"_id": question["thread_ref"]}, {"$set": {"text": "U0ALICE: something else"}})

    _, answer = await press(client, "question_create", question["_id"])

    assert "changed or deleted in Slack" in answer["text"]
    assert (await only_question())["status"] == "withdrawn"
    assert await mongo.decisions().count_documents({}) == 0


async def test_decision_links_open_in_their_workspace(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)

    body = json.loads((await send_digest(workspace_id)).calls[0].request.content)

    assert f"?w={workspace_id}|" in json.dumps(body["blocks"])


async def test_a_channel_the_bot_left_shows_why_its_digest_failed(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    conn = await mongo.connections().find_one({"workspace_id": workspace_id, "provider": "slack"})

    with respx.mock as router:
        router.post(POST_URL).mock(return_value=Response(200, json={"ok": False, "error": "not_in_channel"}))
        await digest.send(conn, "2026-11-07", utcnow())

    target = await mongo.sync_targets().find_one({"workspace_id": workspace_id, "key": CHANNEL})
    assert target["sync_error"] == digest.NOT_IN_CHANNEL
    assert (await only_question())["status"] == "pending"


# -- what Disconnect and uninstall remove (S-5), and what's kept no longer than needed (S-6) ----------------------

async def test_disconnect_gives_the_token_back_and_forgets_the_chat_but_not_decisions(client, fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED, start=-600)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)
    await read_unsettled(fake_llm, workspace_id)
    messages.buffer.apply(workspace_id, "T0ACME", CHANNEL, messages.Change("1.0", {"user": "U", "text": "waiting", "thread_ts": None}))

    with respx.mock as router:
        revoke = router.get(REVOKE_URL).mock(return_value=Response(200, json={"ok": True, "revoked": True}))
        resp = await client.delete("/connections/slack", headers=workspace)

    token = await oauth_tokens.get(workspace_id, "slack")
    assert (resp.status_code, len(revoke.calls)) == (200, 1)
    assert (token["status"], "access_token_encrypted" in token) == ("disconnected", False)
    assert await mongo.channel_messages().count_documents({}) == 0
    assert await mongo.chat_questions().count_documents({}) == 0
    assert len(messages.buffer) == 0
    # the conversation the decision quotes stays with it; the question's conversation goes
    assert await mongo.chat_threads().count_documents({}) == 1


async def test_slacks_answer_to_a_disconnect_keeps_what_decisions_quote(client, fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)
    made = await mongo.decisions().find_one({})

    async def revoked(request):
        # Slack reports the app removed (tokens_revoked, app_uninstalled) for the token we give back,
        # and those events can land before the revoke call has even returned
        await events._uninstalled("T0ACME")
        return Response(200, json={"ok": True, "revoked": True})

    with respx.mock as router:
        router.get(REVOKE_URL).mock(side_effect=revoked)
        await client.delete("/connections/slack", headers=workspace)
    await events._uninstalled("T0ACME")

    assert await mongo.chat_threads().count_documents({}) == 1
    assert (await decision(client, workspace, made["_id"]))["evidence"][0]["state"] == "current"


async def test_removing_the_app_in_slack_drops_even_what_decisions_quote(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)

    await events._uninstalled("T0ACME")

    assert await mongo.chat_threads().count_documents({}) == 0


async def test_a_closed_question_drops_its_quote_and_is_forgotten_later(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    question = await only_question()
    await mongo.chat_questions().update_one({"_id": question["_id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
    await questions.expire_due()
    assert "quote" not in await only_question()

    await mongo.chat_questions().update_one({"_id": question["_id"]}, {"$set": {"closed_at": utcnow() - timedelta(days=31)}})
    await questions.expire_due()

    assert await mongo.chat_questions().count_documents({}) == 0


async def test_removing_a_decisions_slack_evidence_drops_the_conversation(client, fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = reply(("Sessions last 7 days", "OK, 7 days then"))
    await slack_decisions.scan_workspace(workspace_id)
    [made] = [d async for d in mongo.decisions().find({})]

    resp = await client.delete(f"/decisions/{made['_id']}/evidence/{made['evidence'][0]['id']}", headers=workspace)

    assert resp.status_code == 200
    assert await mongo.chat_threads().count_documents({}) == 0


# -- AI failures (S-7) and deleted workspaces (S-8) ---------------------------------------------------------------

async def test_an_ai_outage_gives_the_read_back_and_stops(fake_llm, workspace, monkeypatch):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)

    async def down(*args, **kwargs):
        raise ExternalServiceError("busy", error_code="llm_unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(fake_llm, "stream_text", down)
    with pytest.raises(slack_decisions.AIUnavailable):
        await slack_decisions.scan_workspace(workspace_id)

    counters = {doc["_id"]: doc["count"] async for doc in mongo.usage_counters().find({})}
    assert counters and set(counters.values()) == {0}
    assert await mongo.chat_threads().count_documents({}) == 0


async def test_a_read_that_keeps_failing_is_retried_later_then_left(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await post(workspace_id, SETTLED)
    fake_llm.stream_response = "not json at all"

    await slack_decisions.scan_workspace(workspace_id)
    await slack_decisions.scan_workspace(workspace_id)  # waiting out its backoff: not read
    for _ in range(slack_decisions.MAX_FAILURES - 1):
        await mongo.decision_scans().update_many({}, {"$set": {"retry_after": utcnow() - timedelta(seconds=1)}})
        await slack_decisions.scan_workspace(workspace_id)
    await slack_decisions.scan_workspace(workspace_id)

    assert len(fake_llm.calls) == slack_decisions.MAX_FAILURES
    assert await mongo.channel_messages().count_documents({"read_ref": None}) == 0


async def test_a_deleted_workspace_isnt_served(workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    before = [conn["workspace_id"] async for conn in slack_connections.serving()]
    await mongo.workspaces().update_one({"_id": workspace_id}, {"$set": {"deleted_at": utcnow()}})
    events.forget_listeners()

    after = [conn["workspace_id"] async for conn in slack_connections.serving()]

    assert (before, after) == ([workspace_id], [])
    assert await events._listeners_of("T0ACME") == []


# -- shutdown keeps unsent messages (S-9), webhook hardening (S-14), identity (S-15) -------------------------------

async def test_a_cancelled_write_keeps_its_batch(monkeypatch):
    buffer = messages.MessageBuffer()
    buffer.apply("ws-1", "T0ACME", CHANNEL, messages.Change("1.0", {"user": "U", "text": "kept", "thread_ts": None}))
    real = mongo.channel_messages

    class Slow:
        async def bulk_write(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(mongo, "channel_messages", lambda: Slow())
    task = asyncio.create_task(buffer.flush())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    monkeypatch.setattr(mongo, "channel_messages", real)

    assert len(buffer) == 1
    assert await buffer.flush() == 1


def test_a_garbled_signature_is_a_mismatch_not_an_error(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    get_settings.cache_clear()

    assert events.verify_signature(b"{}", str(int(utcnow().timestamp())), "v0=é") is False


async def test_oversized_requests_are_refused_before_reading(client):
    resp = await client.post("/webhooks/slack/events", content=b"x", headers={"Content-Length": str(events.MAX_BODY_BYTES + 1)})

    assert resp.status_code == 413


async def test_an_unconfirmed_slack_email_isnt_trusted(client, fake_llm, workspace):  # noqa: F811
    await read_unsettled(fake_llm, workspace["X-Workspace-Id"])
    question = await only_question()

    with respx.mock as router:
        router.get("https://slack.com/api/users.info").mock(return_value=Response(200, json={
            "ok": True, "user": {"id": "U0ALICE", "is_email_confirmed": False, "profile": {"email": "octo@example.com"}},
        }))
        answer = router.post("https://hooks.slack.com/actions/T0ACME/1/abc").mock(return_value=Response(200))
        from tests.integration.test_slack_digest import click

        raw, headers = click("question_create", question["_id"])
        await client.post("/webhooks/slack/interactions", content=raw, headers=headers)

    assert "Sign in to Bearings" in json.loads(answer.calls[0].request.content)["text"]
    assert (await only_question())["status"] == "pending"


async def test_settling_after_a_decision_closes_the_question(fake_llm, workspace):  # noqa: F811
    workspace_id = workspace["X-Workspace-Id"]
    await read_unsettled(fake_llm, workspace_id)
    await answer_in_thread(workspace_id, "U0BOB", "OK, Postgres it is")
    fake_llm.stream_response = reply(("Billing uses Postgres", "OK, Postgres it is"))

    await slack_decisions.scan_workspace(workspace_id)

    question = await only_question()
    assert (question["status"], "quote" in question) == ("settled", False)
    assert UNSETTLED  # the conversation the question came from
