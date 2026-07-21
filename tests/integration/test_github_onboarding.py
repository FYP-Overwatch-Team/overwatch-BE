from pathlib import Path

import pytest
import respx
from httpx import Response

from app.db import mongo
from app.services import repo_service
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


async def test_webhook_failure_is_independent_of_clone(client, fake_clone):
    headers = await authed(client)
    with respx.mock as router:
        router.get(f"{API}/repos/{REPO}").mock(return_value=Response(200, json=repo_payload()))
        router.post(f"{API}/repos/{REPO}/hooks").mock(return_value=Response(422, json={"message": "nope"}))
        resp = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["webhook_status"] == "failed"
    assert body["parse_status"] == "pending"  # clone still succeeded; parse job enqueued
    assert len(jobs.enqueued_jobs) == 1


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
