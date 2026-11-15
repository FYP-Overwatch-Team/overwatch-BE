"""AI tools connected over MCP: a personal token is tied to one workspace and stops working when access goes."""

from datetime import timedelta

from app.core.config import get_settings
from app.core.time import utcnow
from app.db import mongo
from app.mcp import grants
from tests.integration.test_workspaces import bearer, in_workspace, join, team, user


async def token(client, headers: dict, **body) -> dict:
    resp = await client.post("/ai-tools/tokens", json={"label": "Claude Code", **body}, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()["data"]


async def test_a_token_is_shown_once_and_tied_to_the_workspace_it_was_made_in(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)

    made = await token(client, in_workspace(alice, acme))

    [listed] = (await client.get("/ai-tools", headers=bearer(alice))).json()["data"]
    assert made["token"].startswith("bwp_") and made["url"].endswith("/mcp")
    assert (listed["workspace_id"], listed["label"], listed["scopes"]) == (acme, "Claude Code", ["decisions:read", "decisions:record"])
    assert "token" not in listed and made["token"].startswith(listed["token_prefix"])
    # only its hash is kept
    assert made["token"] not in str(await mongo.mcp_tokens().find().to_list(None))
    grant = await grants.authenticate(made["token"])
    assert (grant["workspace_id"], grant["user_id"], grant["role"]) == (acme, "id-alice", "owner")


async def test_a_read_only_token_cannot_record(client):
    alice = await user(client, "alice")

    made = await token(client, alice, read_only=True)

    assert made["scopes"] == ["decisions:read"]


async def test_a_token_lasts_as_long_as_asked_up_to_the_longest_allowed(client):
    alice = await user(client, "alice")

    week = await token(client, alice, expires_in_days=7)
    forever = await client.post("/ai-tools/tokens", json={"label": "CI", "expires_in_days": 5000}, headers=alice)

    assert forever.status_code == 422 and "expires_in_days" in forever.json()["meta"]["fields"]
    await mongo.mcp_grants().update_one({"_id": week["id"]}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}})
    assert await grants.authenticate(week["token"]) is None


async def test_a_revoked_tool_stops_working_and_only_its_owner_can_revoke_it(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    made = await token(client, alice)

    by_bob = await client.delete(f"/ai-tools/{made['id']}", headers=bearer(bob))
    assert by_bob.status_code == 404 and await grants.authenticate(made["token"]) is not None

    revoked = await client.delete(f"/ai-tools/{made['id']}", headers=bearer(alice))

    assert revoked.status_code == 200 and await grants.authenticate(made["token"]) is None
    assert (await client.get("/ai-tools", headers=bearer(alice))).json()["data"] == []


async def test_a_tool_stops_working_when_its_member_leaves_or_the_workspace_is_deleted(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    bobs = await token(client, in_workspace(bob, acme))
    alices = await token(client, in_workspace(alice, acme))

    await client.delete(f"/workspaces/{acme}/members/id-bob", headers=bearer(alice))
    assert await grants.authenticate(bobs["token"]) is None
    assert await grants.authenticate(alices["token"]) is not None

    await client.delete(f"/workspaces/{acme}", headers=bearer(alice))
    assert await grants.authenticate(alices["token"]) is None


async def test_a_membership_that_ended_without_a_revoke_still_shuts_the_tool_out(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    made = await token(client, in_workspace(bob, acme))

    # e.g. an account merge moved the membership to another user
    await mongo.workspace_members().delete_one({"workspace_id": acme, "user_id": "id-bob"})

    assert await grants.authenticate(made["token"]) is None


async def test_connected_tools_are_capped_per_person(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_max_connections_per_user", 1)
    alice = await user(client, "alice")
    await token(client, alice)

    second = await client.post("/ai-tools/tokens", json={"label": "Codex"}, headers=alice)

    assert second.json()["meta"]["error_code"] == "too_many_connections"
