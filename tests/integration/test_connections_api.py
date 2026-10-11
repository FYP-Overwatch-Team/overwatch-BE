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
        "provider": "jira", "name": "Jira", "status": "disconnected", "can_write": False,
        "external_account": None, "connected_by": None, "connected_at": None, "targets": [],
    }

    await authorize(client, headers)
    added = await client.post("/connections/jira/targets", json={"key": "OVR", "name": "Overwatch"}, headers=headers)

    state = await jira(client, headers)
    assert added.status_code == 201
    assert (state["status"], state["can_write"], state["connected_by"]) == ("active", True, headers["X-Workspace-Id"])
    assert state["external_account"]["url"] == "https://acme.atlassian.net"
    assert [(t["key"], t["name"], t["sync_status"]) for t in state["targets"]] == [("OVR", "Overwatch", "pending")]

    await oauth_tokens.set_status(headers["X-Workspace-Id"], "jira", "needs_reauth")
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

    assert (await jira(client, headers))["can_write"] is False
