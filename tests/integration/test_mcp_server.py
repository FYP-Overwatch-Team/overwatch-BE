"""The MCP server: an AI tool signs in with its token and reads the one workspace the token is tied to."""

import asyncio
import json

import pytest

from tests.integration.test_ai_tools_api import token
from app.db import mongo
from tests.integration.test_decisions_api import decide, page, quoting
from tests.integration.test_workspaces import bearer, in_workspace, team, user

HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture(autouse=True)
async def running(app):
    """The server's task group, which the API's lifespan holds open in production; entered and left in one task."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with app.state.mcp.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await started.wait()
    yield
    stop.set()
    await task


async def rpc(client, raw_token: str | None, method: str, params: dict | None = None):
    headers = {**HEADERS, **({"Authorization": f"Bearer {raw_token}"} if raw_token else {})}
    return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)


async def call(client, raw_token: str, tool: str, **arguments) -> dict:
    """A tool's result: its data, or `{"error": ...}` when the tool refused."""
    result = (await rpc(client, raw_token, "tools/call", {"name": tool, "arguments": arguments})).json()["result"]
    text = result["content"][0]["text"]
    return {"error": text} if result["isError"] else json.loads(text)


async def connected(client, login: str = "alice", **body) -> tuple[dict, str]:
    """A user with a team workspace and a tool connected to it: their headers there, and the tool's token."""
    headers = await user(client, login)
    headers = in_workspace(headers, await team(client, headers))
    return headers, (await token(client, headers, **body))["token"]


async def test_a_tool_without_a_valid_token_is_told_where_to_sign_in(client):
    for raw in (None, "bwp_wrong"):
        refused = await rpc(client, raw, "tools/list")

        assert refused.status_code == 401
        assert "/.well-known/oauth-protected-resource/mcp" in refused.headers["www-authenticate"]
    described = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert described["resource"].endswith("/mcp") and described["scopes_supported"] == ["decisions:read"]


