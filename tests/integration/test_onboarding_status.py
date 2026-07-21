import respx

from app.db import mongo
from app.services import jira_service
from tests.integration.test_auth_flow import login


async def authed(client) -> dict:
    access = await login(client)
    return {"Authorization": f"Bearer {access}"}


async def test_github_only_partial_setup(client):
    headers = await authed(client)
    with respx.mock:  # status endpoint must not make outbound calls
        resp = await client.get("/onboarding/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["github"]["connected"] is True
    assert body["github"]["repo_connected"] is False
    assert body["github"]["repo_full_name"] is None
    assert body["github"]["parse_status"] is None
    assert body["jira"]["connected"] is False
    assert body["jira"]["sync_status"] is None


async def test_full_setup_reports_both_statuses(client):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await mongo.repos().insert_one({
        "user_id": user["_id"], "repo_full_name": "octocat/hello-world",
        "webhook_status": "created", "parse_status": "in_progress", "connected_at": 1,
    })
    await jira_service.store_tokens(
        user["_id"], {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, cloud_id="c1",
    )
    await mongo.jira_projects().insert_one({
        "user_id": user["_id"], "project_key": "OVR", "sync_status": "done", "connected_at": 1,
    })

    resp = await client.get("/onboarding/status", headers=headers)
    body = resp.json()
    assert body["github"] == {
        "connected": True, "needs_reauth": False, "repo_connected": True,
        "repo_full_name": "octocat/hello-world", "parse_status": "in_progress",
        "webhook_status": "created", "webhook_error": None,
    }
    assert body["jira"] == {
        "connected": True, "needs_reauth": False, "project_connected": True, "sync_status": "done",
    }


async def test_needs_reauth_surfaced_independently(client):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await jira_service.store_tokens(
        user["_id"], {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, cloud_id="c1",
    )
    await mongo.oauth_tokens().update_one(
        {"user_id": user["_id"], "provider": "jira"}, {"$set": {"status": "needs_reauth"}},
    )

    resp = await client.get("/onboarding/status", headers=headers)
    body = resp.json()
    assert body["jira"]["connected"] is False
    assert body["jira"]["needs_reauth"] is True
    assert body["github"]["connected"] is True  # unaffected


async def test_status_requires_auth(client):
    resp = await client.get("/onboarding/status")
    assert resp.status_code == 401
