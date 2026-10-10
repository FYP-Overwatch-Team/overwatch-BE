"""Connecting repos through the GitHub App.

The user token decides what a user may connect (it only sees repos the user
can access *and* the App is installed on); the installation token is what the
background jobs clone and fetch with.
"""

import asyncio
from datetime import timedelta

import structlog
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.repo_identity import validate_repo_full_name
from app.core.time import utcnow
from app.db import oauth_tokens
from app.github import checkout, repository
from app.integrations.github_app import get_github_app

logger = structlog.get_logger("app.repos")


async def user_token(user_id: str) -> str:
    return await oauth_tokens.get_fresh_access_token(user_id, "github", get_github_app().refresh_user_token)


async def list_installations(user_id: str) -> dict:
    installations = await get_github_app().list_user_installations(await user_token(user_id))
    return {
        "install_url": f"https://github.com/apps/{get_settings().github_app_slug}/installations/new",
        "installations": [
            {
                "id": inst["id"],
                "account_login": inst["account"]["login"],
                "account_type": inst["account"]["type"],
                "repository_selection": inst.get("repository_selection"),
            }
            for inst in installations
        ],
    }


async def list_repos(user_id: str) -> list[dict]:
    """Every repo the user could connect, across all their installations."""
    github = get_github_app()
    token = await user_token(user_id)
    connected = {repo["repo_full_name"] for repo in await repository.list_for_user(user_id)}

    installations = await github.list_user_installations(token)
    # one request chain per installation, run side by side; any failure fails the whole list
    pages = await asyncio.gather(*(github.list_installation_repos(token, inst["id"]) for inst in installations))
    repos = [
        {
            "id": repo["id"],
            "full_name": repo["full_name"],
            "private": repo.get("private", False),
            "default_branch": repo.get("default_branch"),
            "installation_id": inst["id"],
            "connected": repo["full_name"] in connected,
        }
        for inst, inst_repos in zip(installations, pages)
        for repo in inst_repos
    ]
    return sorted(repos, key=lambda repo: repo["full_name"].lower())


async def connect(user_id: str, repo_full_name: str) -> dict:
    """Record the repo; the background index clones it, so this returns quickly."""
    validate_repo_full_name(repo_full_name)
    if await repository.find(user_id, repo_full_name):
        raise ConflictError("repository already connected", error_code="repo_already_connected")

    github = get_github_app()
    repo = await github.get_repo(await user_token(user_id), repo_full_name)
    installation = await github.get_repo_installation(repo_full_name) if repo else None
    if repo is None or installation is None:
        raise NotFoundError(
            "repository not found, or the Overwatch GitHub App isn't installed on it",
            error_code="repo_not_accessible",
        )

    now = utcnow()
    doc = {
        "user_id": user_id,
        "repo_full_name": repo["full_name"],
        "github_repo_id": repo["id"],
        "installation_id": installation["id"],
        "default_branch": repo.get("default_branch") or "main",
        "private": repo.get("private", False),
        "access_status": "active",
        "parse_status": "pending",
        "parse_error": None,
        "connected_at": now,
        "updated_at": now,
    }
    try:
        await repository.insert(doc)
    except DuplicateKeyError:  # a concurrent request connected it first
        raise ConflictError("repository already connected", error_code="repo_already_connected")
    return doc


async def request_index(user_id: str, repo_full_name: str) -> dict:
    """Retry a failed index. 409 while one is already queued or running."""
    await repository.get_connected(user_id, repo_full_name)
    stale_before = utcnow() - timedelta(minutes=get_settings().parse_lock_stale_minutes)
    repo = await repository.mark_pending(user_id, repo_full_name, stale_before)
    if repo is None:
        raise ConflictError("repository indexing is already running", error_code="index_already_running")
    return repo


async def disconnect(user_id: str, repo_full_name: str) -> None:
    """Forget the repo; its checkout, facts and graph go once no other user has it connected."""
    await repository.get_connected(user_id, repo_full_name)
    shared = await repository.connected_by_others(repo_full_name, user_id)
    await repository.delete(user_id, repo_full_name)
    if not shared:
        await _purge(repo_full_name)
    logger.info("repo_disconnected", repo=repo_full_name, purged=not shared)


async def _purge(repo_full_name: str) -> None:
    from app.services.facts_repository import get_facts_store
    from app.db.knowledge_graph_store import get_knowledge_graph_store

    await get_knowledge_graph_store().delete_repository(repo_full_name)
    await get_facts_store().delete_repo(repo_full_name)
    await checkout.remove(repo_full_name)

