import asyncio
from datetime import timedelta

import jwt
import pytest
import respx
from httpx import Response

from app.core.time import utcnow
from app.db import mongo
from app.github import repository
from tests.integration.test_auth_flow import login

REPO = "octocat/hello-world"
API = "https://api.github.com"
INSTALLATION = {"id": 9, "account": {"login": "octocat", "type": "User"}, "repository_selection": "selected"}


def repo_payload(full_name: str = REPO, repo_id: int = 1) -> dict:
    return {"id": repo_id, "full_name": full_name, "default_branch": "main", "private": True}


@pytest.fixture
async def headers(client) -> dict:
    return {"Authorization": f"Bearer {await login(client)}"}


def mock_connect(router, *, repo=200, installation=200):
    router.get(f"{API}/repos/{REPO}").mock(
        return_value=Response(repo, json=repo_payload() if repo == 200 else {"message": "Not Found"}),
    )
    return router.get(f"{API}/repos/{REPO}/installation").mock(
        return_value=Response(installation, json=INSTALLATION if installation == 200 else {"message": "Not Found"}),
    )


async def test_installations_include_the_install_link(client, headers):
    with respx.mock as router:
        router.get(f"{API}/user/installations").mock(
            return_value=Response(200, json={"total_count": 1, "installations": [INSTALLATION]}),
        )
        resp = await client.get("/github/installations", headers=headers)

    assert resp.json()["data"] == {
        "install_url": "https://github.com/apps/overwatch-test/installations/new",
        "installations": [{"id": 9, "account_login": "octocat", "account_type": "User", "repository_selection": "selected"}],
    }


async def test_repos_are_listed_across_installations_and_pages(client, headers, record_jobs):
    page1 = [repo_payload(f"octocat/repo-{i:03}", i) for i in range(100)]
    with respx.mock as router:
        router.get(f"{API}/user/installations").mock(return_value=Response(200, json={
            "total_count": 2, "installations": [INSTALLATION, {**INSTALLATION, "id": 10}],
        }))
        router.get(f"{API}/user/installations/9/repositories", params={"page": 1}).mock(
            return_value=Response(200, json={"total_count": 101, "repositories": page1}),
        )
        router.get(f"{API}/user/installations/9/repositories", params={"page": 2}).mock(
            return_value=Response(200, json={"total_count": 101, "repositories": [repo_payload(REPO, 500)]}),
        )
        router.get(f"{API}/user/installations/10/repositories").mock(
            return_value=Response(200, json={"total_count": 1, "repositories": [repo_payload("acme/api", 600)]}),
        )
        mock_connect(router)
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
        resp = await client.get("/github/repos", headers=headers)

    repos = resp.json()["data"]
    assert len(repos) == 102
    assert repos[0]["full_name"] == "acme/api" and repos[0]["installation_id"] == 10
    assert [r["full_name"] for r in repos if r["connected"]] == [REPO]


async def test_installations_are_listed_side_by_side(client, headers):
    # installation 9 only answers once installation 10 has been asked, which a one-by-one listing never does
    asked_10 = asyncio.Event()

    async def installation_9(request):
        await asyncio.wait_for(asked_10.wait(), timeout=1)
        return Response(200, json={"total_count": 1, "repositories": [repo_payload("octocat/zeta", 1)]})

    def installation_10(request):
        asked_10.set()
        return Response(200, json={"total_count": 1, "repositories": [repo_payload("acme/api", 2)]})

    with respx.mock as router:
        router.get(f"{API}/user/installations").mock(return_value=Response(200, json={
            "total_count": 2, "installations": [INSTALLATION, {**INSTALLATION, "id": 10}],
        }))
        router.get(f"{API}/user/installations/9/repositories").mock(side_effect=installation_9)
        router.get(f"{API}/user/installations/10/repositories").mock(side_effect=installation_10)
        resp = await client.get("/github/repos", headers=headers)

    assert resp.status_code == 200
    assert [(r["full_name"], r["installation_id"]) for r in resp.json()["data"]] == [("acme/api", 10), ("octocat/zeta", 9)]


async def test_one_failing_installation_fails_the_list(client, headers):
    with respx.mock as router:
        router.get(f"{API}/user/installations").mock(return_value=Response(200, json={
            "total_count": 2, "installations": [INSTALLATION, {**INSTALLATION, "id": 10}],
        }))
        router.get(f"{API}/user/installations/9/repositories").mock(
            return_value=Response(200, json={"total_count": 1, "repositories": [repo_payload()]}),
        )
        router.get(f"{API}/user/installations/10/repositories").mock(return_value=Response(500, json={"message": "boom"}))
        resp = await client.get("/github/repos", headers=headers)

    # a partial list would read as "these are all your repos"
    assert resp.status_code >= 500


