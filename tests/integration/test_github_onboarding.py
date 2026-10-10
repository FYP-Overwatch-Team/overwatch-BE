import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.core.config import get_settings
from app.db import mongo
from app.parsing.graph_builder import node_id
from app.services import repo_service
from app.services.graph_service import get_graph_repository
from app.workers import jobs
from tests.integration.test_auth_flow import login, mock_github

REPO = "octocat/hello-world"
API = "https://api.github.com"


@pytest.fixture(autouse=True)
def clear_job_log():
    jobs.enqueued_jobs.clear()
    yield
    jobs.enqueued_jobs.clear()


@pytest.fixture
def fake_clone(monkeypatch, tmp_path):
    async def _clone(repo_full_name: str, token: str) -> Path:
        dest = tmp_path / repo_full_name.replace("/", "__")
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(repo_service, "clone_repo", _clone)
    return _clone


def repo_payload(admin: bool = True) -> dict:
    return {
        "full_name": REPO,
        "default_branch": "main",
        "private": False,
        "permissions": {"admin": admin, "push": True, "pull": True},
        "updated_at": "2026-07-01T00:00:00Z",
    }


async def authed(client) -> dict:
    access = await login(client)
    return {"Authorization": f"Bearer {access}"}


async def test_list_repos_walks_pagination(client):
    headers = await authed(client)
    page1 = [dict(repo_payload(), full_name=f"octocat/repo-{i}") for i in range(100)]
    page2 = [repo_payload()]
    with respx.mock as router:
        router.get(f"{API}/user/repos", params={"page": 1}).mock(return_value=Response(200, json=page1))
        router.get(f"{API}/user/repos", params={"page": 2}).mock(return_value=Response(200, json=page2))
        resp = await client.get("/github/repos", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["repos"]) == 101


async def test_connect_repo_happy_path(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 777}))
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["webhook_status"] == "created"
    assert body["parse_status"] == "pending"

    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert doc["webhook_id"] == 777
    assert doc["webhook_secret_encrypted"] is not None
    assert jobs.enqueued_jobs == [{"job": "initial_parse", "user_id": doc["user_id"], "repo": REPO}]


async def test_connect_without_admin_fails_clearly(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload(admin=False)))
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["error_code"] == "repo_admin_required"
    assert await mongo.repos().find_one({"repo_full_name": REPO}) is None
    assert jobs.enqueued_jobs == []


async def test_webhook_failure_is_independent_of_clone(client, fake_clone, monkeypatch):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(422, json={"message": "nope"}))
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["webhook_status"] == "failed"
    assert body["webhook_error"] == "webhook creation failed: nope"
    assert body["parse_status"] == "pending"  # clone still succeeded; parse job enqueued
    assert len(jobs.enqueued_jobs) == 1

    monkeypatch.setenv("PUBLIC_WEBHOOK_BASE_URL", "https://overwatch.ngrok.app")
    get_settings.cache_clear()
    with respx.mock as router:
        hook = router.post(f"{API}/repos/{REPO}/hooks").mock(
            return_value=Response(201, json={"id": 781})
        )
        retry = await client.post(
            "/github/repos/webhook/retry",
            json={"repo_full_name": REPO},
            headers=headers,
        )

    assert retry.status_code == 200
    assert retry.json()["webhook_status"] == "created"
    payload = json.loads(hook.calls.last.request.content)
    assert payload["config"]["url"] == "https://overwatch.ngrok.app/webhooks/github"


async def test_clone_failure_marks_parse_failed_but_webhook_ok(client, monkeypatch):
    async def _broken_clone(repo_full_name, token):
        raise RuntimeError("git clone failed with exit code 128")

    monkeypatch.setattr(repo_service, "clone_repo", _broken_clone)
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 778}))
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["webhook_status"] == "created"
    assert body["parse_status"] == "failed"
    assert jobs.enqueued_jobs == []

    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert "clone failed" in doc["parse_error"]


