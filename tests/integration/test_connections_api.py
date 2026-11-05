from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from app.db import mongo, oauth_tokens
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_jira_onboarding import mock_atlassian
from tests.integration.test_workspaces import in_workspace, join, team, user

ASSIGNABLE = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/user/assignable/search"


@pytest.fixture(autouse=True)
def scheduled(record_jobs):
    return record_jobs


async def authorize(client, headers: dict, return_to: str = "onboarding") -> Response:
    start = await client.post("/connections/jira/authorize", json={"return_to": return_to}, headers=headers)
    state = parse_qs(urlparse(start.json()["data"]["authorization_url"]).query)["state"][0]
    with respx.mock as router:
        mock_atlassian(router)
        return await client.get(f"/jira/callback?code=fake&state={state}")


async def jira(client, headers: dict) -> dict:
    catalog = (await client.get("/connections", headers=headers)).json()["data"]
    return next(c for c in catalog if c["provider"] == "jira")


async def test_the_catalog_shows_each_connector_and_its_state(client):
    headers = auth_headers(await login(client))
    assert await jira(client, headers) == {
        "provider": "jira", "name": "Jira", "category": "work_items", "status": "disconnected", "can_write": False,
        "outdated": False, "external_account": None, "connected_by": None, "connected_at": None, "targets": [],
    }

    await authorize(client, headers)
    added = await client.post("/connections/jira/targets", json={"key": "OVR", "name": "Overwatch"}, headers=headers)

    state = await jira(client, headers)
    assert added.status_code == 201
    assert (state["status"], state["can_write"], state["connected_by"]) == ("active", True, headers["X-Workspace-Id"])
    assert state["outdated"] is False
    assert state["external_account"]["url"] == "https://acme.atlassian.net"
    assert [(t["key"], t["name"], t["sync_status"]) for t in state["targets"]] == [("OVR", "Overwatch", "pending")]

    await oauth_tokens.set_status(headers["X-Workspace-Id"], "atlassian", "needs_reauth")
    assert (await jira(client, headers))["status"] == "needs_reauth"


async def test_settings_flows_land_back_in_settings(client):
    headers = auth_headers(await login(client))
    cb = await authorize(client, headers, return_to="settings")
    assert cb.headers["location"].endswith(f"/settings/integrations?jira=connected&w={headers['X-Workspace-Id']}")


async def test_only_admins_manage_connections(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    as_bob = in_workspace(bob, acme)

    for resp in (
        await client.post("/connections/jira/authorize", headers=as_bob),
        await client.post("/connections/jira/targets", json={"key": "OVR"}, headers=as_bob),
        await client.delete("/connections/jira", headers=as_bob),
        await client.post("/connections/jira/sync", headers=as_bob),
    ):
        assert resp.json()["meta"]["error_code"] == "insufficient_role"
    assert (await client.get("/connections", headers=as_bob)).status_code == 200


async def test_unknown_providers_and_missing_connections(client):
    headers = auth_headers(await login(client))
    unknown = await client.post("/connections/trello/authorize", headers=headers)
    missing = await client.post("/connections/jira/targets", json={"key": "OVR"}, headers=headers)
    assert unknown.json()["meta"]["error_code"] == "unknown_connector"
    assert missing.json()["meta"]["error_code"] == "jira_not_connected"


async def test_targets_can_be_removed_and_resynced_in_full(client, scheduled):
    headers = auth_headers(await login(client))
    ws = headers["X-Workspace-Id"]
    await authorize(client, headers)
    await client.post("/connections/jira/targets", json={"key": "OVR"}, headers=headers)
    await client.post("/connections/jira/targets", json={"key": "OPS"}, headers=headers)
    await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": "recent"}})

    removed = await client.delete("/connections/jira/targets/OPS", headers=headers)
    synced = await client.post("/connections/jira/sync", headers=headers)

    assert (removed.status_code, synced.status_code) == (200, 202)
    assert scheduled[-1] == ("sync", ws, "jira", "OVR")
    assert (await mongo.sync_targets().find_one({"key": "OVR"}))["last_full_sync_at"] is None
    assert [t["key"] for t in (await jira(client, headers))["targets"]] == ["OVR"]


async def test_assignees_come_from_jira(client):
    headers = auth_headers(await login(client))
    await authorize(client, headers)
    with respx.mock as router:
        router.get(ASSIGNABLE, params={"project": "OVR"}).mock(return_value=Response(200, json=[
            {"accountId": "acc-1", "displayName": "Sam Lee", "avatarUrls": {"24x24": "https://a/24.png"}},
        ]))
        resp = await client.get("/connections/jira/targets/OVR/assignees", headers=headers)
    assert resp.json()["data"] == [{"id": "acc-1", "name": "Sam Lee", "avatar_url": "https://a/24.png"}]


async def test_a_token_response_with_scopes_records_what_was_granted(client):
    headers = auth_headers(await login(client))
    start = await client.post("/connections/jira/authorize", headers=headers)
    state = parse_qs(urlparse(start.json()["data"]["authorization_url"]).query)["state"][0]
    with respx.mock as router:
        mock_atlassian(router)
        router.post("https://auth.atlassian.com/oauth/token").mock(return_value=Response(200, json={
            "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "read:jira-work offline_access",
        }))
        await client.get(f"/jira/callback?code=fake&state={state}")

    state = await jira(client, headers)
    assert state["can_write"] is False
    # it lacks what the board needs too, so settings and the board ask for a reconnect
    assert state["outdated"] is True


async def test_reconnecting_makes_the_next_sync_read_everything(client):
    headers = auth_headers(await login(client))
    await authorize(client, headers)
    await client.post("/connections/jira/targets", json={"key": "OVR", "name": "Overwatch"}, headers=headers)
    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"last_full_sync_at": "2026-10-25"}})

    await authorize(client, headers)

    assert (await mongo.sync_targets().find_one({"key": "OVR"}))["last_full_sync_at"] is None


