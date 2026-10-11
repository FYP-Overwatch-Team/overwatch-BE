import hashlib
import hmac
import json

import pytest

from app.db import mongo, oauth_tokens

REPO = "octocat/hello-world"
SECRET = "test-webhook-secret"  # set by the test_env fixture


@pytest.fixture(autouse=True)
def scheduled(record_jobs):
    return record_jobs


async def connect(workspace_id: str, *, repo_id: int = 1, installation_id: int = 9, branch: str = "main") -> None:
    await mongo.repos().insert_one({
        "workspace_id": workspace_id, "repo_full_name": REPO, "github_repo_id": repo_id,
        "installation_id": installation_id, "default_branch": branch,
        "access_status": "active", "parse_status": "done",
    })


def push_payload(**overrides) -> dict:
    return {
        "ref": "refs/heads/main",
        "repository": {"id": 1, "full_name": REPO},
        "installation": {"id": 9},
        **overrides,
    }


async def deliver(client, event: str, payload: dict, *, secret: str = SECRET, delivery: str | None = None):
    body = json.dumps(payload).encode()
    headers = {
        "content-type": "application/json",
        "x-github-event": event,
        "x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }
    if delivery:
        headers["x-github-delivery"] = delivery
    return await client.post("/webhooks/github", content=body, headers=headers)


async def statuses() -> dict:
    return {doc["workspace_id"]: doc["access_status"] async for doc in mongo.repos().find({})}


async def test_push_reindexes_a_repo_once_however_many_workspaces_connected_it(client, scheduled):
    await connect("alice")
    await connect("bob")

    resp = await deliver(client, "push", push_payload())

    assert resp.json()["data"] == {"outcome": "accepted", "repos": 1, "event": None}
    assert scheduled == [("index", REPO, False)]


async def test_push_to_another_branch_is_ignored(client, scheduled):
    await connect("alice")
    resp = await deliver(client, "push", push_payload(ref="refs/heads/feature"))
    assert resp.json()["data"]["outcome"] == "ignored"
    assert scheduled == []


async def test_push_for_an_unconnected_repo_is_ignored(client, scheduled):
    resp = await deliver(client, "push", push_payload(repository={"id": 404, "full_name": "x/y"}))
    assert resp.status_code == 200
    assert resp.json()["data"]["outcome"] == "ignored"


async def test_push_through_another_installation_is_ignored(client, scheduled):
    await connect("alice", installation_id=10)
    resp = await deliver(client, "push", push_payload())
    assert resp.json()["data"]["outcome"] == "ignored"
    assert scheduled == []


async def test_bad_signature_is_rejected_before_anything_runs(client, scheduled):
    await connect("alice")
    resp = await deliver(client, "push", push_payload(), secret="wrong", delivery="d-1")
    assert resp.status_code == 401
    assert resp.json()["meta"]["error_code"] == "webhook_bad_signature"
    assert scheduled == []
    assert await mongo.webhook_deliveries().count_documents({}) == 0


async def test_missing_signature_is_rejected(client):
    resp = await client.post("/webhooks/github", content=b"{}", headers={"x-github-event": "push"})
    assert resp.status_code == 401


async def test_a_redelivery_is_processed_once(client, scheduled):
    await connect("alice")
    first = await deliver(client, "push", push_payload(), delivery="dup-42")
    second = await deliver(client, "push", push_payload(), delivery="dup-42")
    third = await deliver(client, "push", push_payload(), delivery="other")

    assert first.json()["data"]["outcome"] == "accepted"
    assert second.json()["data"] == {"outcome": "duplicate_ignored", "repos": None, "event": None}
    assert third.json()["data"]["outcome"] == "accepted"
    assert len(scheduled) == 2


@pytest.mark.parametrize(("action", "expected"), [("deleted", "revoked"), ("suspend", "revoked")])
async def test_losing_the_installation_revokes_its_repos(client, action, expected):
    await connect("alice")
    await connect("bob", installation_id=10)

    await deliver(client, "installation", {"action": action, "installation": {"id": 9}})

    assert await statuses() == {"alice": expected, "bob": "active"}


async def test_unsuspending_restores_access(client):
    await connect("alice")
    await deliver(client, "installation", {"action": "suspend", "installation": {"id": 9}})
    await deliver(client, "installation", {"action": "unsuspend", "installation": {"id": 9}})
    assert await statuses() == {"alice": "active"}


async def test_repos_removed_from_the_installation_are_revoked_and_restored_when_added(client):
    await connect("alice")
    removed = {"action": "removed", "installation": {"id": 9}, "repositories_removed": [{"id": 1}]}
    await deliver(client, "installation_repositories", removed)
    assert await statuses() == {"alice": "revoked"}

    added = {"action": "added", "installation": {"id": 9}, "repositories_added": [{"id": 1}]}
    await deliver(client, "installation_repositories", added)
    assert await statuses() == {"alice": "active"}


async def test_revoked_authorization_marks_the_github_token_for_reauth(client):
    await mongo.users().insert_one({"_id": "alice", "github_id": 42, "github_login": "octocat"})
    await oauth_tokens.save("alice", "github", {"access_token": "ghu"})

    await deliver(client, "github_app_authorization", {"action": "revoked", "sender": {"id": 42}})

    assert (await oauth_tokens.get("alice", "github"))["status"] == "needs_reauth"


async def test_other_events_are_acknowledged_and_ignored(client):
    resp = await deliver(client, "ping", {"zen": "Keep it logically awesome."})
    assert resp.json()["data"] == {"outcome": "ignored", "repos": None, "event": "ping"}


async def test_reinstalling_the_app_moves_repos_to_the_new_installation(client):
    await connect("alice")
    await mongo.repos().update_one({}, {"$set": {"access_status": "revoked"}})

    resp = await deliver(client, "installation", {
        "action": "created", "installation": {"id": 42}, "repositories": [{"id": 1, "full_name": REPO}],
    })

    repo = await mongo.repos().find_one({})
    assert resp.json()["data"]["repos"] == 1
    assert (repo["installation_id"], repo["access_status"]) == (42, "active")


async def test_a_repo_added_through_a_new_installation_is_reattached(client):
    await connect("alice")
    await mongo.repos().update_one({}, {"$set": {"access_status": "revoked"}})

    await deliver(client, "installation_repositories", {
        "action": "added", "installation": {"id": 42}, "repositories_added": [{"id": 1, "full_name": REPO}],
    })

    repo = await mongo.repos().find_one({})
    assert (repo["installation_id"], repo["access_status"]) == (42, "active")


async def test_a_delivery_that_fails_can_be_redelivered(client, monkeypatch):
    from app.github import repository

    await connect("alice")
    original = repository.set_access_status

    async def broken(*_args, **_kwargs):
        raise RuntimeError("mongo blip")

    monkeypatch.setattr(repository, "set_access_status", broken)
    payload = {"action": "deleted", "installation": {"id": 9}}
    assert (await deliver(client, "installation", payload, delivery="d-1")).status_code == 500

    monkeypatch.setattr(repository, "set_access_status", original)
    resp = await deliver(client, "installation", payload, delivery="d-1")

    assert resp.json()["data"]["outcome"] == "accepted"
    assert await statuses() == {"alice": "revoked"}
