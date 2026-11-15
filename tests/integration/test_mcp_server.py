"""The MCP server: an AI tool signs in with its token and reads the one workspace the token is tied to."""

import asyncio
import json

import pytest

from tests.integration.test_ai_tools_api import token
from tests.integration.test_decisions_api import decide
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
