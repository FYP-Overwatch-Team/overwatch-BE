from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from app.db import mongo
from app.workers import jobs
from tests.integration.test_auth_flow import login

TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
PROJECT_SEARCH = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/project/search"


@pytest.fixture(autouse=True)
def clear_job_log():
    jobs.enqueued_jobs.clear()
    yield
    jobs.enqueued_jobs.clear()


def mock_atlassian(router: respx.Router):
    router.post(TOKEN_URL).mock(return_value=Response(200, json={
        "access_token": "jira-access", "refresh_token": "jira-refresh", "expires_in": 3600,
    }))
    router.get(RESOURCES_URL).mock(return_value=Response(200, json=[
        {"id": "cloud-1", "name": "acme", "url": "https://acme.atlassian.net"},
    ]))


async def connect_jira(client, headers) -> None:
    login_resp = await client.post("/jira/login", headers=headers)
    assert login_resp.status_code == 200
    authorize_url = login_resp.json()["authorization_url"]
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    with respx.mock as router:
        mock_atlassian(router)
        cb = await client.get(f"/jira/callback?code=fake&state={state}")
    assert cb.status_code == 307
    assert cb.headers["location"].endswith("/connect?jira=connected")


async def test_jira_oauth_flow_stores_cloud_id(client):
    access = await login(client)
    headers = {"Authorization": f"Bearer {access}"}
    await connect_jira(client, headers)

    doc = await mongo.oauth_tokens().find_one({"provider": "jira"})
    assert doc is not None
    assert doc["cloud_id"] == "cloud-1"
    assert doc["status"] == "active"


async def test_jira_state_mismatch_rejected(client):
    access = await login(client)
    await client.post("/jira/login", headers={"Authorization": f"Bearer {access}"})
    resp = await client.get("/jira/callback?code=x&state=user.wrong")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "oauth_state_mismatch"


async def test_jira_denial_returns_to_connect(client):
    access = await login(client)
    start = await client.post("/jira/login", headers={"Authorization": f"Bearer {access}"})
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]

    response = await client.get(f"/jira/callback?error=access_denied&state={state}")

    assert response.status_code == 307
    assert response.headers["location"].endswith("/connect?jira=denied")


async def test_jira_login_requires_auth(client):
    response = await client.post("/jira/login")
    assert response.status_code == 401


async def test_list_projects(client):
    access = await login(client)
    headers = {"Authorization": f"Bearer {access}"}
    await connect_jira(client, headers)

    with respx.mock as router:
        router.get(PROJECT_SEARCH).mock(return_value=Response(200, json={
            "values": [{"key": "OVR", "name": "Overwatch", "id": "10001"}],
        }))
        resp = await client.get("/jira/projects", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["projects"] == [{"key": "OVR", "name": "Overwatch", "id": "10001"}]


async def test_disconnect_soft_deletes_connection_and_allows_reconnect(client):
    access = await login(client)
    headers = {"Authorization": f"Bearer {access}"}
    await connect_jira(client, headers)
    connected = await client.post(
        "/jira/projects/connect",
        json={"project_key": "OVR"},
        headers=headers,
    )
    assert connected.status_code == 201
    user = await mongo.users().find_one({})
    await mongo.tickets().insert_one({
        "user_id": user["_id"], "project_key": "OVR", "ticket_key": "OVR-1",
    })

    response = await client.delete("/jira/connection", headers=headers)

    assert response.status_code == 200
    token = await mongo.oauth_tokens().find_one({"user_id": user["_id"], "provider": "jira"})
    project = await mongo.jira_projects().find_one({"user_id": user["_id"], "project_key": "OVR"})
    assert token["status"] == "disconnected"
    assert project["status"] == "disconnected"
    assert project["disconnected_at"] is not None
    assert await mongo.tickets().count_documents({"user_id": user["_id"]}) == 1

    tickets = await client.get("/tickets", headers=headers)
    assert tickets.json()["tickets"] == []

    await connect_jira(client, headers)
    reconnected = await client.post(
        "/jira/projects/connect",
        json={"project_key": "OVR"},
        headers=headers,
    )
    assert reconnected.status_code == 201
    project = await mongo.jira_projects().find_one({"user_id": user["_id"], "project_key": "OVR"})
    assert project["status"] == "active"
    assert project["disconnected_at"] is None


async def test_connect_project_enqueues_sync(client):
    access = await login(client)
    headers = {"Authorization": f"Bearer {access}"}
    await connect_jira(client, headers)

    resp = await client.post(
        "/jira/projects/connect",
        json={"project_key": "OVR"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["sync_status"] == "pending"

    doc = await mongo.jira_projects().find_one({"project_key": "OVR"})
    assert doc["cloud_id"] == "cloud-1"
    assert {"job": "ticket_sync", "user_id": doc["user_id"], "project": "OVR"} in jobs.enqueued_jobs

    dup = await client.post(
        "/jira/projects/connect",
        json={"project_key": "OVR"},
        headers=headers,
    )
    assert dup.status_code == 409
