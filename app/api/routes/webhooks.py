import hashlib
import hmac
import json
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Request

from app.core import security
from app.core.exceptions import UnauthorizedError
from app.db import mongo
from app.workers import jobs

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = structlog.get_logger("app.webhooks")


def _verify_signature(raw_body: bytes, secret: str, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


@router.post("/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    delivery_id = request.headers.get("x-github-delivery")
    event = request.headers.get("x-github-event", "")

    # The webhook secret is per-repo, so the payload is decoded only to identify
    # the repo; nothing is *processed* until the HMAC over the raw bytes verifies.
    try:
        payload = json.loads(raw_body)
        repo_full_name = payload["repository"]["full_name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise UnauthorizedError("unrecognized webhook payload", error_code="webhook_rejected")

    repo = await mongo.repos().find_one({"repo_full_name": repo_full_name})
    if repo is None or not repo.get("webhook_secret_encrypted"):
        raise UnauthorizedError("unknown repository", error_code="webhook_rejected")

    secret = security.decrypt_secret(repo["webhook_secret_encrypted"])
    if not _verify_signature(raw_body, secret, signature):
        logger.warning("webhook_bad_signature", repo=repo_full_name, delivery_id=delivery_id)
        raise UnauthorizedError("invalid webhook signature", error_code="webhook_bad_signature")

    # GitHub retries on timeout; X-GitHub-Delivery dedup keeps processing idempotent.
    if delivery_id:
        already = await mongo.webhook_deliveries().find_one({"delivery_id": delivery_id})
        if already:
            return {"status": "duplicate_ignored"}
        await mongo.webhook_deliveries().insert_one({
            "delivery_id": delivery_id,
            "repo_full_name": repo_full_name,
            "event": event,
            "received_at": datetime.now(timezone.utc),
        })

    if event != "push":
        return {"status": "ignored", "event": event}

    if payload.get("ref") != f"refs/heads/{repo['default_branch']}":
        return {"status": "ignored_branch"}

    changed: list[str] = []
    removed: list[str] = []
    for commit in payload.get("commits", []):
        changed.extend(commit.get("added", []))
        changed.extend(commit.get("modified", []))
        removed.extend(commit.get("removed", []))
    changed = sorted(set(changed))
    removed = sorted(set(removed) - set(changed))

    if changed or removed:
        # respond 200 immediately; the re-parse runs after the response is sent
        jobs.enqueue_incremental_reparse(
            background_tasks, repo["user_id"], repo_full_name, changed, removed,
        )
    return {"status": "accepted", "changed": len(changed), "removed": len(removed)}
