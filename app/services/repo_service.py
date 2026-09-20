import asyncio
import secrets
from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.core import security
from app.core.config import get_settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.repo_identity import InvalidRepoName, checkout_dir, validate_repo_full_name
from app.db import mongo
from app.integrations import git_cli
from app.integrations.github_client import get_github_client

logger = structlog.get_logger("app.repos")

# Checkouts hold private source code: owner-only, never world-readable.
CHECKOUT_DIR_MODE = 0o700


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_github_token(user_id: str) -> str:
    doc = await mongo.oauth_tokens().find_one({"user_id": user_id, "provider": "github"})
    if doc is None or doc.get("status") == "needs_reauth":
        raise UnauthorizedError("GitHub connection needs re-authorization", error_code="github_needs_reauth")
    return security.decrypt_secret(doc["access_token_encrypted"])


def repos_root() -> Path:
    return Path(get_settings().repos_dir)


def repo_workdir(repo_full_name: str) -> Path:
    """Checkout location for a repository, validated to stay under the root."""
    return checkout_dir(repos_root(), repo_full_name)


def ensure_private_repos_root() -> Path:
    """Create the checkout root owner-only, tightening it if it already exists."""
    root = repos_root()
    root.mkdir(parents=True, exist_ok=True, mode=CHECKOUT_DIR_MODE)
    root.chmod(CHECKOUT_DIR_MODE)
    return root


async def clone_repo(repo_full_name: str, token: str) -> Path:
    """Shallow-clone the default branch; wipes any previous checkout first."""
    dest = repo_workdir(repo_full_name)
    ensure_private_repos_root()
    if dest.exists():
        await asyncio.to_thread(_rmtree, dest)
    await git_cli.clone(repo_full_name, token, dest)
    return dest


async def update_workdir(repo_full_name: str, token: str, *, branch: str | None = None) -> Path:
    """Bring the shallow checkout up to date; falls back to a fresh clone."""
    dest = repo_workdir(repo_full_name)
    if not (dest / ".git").exists():
        return await clone_repo(repo_full_name, token)
    try:
        await git_cli.ensure_clean_remote(dest, repo_full_name)
        await git_cli.fetch_and_reset(dest, repo_full_name, token, branch=branch)
    except git_cli.GitCommandError as exc:
        logger.warning("git_update_failed_recloning", repo=repo_full_name, error=str(exc))
        return await clone_repo(repo_full_name, token)
    return dest


async def sanitize_existing_checkouts() -> int:
    """Strip credentials that earlier versions persisted into `.git/config`.

    Runs once at boot. A checkout that cannot be cleaned is deleted rather
    than left holding a usable token; the next sync re-clones it.
    """
    if not repos_root().is_dir():
        return 0
    ensure_private_repos_root()

    cleaned = 0
    for repo_full_name in await mongo.repos().distinct("repo_full_name"):
        try:
            dest = repo_workdir(repo_full_name)
        except InvalidRepoName:
            continue
        if not (dest / ".git").exists():
            continue
        try:
            if await git_cli.ensure_clean_remote(dest, repo_full_name):
                cleaned += 1
        except Exception:
            logger.exception("checkout_sanitize_failed_discarding", repo=repo_full_name)
            await asyncio.to_thread(_rmtree, dest)
            cleaned += 1
    return cleaned


def _rmtree(path: Path) -> None:
    import shutil
    import stat

    def _on_error(func, p, _exc):
        Path(p).chmod(stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=_on_error)


async def register_push_webhook(user_id: str, repo_full_name: str) -> dict:
    repo = await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})
    if repo is None:
        raise NotFoundError("repository is not connected", error_code="repo_not_connected")
    if repo.get("webhook_status") == "created" and repo.get("webhook_id"):
        return repo

    token = await get_github_token(user_id)
    settings = get_settings()
    public_base_url = settings.public_webhook_base_url or settings.app_base_url
    callback_url = f"{public_base_url.rstrip('/')}/webhooks/github"
    webhook_secret = secrets.token_hex(32)

    try:
        webhook_id = await get_github_client().create_push_webhook(
            token, repo_full_name, callback_url, webhook_secret,
        )
    except Exception as exc:
        await _update_repo(user_id, repo_full_name, {
            "webhook_status": "failed",
            "webhook_error": str(exc),
        })
        raise

    await _update_repo(user_id, repo_full_name, {
        "webhook_id": webhook_id,
        "webhook_secret_encrypted": security.encrypt_secret(webhook_secret),
        "webhook_status": "created",
        "webhook_error": None,
    })
    return await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})


async def connect_repo(user_id: str, repo_full_name: str) -> dict:
    """Register webhook + clone + enqueue parse for a repo. Statuses tracked independently."""
    validate_repo_full_name(repo_full_name)
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
        "webhook_error": None,
        "parse_status": "pending",
        "parse_error": None,
        "connected_at": now,
        "updated_at": now,
    }
    await mongo.repos().insert_one(doc)

    try:
        await register_push_webhook(user_id, repo_full_name)
    except Exception as exc:
        logger.exception("webhook_registration_failed", repo=repo_full_name, error=str(exc))

    try:
        await clone_repo(repo_full_name, token)
    except Exception as exc:
        logger.exception("initial_clone_failed", repo=repo_full_name)
        await _update_repo(user_id, repo_full_name, {
            "parse_status": "failed",
            "parse_error": f"clone failed: {exc}",
        })

    return await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})


async def disconnect_repo(user_id: str, repo_full_name: str) -> None:
    """Disconnect a repository and leave nothing of it behind.

    Source code, extracted facts and the graph all exist only because the
    repository was connected, so disconnecting removes every one of them, plus
    the webhook on GitHub. Best-effort where GitHub is concerned: a webhook we
    cannot delete must not prevent us deleting our own copy of the data.
    """
    validate_repo_full_name(repo_full_name)
    repo = await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})
    if repo is None:
        raise NotFoundError("repository is not connected", error_code="repo_not_connected")

    if repo.get("webhook_id"):
        try:
            token = await get_github_token(user_id)
            await get_github_client().delete_webhook(token, repo_full_name, repo["webhook_id"])
        except Exception:
            logger.warning("webhook_delete_failed", repo=repo_full_name, exc_info=True)

    still_connected = await mongo.repos().count_documents(
        {"repo_full_name": repo_full_name, "user_id": {"$ne": user_id}},
    )
    await mongo.repos().delete_one({"user_id": user_id, "repo_full_name": repo_full_name})

    # Facts, graph and checkout are keyed by repository, not by user, so they
    # are only removed once nobody else has it connected.
    if not still_connected:
        await _purge_repository_data(repo_full_name)

    logger.info("repo_disconnected", repo=repo_full_name, purged=not still_connected)


async def _purge_repository_data(repo_full_name: str) -> None:
    from app.services.facts_repository import get_facts_store
    from app.services.knowledge_graph_store import get_knowledge_graph_store

    await get_knowledge_graph_store().delete_repository(repo_full_name)
    await get_facts_store().delete_repo(repo_full_name)

    workdir = repo_workdir(repo_full_name)
    if workdir.exists():
        await asyncio.to_thread(_rmtree, workdir)


async def _update_repo(user_id: str, repo_full_name: str, fields: dict) -> None:
    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
        {"$set": {**fields, "updated_at": _now()}},
    )
