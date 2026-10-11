"""GitHub App webhook: one endpoint and one secret for every installation.

The signature is checked over the raw body before anything is parsed. GitHub
retries on timeouts, so each delivery id is processed once.
"""

import hashlib
import hmac
import json

import structlog
from fastapi import BackgroundTasks, Request
from pymongo.errors import DuplicateKeyError

from app.auth.repository import find_user_by_github_id
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.responses import api_router
from app.core.time import utcnow
from app.db import mongo, oauth_tokens
from app.github import repository
from app.github.schemas import WebhookResult
from app.workers import jobs

router = api_router(prefix="/webhooks", tags=["webhooks"])
logger = structlog.get_logger("app.webhooks")

INSTALLATION_STATUS = {"deleted": "revoked", "suspend": "revoked", "unsuspend": "active"}


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    secret = get_settings().github_webhook_secret
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


@router.post("/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> WebhookResult:
    raw_body = await request.body()
    delivery_id = request.headers.get("x-github-delivery")
    event = request.headers.get("x-github-event", "")

    if not verify_signature(raw_body, request.headers.get("x-hub-signature-256")):
        logger.warning("webhook_bad_signature", delivery_id=delivery_id, github_event=event)
        raise UnauthorizedError("invalid webhook signature", error_code="webhook_bad_signature")

    if delivery_id and not await _first_delivery(delivery_id, event):
        return {"outcome": "duplicate_ignored"}

    try:
        return await _dispatch(event, json.loads(raw_body), background_tasks)
    except Exception:
        # forget the delivery so GitHub's redelivery is processed instead of ignored
        if delivery_id:
            await mongo.webhook_deliveries().delete_one({"delivery_id": delivery_id})
        raise


async def _dispatch(event: str, payload: dict, background_tasks: BackgroundTasks) -> dict:
    match event:
        case "push":
            return await _on_push(payload, background_tasks)
        case "installation":
            return await _on_installation(payload)
        case "installation_repositories":
            return await _on_repo_selection(payload)
        case "github_app_authorization":
            return await _on_authorization(payload)
    return {"outcome": "ignored", "event": event}


async def _first_delivery(delivery_id: str, event: str) -> bool:
    """Record the delivery; False if it was already seen (the unique index makes this atomic)."""
    try:
        await mongo.webhook_deliveries().insert_one(
            {"delivery_id": delivery_id, "event": event, "received_at": utcnow()},
        )
    except DuplicateKeyError:
        return False
    return True


async def _on_push(payload: dict, background_tasks: BackgroundTasks) -> dict:
    repos = await repository.find_by_github_repo(payload["repository"]["id"], payload["installation"]["id"])
    # every workspace's connection shares one graph, so the repo is indexed once
    targets = sorted({
        repo["repo_full_name"]
        for repo in repos
        if payload.get("ref") == f"refs/heads/{repo['default_branch']}"
    })
    if not targets:
        return {"outcome": "ignored"}
    for repo_full_name in targets:
        background_tasks.add_task(jobs.run_index, repo_full_name)
    return {"outcome": "accepted", "repos": len(targets)}


async def _on_installation(payload: dict) -> dict:
    installation_id = payload["installation"]["id"]
    if payload["action"] == "created":
        # a reinstall gets a new installation id; repos connected through the old one move over
        updated = await repository.reattach(installation_id, _repo_ids(payload.get("repositories")))
    else:
        status = INSTALLATION_STATUS.get(payload["action"])
        if status is None:
            return {"outcome": "ignored"}
        updated = await repository.set_access_status(installation_id, status)
    logger.info("installation_access_changed", action=payload["action"], repos=updated)
    return {"outcome": "accepted", "repos": updated}


async def _on_repo_selection(payload: dict) -> dict:
    action = payload["action"]
    repo_ids = _repo_ids(payload.get(f"repositories_{action}"))
    if action not in ("added", "removed") or not repo_ids:
        return {"outcome": "ignored"}
    installation_id = payload["installation"]["id"]
    if action == "added":
        updated = await repository.reattach(installation_id, repo_ids)
    else:
        updated = await repository.set_access_status(installation_id, "revoked", github_repo_ids=repo_ids)
    return {"outcome": "accepted", "repos": updated}


def _repo_ids(repos: list[dict] | None) -> list[int]:
    return [repo["id"] for repo in repos or []]


async def _on_authorization(payload: dict) -> dict:
    """The user revoked the App's access to their account: their GitHub token is dead."""
    if payload["action"] != "revoked":
        return {"outcome": "ignored"}
    user = await find_user_by_github_id(payload["sender"]["id"])
    if user:
        await oauth_tokens.set_status(user["_id"], "github", "needs_reauth")
    return {"outcome": "accepted"}