async def test_connect_records_the_installation_and_queues_a_full_index(client, headers, record_jobs, github_app_key):
    with respx.mock as router:
        installation_route = mock_connect(router)
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert resp.status_code == 201
    assert resp.json()["data"]["parse_status"] == "pending"
    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert (doc["installation_id"], doc["github_repo_id"], doc["access_status"]) == (9, 1, "active")
    assert record_jobs == [("index", doc["user_id"], REPO, True)]

    # the installation lookup is an app-level call, signed with the App's private key
    app_jwt = installation_route.calls.last.request.headers["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(app_jwt, github_app_key[0].public_key(), algorithms=["RS256"])
    assert claims["iss"] == "test-client-id"
    assert claims["exp"] - claims["iat"] <= 600


async def test_connect_rejects_a_repo_the_user_cannot_see(client, headers, record_jobs):
    with respx.mock as router:
        mock_connect(router, repo=404)
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["meta"]["error_code"] == "repo_not_accessible"
    assert record_jobs == []


async def test_connect_rejects_a_repo_without_the_app_installed(client, headers, record_jobs):
    with respx.mock as router:
        mock_connect(router, installation=404)
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    assert resp.status_code == 404
    assert await mongo.repos().count_documents({}) == 0


async def test_connecting_twice_conflicts(client, headers, record_jobs):
    with respx.mock as router:
        mock_connect(router)
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
        again = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    assert again.status_code == 409
    assert again.json()["meta"]["error_code"] == "repo_already_connected"


async def test_invalid_repo_names_are_rejected_at_the_edge(client, headers):
    resp = await client.post("/github/repos/connect", json={"repo_full_name": "../etc/passwd"}, headers=headers)
    assert resp.status_code == 422


async def _connect(client, headers):
    with respx.mock as router:
        mock_connect(router)
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)


async def test_connected_repos_report_status(client, headers, record_jobs):
    await _connect(client, headers)
    repos = (await client.get("/github/repos/connected", headers=headers)).json()["data"]
    assert repos == [{
        "repo_full_name": REPO, "default_branch": "main", "access_status": "active",
        "parse_status": "pending", "parse_error": None, "indexed_at": None, "graph_stats": None,
    }]


async def test_a_failed_index_can_be_retried(client, headers, record_jobs):
    await _connect(client, headers)
    await mongo.repos().update_one({}, {"$set": {"parse_status": "failed", "parse_error": "boom"}})
    record_jobs.clear()

    resp = await client.post(f"/github/repos/{REPO}/index", headers=headers)

    assert resp.status_code == 202
    assert resp.json()["data"]["parse_status"] == "pending" and resp.json()["data"]["parse_error"] is None
    assert len(record_jobs) == 1


async def test_retry_while_indexing_conflicts(client, headers, record_jobs):
    await _connect(client, headers)
    await mongo.repos().update_one({}, {"$set": {"parse_status": "in_progress", "parse_started_at": utcnow()}})

    resp = await client.post(f"/github/repos/{REPO}/index", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["meta"]["error_code"] == "index_already_running"


async def test_retry_for_an_unknown_repo_is_not_found(client, headers):
    resp = await client.post(f"/github/repos/{REPO}/index", headers=headers)
    assert resp.status_code == 404


async def test_stale_in_progress_index_can_be_retried(client, headers, record_jobs):
    await _connect(client, headers)
    old = utcnow() - timedelta(hours=6)
    await mongo.repos().update_one({}, {"$set": {"parse_status": "in_progress", "parse_started_at": old, "updated_at": old}})
    resp = await client.post(f"/github/repos/{REPO}/index", headers=headers)
    assert resp.status_code == 202, resp.json()


async def test_pending_left_by_a_restart_can_be_retried(client, headers, record_jobs):
    await _connect(client, headers)
    old = utcnow() - timedelta(hours=6)
    await mongo.repos().update_one({}, {"$set": {"parse_status": "pending", "updated_at": old}})
    resp = await client.post(f"/github/repos/{REPO}/index", headers=headers)
    assert resp.status_code == 202, resp.json()


async def test_concurrent_connect_is_a_conflict_not_a_500(client, headers, record_jobs, monkeypatch):
    await _connect(client, headers)
    async def none(*_a, **_k):
        return None
    monkeypatch.setattr(repository, "find", none)  # the second request's check ran before the first insert
    with respx.mock as router:
        mock_connect(router)
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    assert resp.status_code == 409, resp.json()
