import json

from app.db import mongo
from app.workers import jobs
from tests.integration.test_webhook_signature import (  # noqa: F401 (fixtures)
    REPO,
    clear_job_log,
    connected_repo,
    post_webhook,
    push_payload,
    signed_headers,
)


async def test_same_delivery_id_processed_once(client, connected_repo):
    payload = push_payload()
    body = json.dumps(payload).encode()
    headers = signed_headers(body, delivery="dup-42")

    first = await post_webhook(client, payload, headers)
    second = await post_webhook(client, payload, headers)

    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_ignored"
    assert len(jobs.enqueued_jobs) == 1
    assert await mongo.webhook_deliveries().count_documents({"delivery_id": "dup-42"}) == 1


async def test_distinct_delivery_ids_both_processed(client, connected_repo):
    payload = push_payload()
    body = json.dumps(payload).encode()

    r1 = await post_webhook(client, payload, signed_headers(body, delivery="d-1"))
    r2 = await post_webhook(client, payload, signed_headers(body, delivery="d-2"))

    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "accepted"
    assert len(jobs.enqueued_jobs) == 2
