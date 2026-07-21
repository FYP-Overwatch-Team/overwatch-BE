import asyncio
import secrets
from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.core import security
from app.core.config import get_settings
from app.core.exceptions import ConflictError, ForbiddenError, UnauthorizedError
from app.db import mongo
from app.integrations.github_client import get_github_client

logger = structlog.get_logger("app.repos")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_github_token(user_id: str) -> str:
    doc = await mongo.oauth_tokens().find_one({"user_id": user_id, "provider": "github"})
    if doc is None or doc.get("status") == "needs_reauth":
        raise UnauthorizedError("GitHub connection needs re-authorization", error_code="github_needs_reauth")
    return security.decrypt_secret(doc["access_token_encrypted"])


def repo_workdir(repo_full_name: str) -> Path:
    return Path(get_settings().repos_dir) / repo_full_name.replace("/", "__")


async def clone_repo(repo_full_name: str, token: str) -> Path:
    """Shallow-clone the default branch; wipes any previous checkout first."""
    dest = repo_workdir(repo_full_name)
    if dest.exists():
        await asyncio.to_thread(_rmtree, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, str(dest),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # never echo stderr verbatim — the clone URL embeds the token
        raise RuntimeError(f"git clone failed with exit code {proc.returncode}")
    return dest


async def update_workdir(repo_full_name: str, token: str) -> Path:
    """Bring the shallow checkout up to date; falls back to a fresh clone."""
    dest = repo_workdir(repo_full_name)
    if not (dest / ".git").exists():
        return await clone_repo(repo_full_name, token)
    for args in (["fetch", "--depth", "1", "origin"], ["reset", "--hard", "FETCH_HEAD"]):
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(dest), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            return await clone_repo(repo_full_name, token)
    return dest


def _rmtree(path: Path) -> None:
    import shutil
    import stat

    def _on_error(func, p, _exc):
        Path(p).chmod(stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=_on_error)


async def connect_repo(user_id: str, repo_full_name: str) -> dict:
    """Register webhook + clone + enqueue parse for a repo. Statuses tracked independently."""
    existing = await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})
    if existing:
        raise ConflictError("repository already connected", error_code="repo_already_connected")

    token = await get_github_token(user_id)
    client = get_github_client()
    repo_info = await client.get_repo(token, repo_full_name)
    if not repo_info.get("permissions", {}).get("admin"):
        raise ForbiddenError(
            "admin access to the repository is required to register a webhook",
            error_code="repo_admin_required",
        )

    now = _now()
    doc = {
        "user_id": user_id,
        "repo_full_name": repo_full_name,
        "default_branch": repo_info.get("default_branch", "main"),
        "webhook_id": None,
        "webhook_secret_encrypted": None,
        "webhook_status": "pending",
        "parse_status": "pending",
        "parse_error": None,
        "connected_at": now,
        "updated_at": now,
    }
    await mongo.repos().insert_one(doc)

    webhook_secret = secrets.token_hex(32)
    try:
        webhook_id = await client.create_push_webhook(
            token,
            repo_full_name,
            f"{get_settings().app_base_url}/webhooks/github",
            webhook_secret,
        )
        await _update_repo(user_id, repo_full_name, {
            "webhook_id": webhook_id,
            "webhook_secret_encrypted": security.encrypt_secret(webhook_secret),
            "webhook_status": "created",
        })
    except Exception:
        logger.exception("webhook_registration_failed", repo=repo_full_name)
        await _update_repo(user_id, repo_full_name, {"webhook_status": "failed"})

    try:
        await clone_repo(repo_full_name, token)
    except Exception as exc:
        logger.exception("initial_clone_failed", repo=repo_full_name)
        await _update_repo(user_id, repo_full_name, {
            "parse_status": "failed",
            "parse_error": f"clone failed: {exc}",
        })

    return await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})


async def _update_repo(user_id: str, repo_full_name: str, fields: dict) -> None:
    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
        {"$set": {**fields, "updated_at": _now()}},
    )
