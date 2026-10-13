"""Local git checkouts of connected repos, kept owner-only on disk."""

import asyncio
import shutil
import stat
from pathlib import Path

import structlog

from app.core.config import get_settings
from app.core.repo_identity import InvalidRepoName, checkout_dir
from app.github import repository
from app.integrations import git_cli

logger = structlog.get_logger("app.repos")

# checkouts hold private source code: never world-readable
CHECKOUT_DIR_MODE = 0o700


def repos_root() -> Path:
    return Path(get_settings().repos_dir)


def repo_workdir(repo_full_name: str) -> Path:
    """Checkout location for a repo, validated to stay under the root."""
    return checkout_dir(repos_root(), repo_full_name)


def ensure_private_repos_root() -> Path:
    root = repos_root()
    root.mkdir(parents=True, exist_ok=True, mode=CHECKOUT_DIR_MODE)
    root.chmod(CHECKOUT_DIR_MODE)
    return root


async def clone(repo_full_name: str, token: str) -> Path:
    """Fresh shallow clone of the default branch, replacing any old checkout."""
    dest = repo_workdir(repo_full_name)
    ensure_private_repos_root()
    await remove(repo_full_name)
    await git_cli.clone(repo_full_name, token, dest)
    return dest


async def update(repo_full_name: str, token: str, *, branch: str | None = None) -> Path:
    """Bring the checkout up to date, cloning if it's missing or broken."""
    dest = repo_workdir(repo_full_name)
    if not (dest / ".git").exists():
        return await clone(repo_full_name, token)
    try:
        await git_cli.ensure_clean_remote(dest, repo_full_name)
        await git_cli.fetch_and_reset(dest, repo_full_name, token, branch=branch)
    except git_cli.GitCommandError as exc:
        logger.warning("git_update_failed_recloning", repo=repo_full_name, error=str(exc))
        return await clone(repo_full_name, token)
    return dest


async def remove(repo_full_name: str) -> None:
    dest = repo_workdir(repo_full_name)
    if dest.exists():
        await asyncio.to_thread(_rmtree, dest)


async def sanitize_existing_checkouts() -> int:
    """Strip credentials that older versions persisted into `.git/config`.

    Runs at boot. A checkout that can't be cleaned is deleted; the next sync re-clones.
    """
    if not repos_root().is_dir():
        return 0
    ensure_private_repos_root()

    cleaned = 0
    for repo_full_name in await repository.all_repo_names():
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
            await remove(repo_full_name)
            cleaned += 1
    return cleaned


def _rmtree(path: Path) -> None:
    def _on_error(func, p, _exc):
        Path(p).chmod(stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=_on_error)
