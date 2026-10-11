import respx

from app.db import mongo, oauth_tokens
from tests.integration.test_auth_flow import auth_headers, login

JIRA_TOKENS = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}


async def authed(client) -> tuple[dict, str]:
    access = await login(client)
    user = await mongo.users().find_one({})
    return auth_headers(access), user["_id"]


async def add_repo(workspace_id: str, name: str, parse_status: str, access_status: str = "active") -> None:
    await mongo.repos().insert_one({
        "workspace_id": workspace_id, "repo_full_name": name, "parse_status": parse_status,
        "access_status": access_status, "connected_at": len(name),
    })


async def test_signed_in_user_has_nothing_connected(client):
    headers, _ = await authed(client)
    with respx.mock:  # the status endpoint must not make outbound calls
        body = (await client.get("/onboarding/status", headers=headers)).json()["data"]

    assert body == {
        "role": "owner",
        "github": {"authorized": True, "needs_reauth": False, "repos": []},
        "jira": {"connected": False, "needs_reauth": False, "project": None},
        "ready": False,
    }


async def test_full_setup_reports_repos_and_project(client):
    headers, user_id = await authed(client)
    await add_repo(user_id, "octocat/hello-world", "in_progress")
    await oauth_tokens.save(user_id, "jira", JIRA_TOKENS, cloud_id="c1")
    await mongo.jira_projects().insert_one({
        "workspace_id": user_id, "project_key": "OVR", "sync_status": "done", "status": "active", "connected_at": 1,
    })

    body = (await client.get("/onboarding/status", headers=headers)).json()["data"]

    assert body["github"]["repos"] == [{
        "repo_full_name": "octocat/hello-world", "graph_stats": None, "parse_status": "in_progress",
        "parse_error": None, "access_status": "active",
    }]
    assert body["jira"] == {
        "connected": True, "needs_reauth": False,
        "project": {"key": "OVR", "sync_status": "done", "sync_error": None},
    }
    assert body["ready"] is False  # still indexing


async def test_ready_once_a_reachable_repo_is_indexed(client):
    headers, user_id = await authed(client)
    await add_repo(user_id, "octocat/revoked", "done", access_status="revoked")
    assert (await client.get("/onboarding/status", headers=headers)).json()["data"]["ready"] is False

    await add_repo(user_id, "octocat/hello-world", "done")
    assert (await client.get("/onboarding/status", headers=headers)).json()["data"]["ready"] is True


async def test_disconnected_jira_projects_are_not_reported(client):
    headers, user_id = await authed(client)
    await mongo.jira_projects().insert_one({
        "workspace_id": user_id, "project_key": "OLD", "sync_status": "done", "status": "disconnected", "connected_at": 1,
    })
    assert (await client.get("/onboarding/status", headers=headers)).json()["data"]["jira"]["project"] is None


async def test_needs_reauth_surfaced_independently(client):
    headers, user_id = await authed(client)
    await oauth_tokens.save(user_id, "jira", JIRA_TOKENS, cloud_id="c1")
    await oauth_tokens.set_status(user_id, "jira", "needs_reauth")

    body = (await client.get("/onboarding/status", headers=headers)).json()["data"]
    assert body["jira"]["connected"] is False
    assert body["jira"]["needs_reauth"] is True
    assert body["github"]["authorized"] is True  # unaffected


async def test_status_requires_auth(client):
    assert (await client.get("/onboarding/status")).status_code == 401