async def test_failed_parse_can_be_retried(client, fake_clone, tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path))
    get_settings.cache_clear()
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 782}))
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    await mongo.repos().update_one(
        {"repo_full_name": REPO}, {"$set": {"parse_status": "failed", "parse_error": "old"}},
    )

    response = await client.post(
        "/github/repos/parse/retry", json={"repo_full_name": REPO}, headers=headers,
    )

    assert response.status_code == 202
    assert response.json()["parse_status"] == "pending"
    assert jobs.enqueued_jobs[-1]["job"] == "parse_retry"
    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert doc["parse_status"] == "done"
    assert doc["parse_error"] is None


async def test_parse_retry_rejected_while_running(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 783}))
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    await mongo.repos().update_one(
        {"repo_full_name": REPO}, {"$set": {"parse_status": "in_progress"}},
    )

    response = await client.post(
        "/github/repos/parse/retry", json={"repo_full_name": REPO}, headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "parse_already_running"


async def test_disconnect_removes_webhook_and_last_repo_artifacts(client, fake_clone, tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path))
    get_settings.cache_clear()
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 784}))
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    await mongo.repos().update_one({"repo_full_name": REPO}, {"$set": {"parse_status": "done"}})
    await mongo.file_parses().insert_one({"repo_full_name": REPO, "path": "api/main.py"})
    await get_graph_repository().upsert_graph(REPO, {
        "nodes": [{"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 1, "definition_count": 1}],
        "edges": [],
    })

    with respx.mock as router:
        deleted = router.delete(f"{API}/repos/{REPO}/hooks/784").mock(return_value=Response(204))
        response = await client.request(
            "DELETE", "/github/repos/connection", json={"repo_full_name": REPO}, headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["webhook_removed"] is True
    assert deleted.called
    assert await mongo.repos().count_documents({"repo_full_name": REPO}) == 0
    assert await mongo.file_parses().count_documents({"repo_full_name": REPO}) == 0
    assert await get_graph_repository().fetch_graph(REPO) == {"nodes": [], "edges": []}
    graph_response = await client.get(f"/graph?repo_full_name={REPO}", headers=headers)
    query_response = await client.post(
        "/query/ask", json={"repo_full_name": REPO, "question": "q"}, headers=headers,
    )
    assert graph_response.status_code == 404
    assert query_response.status_code == 404


async def test_disconnect_keeps_shared_graph_for_another_connection(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 785}))
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
    first_user = await mongo.users().find_one({})
    await mongo.repos().update_one({"user_id": first_user["_id"]}, {"$set": {"parse_status": "done"}})
    await mongo.repos().insert_one({
        "user_id": "another-user", "repo_full_name": REPO, "default_branch": "main",
        "webhook_status": "created", "parse_status": "done", "connected_at": 1,
    })
    await get_graph_repository().upsert_graph(REPO, {
        "nodes": [{"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 1, "definition_count": 1}],
        "edges": [],
    })

    with respx.mock as router:
        router.delete(f"{API}/repos/{REPO}/hooks/785").mock(return_value=Response(500))
        response = await client.request(
            "DELETE", "/github/repos/connection", json={"repo_full_name": REPO}, headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["webhook_removed"] is False
    assert await mongo.repos().count_documents({"repo_full_name": REPO}) == 1
    graph = await get_graph_repository().fetch_graph(REPO)
    assert [node["name"] for node in graph["nodes"]] == ["api"]


async def test_duplicate_connect_conflicts(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 779}))
        first = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)
        second = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error_code"] == "repo_already_connected"


async def test_connected_repos_listing(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(201, json={"id": 780}))
        await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    resp = await client.get("/github/repos/connected", headers=headers)
    assert resp.status_code == 200
    repos = resp.json()["repos"]
    assert len(repos) == 1
    assert repos[0]["repo_full_name"] == REPO