async def connect(client, headers: dict, provider: str, scope: str | None = None) -> Response:
    start = await client.post(f"/connections/{provider}/authorize", json={"return_to": "settings"}, headers=headers)
    url = start.json()["data"]["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    with respx.mock as router:
        mock_atlassian(router)
        if scope is not None:
            router.post("https://auth.atlassian.com/oauth/token").mock(return_value=Response(200, json={
                "access_token": "a2", "refresh_token": "r2", "expires_in": 3600, "scope": scope,
            }))
        callback = await client.get(f"/jira/callback?code=fake&state={state}")
    callback.authorize_url = url
    return callback


async def test_confluence_shares_jiras_atlassian_grant(client):
    headers = auth_headers(await login(client))
    ws = headers["X-Workspace-Id"]
    await connect(client, headers, "jira")

    callback = await connect(client, headers, "confluence", scope="read:jira-work write:jira-work read:page:confluence write:page:confluence")

    requested = parse_qs(urlparse(callback.authorize_url).query)["scope"][0].split()
    assert {"write:jira-work", "write:page:confluence", "offline_access"} <= set(requested)  # Jira keeps its scopes
    assert callback.headers["location"].endswith(f"/settings/integrations?confluence=connected&w={ws}")
    assert await mongo.oauth_tokens().count_documents({"owner_id": ws, "provider": {"$ne": "github"}}) == 1
    catalog = {c["provider"]: c for c in (await client.get("/connections", headers=headers)).json()["data"]}
    assert (catalog["confluence"]["category"], catalog["confluence"]["status"], catalog["confluence"]["can_write"]) == ("documents", "active", True)
    # the new grant is Jira's too
    assert catalog["jira"]["status"] == "active" and "write:page:confluence" in (await mongo.connections().find_one({"provider": "jira"}))["scopes"]
    assert (await mongo.connections().find_one({"provider": "confluence"}))["external_account"]["id"] == "cloud-1"


async def test_a_jira_only_grant_never_asks_for_confluence(client):
    headers = auth_headers(await login(client))
    callback = await connect(client, headers, "jira")
    assert not any("confluence" in s for s in parse_qs(urlparse(callback.authorize_url).query)["scope"][0].split())


async def test_disconnecting_one_product_keeps_the_shared_token_for_the_other(client):
    headers = auth_headers(await login(client))
    ws = headers["X-Workspace-Id"]
    await connect(client, headers, "jira")
    await connect(client, headers, "confluence")

    await client.delete("/connections/confluence", headers=headers)
    assert (await oauth_tokens.get(ws, "atlassian"))["status"] == "active"
    assert (await jira(client, headers))["status"] == "active"

    await client.delete("/connections/jira", headers=headers)
    assert (await oauth_tokens.get(ws, "atlassian"))["status"] == "disconnected"


async def test_a_state_naming_another_apps_product_is_rejected(client):
    headers = auth_headers(await login(client))
    start = await client.post("/connections/jira/authorize", headers=headers)
    state = parse_qs(urlparse(start.json()["data"]["authorization_url"]).query)["state"][0]
    forged = state.replace(".jira.", ".github.")
    callback = await client.get(f"/jira/callback?code=fake&state={forged}")
    assert "jira=error" in callback.headers["location"]