async def test_a_tool_is_given_the_rules_and_only_read_tools_change_nothing(client):
    _, raw = await connected(client)

    hello = await rpc(client, raw, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    listed = (await rpc(client, raw, "tools/list")).json()["result"]["tools"]

    assert "start_task" in hello.json()["result"]["instructions"]
    assert {tool["name"] for tool in listed} >= {"start_task", "search_decisions", "get_decision", "search_workspace"}
    assert all(tool["annotations"]["readOnlyHint"] for tool in listed if tool["name"] != "record_decision")


async def test_only_a_post_is_answered_and_other_paths_keep_the_apis_own_errors(client):
    _, raw = await connected(client)

    opened = await client.get("/mcp", headers={**HEADERS, "Authorization": f"Bearer {raw}"})
    missing = await client.get("/nothing-here")

    assert opened.status_code == 405
    assert missing.status_code == 404 and missing.json()["status"] == "error"


async def test_starting_a_task_shows_what_was_decided_and_what_is_still_open(client):
    headers, raw = await connected(client)
    await decide(client, headers, title="Locking", statement="Use advisory locks for imports.", status="accepted", area="Imports")
    await decide(client, headers, title="Retry policy", statement="How often do we retry imports?")
    await decide(client, headers, title="Guest export", statement="Guests can't export.", status="proposed")

    started = await call(client, raw, "start_task", topic="advisory locks")

    assert (started["workspace"], started["your_role"], started["can_record"]) == ("Acme", "owner", True)
    assert [d["key"] for d in started["about_topic"]] == ["D-1"]
    assert [(d["key"], d["area"]) for d in started["accepted"]] == [("D-1", "Imports")]
    assert [d["title"] for d in started["open_questions"]] == ["Retry policy"]
    assert started["accepted"][0]["url"].endswith(f"?w={headers['X-Workspace-Id']}")


async def test_a_search_leaves_unreviewed_proposals_out_unless_asked_for(client):
    headers, raw = await connected(client)
    await decide(client, headers, title="Export format", statement="Admins export as CSV.", status="accepted")
    await decide(client, headers, title="Export limits", statement="Exports stop at 10k rows.", status="proposed")

    active = await call(client, raw, "search_decisions", query="export")
    proposed = await call(client, raw, "search_decisions", query="export", status="proposed")

    assert [d["title"] for d in active["decisions"]] == ["Export format"]
    assert [(d["title"], d["unreviewed"]) for d in proposed["decisions"]] == [("Export limits", True)]


async def test_a_decision_is_read_in_full_by_its_short_id(client):
    headers, raw = await connected(client)
    await decide(client, headers, title="Export format", statement="Admins export as CSV.", context_md="Asked for by finance.")

    read = await call(client, raw, "get_decision", key="d-1")
    missing = await call(client, raw, "get_decision", key="D-99")

    assert (read["decision"]["key"], read["decision"]["background"]) == ("D-1", "Asked for by finance.")
    assert "decision_not_found" in missing["error"]


async def test_a_tool_never_reads_another_workspace_even_by_the_same_short_id(client):
    alice, _ = await connected(client, "alice")
    bob, bobs_token = await connected(client, "bob")
    await decide(client, alice, title="Alice's secret", statement="Only for Acme of alice.")
    await decide(client, bob, title="Bob's plan", statement="Ship on Friday.")

    read = await call(client, bobs_token, "get_decision", key="D-1")
    searched = await call(client, bobs_token, "search_decisions", status="any")
    started = await call(client, bobs_token, "start_task", topic="secret")

    assert read["decision"]["title"] == "Bob's plan"
    assert [d["title"] for d in searched["decisions"]] == ["Bob's plan"]
    assert started["about_topic"] == []


async def test_a_revoked_tool_is_refused_on_its_next_call(client):
    headers, raw = await connected(client)
    [tool] = (await client.get("/ai-tools", headers=bearer(headers))).json()["data"]

    await client.delete(f"/ai-tools/{tool['id']}", headers=bearer(headers))

    assert (await rpc(client, raw, "tools/list")).status_code == 401


async def test_lookups_are_limited_per_tool(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_reads_per_connection_minute", 1)
    _, raw = await connected(client)

    await call(client, raw, "search_decisions")
    again = await call(client, raw, "search_decisions")

    assert "rate_limited" in again["error"]


LOCKS = {"title": "Import locking", "statement": "Imports take a Postgres advisory lock per workspace."}


async def inbox(client, headers: dict, tab: str = "needs_you") -> list[dict]:
    return (await client.get("/decisions", params={"tab": tab}, headers=headers)).json()["data"]


async def test_a_tool_asks_before_recording_unless_the_person_asked_for_it(client):
    headers, raw = await connected(client)

    asked = await call(client, raw, "record_decision", **LOCKS)
    assert asked["result"] == "confirmation_needed" and await inbox(client, headers) == []

    recorded = await call(client, raw, "record_decision", **LOCKS, user_approved=True, user_words="yes, lock per workspace")

    [proposal] = await inbox(client, headers)
    assert (recorded["result"], recorded["decision"]["key"], recorded["decision"]["unreviewed"]) == ("recorded", "D-1", True)
    assert (proposal["status"], proposal["origin"], proposal["created_by"]) == ("proposed", "agent", "id-alice")
    assert (proposal["capture"]["client"], proposal["capture"]["user_words"]) == ("Claude Code", "yes, lock per workspace")
    [tool] = (await client.get("/ai-tools", headers=bearer(headers))).json()["data"]
    assert tool["records_count"] == 1 and tool["last_used_at"]


async def test_a_tool_set_to_record_on_its_own_does_not_ask(client):
    headers, raw = await connected(client, ask_first=False)

    recorded = await call(client, raw, "record_decision", **LOCKS)

    assert recorded["result"] == "recorded"
    [proposal] = await inbox(client, headers)
    assert proposal["capture"]["user_approved"] is False


async def test_an_open_question_is_the_persons_to_settle(client):
    headers, raw = await connected(client)

    await call(client, raw, "record_decision", title="Retries", statement="How often do imports retry?", kind="open_question", user_approved=True)

    [question] = await inbox(client, headers)
    assert (question["status"], question["owner_id"]) == ("open", "id-alice")


async def test_the_same_wording_is_recorded_once_whoever_sends_it(client):
    headers, raw = await connected(client)
    second = (await token(client, headers, label="Codex"))["token"]

    first = await call(client, raw, "record_decision", **LOCKS, user_approved=True)
    retried = await call(client, raw, "record_decision", **LOCKS, user_approved=True)
    other = await call(client, second, "record_decision", title="import locking", statement="Imports take a Postgres  advisory lock per workspace.", user_approved=True)

    assert [first["result"], retried["result"], other["result"]] == ["recorded", "already_recorded", "already_recorded"]
    assert retried["decision"]["key"] == other["decision"]["key"] == "D-1" and len(await inbox(client, headers)) == 1


async def test_a_similar_decision_stops_a_record_until_the_person_insists(client):
    headers, raw = await connected(client)
    await decide(client, headers, title="Locks", statement="Imports take a Postgres advisory lock per workspace, always.", status="accepted")

    held = await call(client, raw, "record_decision", **LOCKS, user_approved=True)
    forced = await call(client, raw, "record_decision", **LOCKS, user_approved=True, force=True)

    assert (held["result"], [d["key"] for d in held["similar"]]) == ("similar_exists", ["D-1"])
    assert forced["result"] == "recorded"


async def test_a_dismissed_record_is_not_recorded_again(client):
    headers, raw = await connected(client)
    recorded = await call(client, raw, "record_decision", **LOCKS, user_approved=True)
    [proposal] = await inbox(client, headers)

    dismissed = await client.post(f"/decisions/{proposal['id']}/dismiss", headers=headers)
    again = await call(client, raw, "record_decision", **LOCKS, user_approved=True)

    assert recorded["result"] == "recorded" and dismissed.status_code == 200
    assert again["result"] == "previously_dismissed" and await inbox(client, headers) == []


async def test_a_record_holding_a_credential_is_refused_without_repeating_it(client):
    headers, raw = await connected(client)
    key = "ghp_" + "a1B2" * 9

    refused = await call(client, raw, "record_decision", **LOCKS, rationale=f"deploy with {key}", user_approved=True)

    assert "secret_detected" in refused["error"] and "`rationale`" in refused["error"] and key not in refused["error"]
    assert await mongo.decisions().count_documents({}) == 0


async def test_evidence_must_be_a_passage_really_in_a_page_of_the_workspace(client):
    headers, raw = await connected(client)
    prd = await page(client, headers)
    [found] = (await call(client, raw, "search_workspace", query="Export PRD"))["results"]

    misquoted = await call(client, raw, "record_decision", **LOCKS, user_approved=True, evidence=[{"kind": "page", "id": found["id"], "quote": "Guests export everything."}])
    unknown = await call(client, raw, "record_decision", **LOCKS, user_approved=True, evidence=[{"kind": "page", "id": "nope", "quote": "x"}])
    quoted = await call(client, raw, "record_decision", **LOCKS, user_approved=True, evidence=[{"kind": "page", "id": found["id"], "quote": "Guests can't export reports."}])

    assert "quote_not_in_source" in misquoted["error"] and "evidence_not_found" in unknown["error"]
    assert (found["id"], quoted["result"]) == (prd["id"], "recorded")
    [proposal] = await inbox(client, headers)
    assert [e["title"] for e in proposal["evidence"]] == ["Export PRD"]


async def test_a_read_only_tool_cannot_record(client):
    headers, raw = await connected(client, read_only=True)

    refused = await call(client, raw, "record_decision", **LOCKS, user_approved=True)

    assert "read_only" in refused["error"] and await inbox(client, headers) == []


async def test_records_are_limited_per_tool(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_writes_per_connection_hourly", 1)
    headers, raw = await connected(client)

    await call(client, raw, "record_decision", **LOCKS, user_approved=True)
    over = await call(client, raw, "record_decision", title="Retries", statement="Imports retry three times.", user_approved=True)

    assert "rate_limited" in over["error"] and len(await inbox(client, headers)) == 1


async def test_a_tools_owner_can_rename_it_and_change_whether_it_asks(client):
    headers, _ = await connected(client)
    [tool] = (await client.get("/ai-tools", headers=bearer(headers))).json()["data"]

    changed = await client.patch(f"/ai-tools/{tool['id']}", json={"label": "Laptop", "ask_first": False}, headers=bearer(headers))

    assert (changed.json()["data"]["label"], changed.json()["data"]["ask_first"]) == ("Laptop", False)
