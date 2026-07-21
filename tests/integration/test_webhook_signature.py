import hashlib
import hmac
import json

import pytest

from app.core import security
from app.db import mongo
from app.workers import jobs

REPO = "octocat/hello-world"
SECRET = "shhh-webhook-secret"


@pytest.fixture(autouse=True)
def clear_job_log():
    jobs.enqueued_jobs.clear()
    yield
    jobs.enqueued_jobs.clear()


@pytest.fixture
async def connected_repo():
    await mongo.repos().insert_one({
        "user_id": "user-1",
        "repo_full_name": REPO,
        "default_branch": "main",
        "webhook_secret_encrypted": security.encrypt_secret(SECRET),
        "webhook_status": "created",
        "parse_status": "done",
        "connected_at": 1,
        "updated_at": 1,
    })


def push_payload(**overrides) -> dict:
    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": REPO},
        "commits": [{"added": ["new.py"], "modified": ["api/handlers.py"], "removed": []}],
    }
    payload.update(overrides)
    return payload


def signed_headers(body: bytes, secret: str = SECRET, delivery: str = "delivery-1") -> dict:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "x-hub-signature-256": f"sha256={sig}",
        "x-github-delivery": delivery,
        "x-github-event": "push",
        "content-type": "application/json",
    }


async def post_webhook(client, payload: dict, headers: dict):
    return await client.post("/webhooks/github", content=json.dumps(payload).encode(), headers=headers)


async def test_valid_signature_accepted_and_reparse_enqueued(client, connected_repo):
    payload = push_payload()
    body = json.dumps(payload).encode()
    resp = await post_webhook(client, payload, signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert jobs.enqueued_jobs == [{
        "job": "incremental_reparse", "user_id": "user-1", "repo": REPO,
        "changed": ["api/handlers.py", "new.py"], "removed": [],
    }]


async def test_bad_signature_rejected_before_processing(client, connected_repo):
    payload = push_payload()
    body = json.dumps(payload).encode()
    resp = await post_webhook(client, payload, signed_headers(body, secret="wrong-secret"))
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "webhook_bad_signature"
    assert jobs.enqueued_jobs == []
    assert await mongo.webhook_deliveries().count_documents({}) == 0


async def test_missing_signature_rejected(client, connected_repo):
    payload = push_payload()
    resp = await client.post(
        "/webhooks/github", content=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-github-event": "push"},
    )
    assert resp.status_code == 401


async def test_unknown_repo_rejected(client):
    payload = push_payload(repository={"full_name": "stranger/repo"})
    body = json.dumps(payload).encode()
    resp = await post_webhook(client, payload, signed_headers(body))
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "webhook_rejected"


async def test_non_default_branch_ignored(client, connected_repo):
    payload = push_payload(ref="refs/heads/feature-x")
    body = json.dumps(payload).encode()
    resp = await post_webhook(client, payload, signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored_branch"
    assert jobs.enqueued_jobs == []


async def test_non_push_event_ignored(client, connected_repo):
    payload = push_payload()
    body = json.dumps(payload).encode()
    headers = signed_headers(body)
    headers["x-github-event"] = "ping"
    resp = await post_webhook(client, payload, headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert jobs.enqueued_jobs == []
