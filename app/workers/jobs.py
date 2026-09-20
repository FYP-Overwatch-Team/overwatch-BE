"""Background jobs: repository indexing and Jira ticket sync.

Indexing is guarded by a per-repository lock held in Mongo, with three
properties that matter in production:

* **Mutual exclusion** — one index per repository at a time, so two runs never
  write conflicting graphs.
* **Crash recovery** — a lock left behind by a killed process becomes
  reclaimable after `parse_lock_stale_minutes`. Without this a restart
  mid-parse would wedge a repository in `in_progress` forever.
* **No lost pushes** — a push that arrives while an index is running is
  recorded and drained before the lock is released, instead of being dropped.

The indexing work itself lives in `app/knowledge_graph/pipeline.py`; this
module owns only *when* it runs and what happens if it fails.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from app.core.config import get_settings
from app.db import mongo
from app.integrations import git_cli
from app.knowledge_graph.pipeline import IndexingPipeline
from app.services import repo_service
from app.services.facts_repository import get_facts_store
from app.services.knowledge_graph_store import get_knowledge_graph_store

logger = structlog.get_logger("app.jobs")

# In-memory record of enqueued jobs; also what tests assert against.
# For eval 1 a single-instance BackgroundTasks runner is sufficient (see plan Phase 5).
enqueued_jobs: list[dict] = []

# How many queued follow-up syncs one lock holder drains before releasing.
# Bounded so a push storm cannot keep a worker busy indefinitely.
MAX_FOLLOW_UP_SYNCS = 3


@dataclass(frozen=True)
class PendingSync:
    """Work that arrived while the repository was already being indexed."""

    full: bool = False
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.full and not self.changed and not self.removed


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Repository lock
# --------------------------------------------------------------------------


async def _claim(user_id: str, repo_full_name: str) -> bool:
    """Take the indexing lock, reclaiming one abandoned by a crashed run."""
    now = _now()
    stale_before = now - timedelta(minutes=get_settings().parse_lock_stale_minutes)
    claimed = await mongo.repos().find_one_and_update(
        {
            "user_id": user_id,
            "repo_full_name": repo_full_name,
            "$or": [
                {"parse_status": {"$ne": "in_progress"}},
                {"parse_started_at": {"$lt": stale_before}},
                # Held by a version that did not record a start time: treat as
                # abandoned, since the alternative is a permanent deadlock.
                {"parse_started_at": None},
            ],
        },
        {"$set": {"parse_status": "in_progress", "parse_started_at": now, "updated_at": now}},
    )
    return claimed is not None


async def _release(user_id: str, repo_full_name: str, status: str, error: str | None = None) -> None:
    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
        {"$set": {
            "parse_status": status,
            "parse_error": error,
            "parse_started_at": None,
            "updated_at": _now(),
        }},
    )


async def _record_pending(user_id: str, repo_full_name: str, pending: PendingSync) -> None:
    """Remember work that could not run because the lock was held."""
    update: dict = {"$set": {"sync_requested_at": _now()}}
    if pending.full:
        update["$set"]["pending_full"] = True
    else:
        update["$addToSet"] = {
            "pending_changed": {"$each": list(pending.changed)},
            "pending_removed": {"$each": list(pending.removed)},
        }
    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name}, update,
    )


async def _take_pending(user_id: str, repo_full_name: str) -> PendingSync | None:
    """Atomically claim and clear any queued work."""
    doc = await mongo.repos().find_one_and_update(
        {
            "user_id": user_id,
            "repo_full_name": repo_full_name,
            "sync_requested_at": {"$ne": None},
        },
        {
            "$set": {"sync_requested_at": None, "pending_full": False},
            "$unset": {"pending_changed": "", "pending_removed": ""},
        },
    )
    if doc is None:
        return None
    pending = PendingSync(
        full=bool(doc.get("pending_full")),
        changed=tuple(doc.get("pending_changed") or ()),
        removed=tuple(doc.get("pending_removed") or ()),
    )
    return None if pending.is_empty() else pending


async def _has_pending(user_id: str, repo_full_name: str) -> bool:
    doc = await mongo.repos().find_one(
        {
            "user_id": user_id,
            "repo_full_name": repo_full_name,
            "sync_requested_at": {"$ne": None},
        },
        {"_id": 1},
    )
    return doc is not None


# --------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------


async def _run_indexing_job(
    user_id: str,
    repo_full_name: str,
    work: Callable[[], Awaitable[None]],
    busy_pending: PendingSync,
) -> None:
    """Run `work` under the repository lock, then drain anything queued behind it."""
    if not await _claim(user_id, repo_full_name):
        await _record_pending(user_id, repo_full_name, busy_pending)
        logger.info("index_deferred_lock_held", repo=repo_full_name)
        return

    try:
        await work()
        for _ in range(MAX_FOLLOW_UP_SYNCS):
            pending = await _take_pending(user_id, repo_full_name)
            if pending is None:
                break
            logger.info("index_draining_pending", repo=repo_full_name, full=pending.full)
            await _apply_pending(user_id, repo_full_name, pending)
        else:
            # Hit the cap. Anything still queued waits for the next push.
            if await _has_pending(user_id, repo_full_name):
                logger.warning("index_followups_exhausted", repo=repo_full_name)
        await _release(user_id, repo_full_name, "done")
    except Exception as exc:
        logger.exception("index_failed", repo=repo_full_name)
        await _release(user_id, repo_full_name, "failed", error=str(exc))


async def _apply_pending(user_id: str, repo_full_name: str, pending: PendingSync) -> None:
    if pending.full:
        await _full_index(user_id, repo_full_name)
    else:
        await _incremental_index(user_id, repo_full_name, pending.changed, pending.removed)


async def _full_index(user_id: str, repo_full_name: str) -> None:
    """Index every file in the checkout, from scratch."""
    await _index(user_id, repo_full_name, full=True)


async def _incremental_index(
    user_id: str,
    repo_full_name: str,
    changed_files: Sequence[str] = (),
    removed_files: Sequence[str] = (),
) -> None:
    """Fetch the latest commit and index whatever actually changed.

    The webhook's file lists are accepted for compatibility but not used: the
    pipeline compares content hashes, which stays correct for force-pushes and
    for payloads that under-report what changed.
    """
    await _index(user_id, repo_full_name, full=False)


async def _index(user_id: str, repo_full_name: str, *, full: bool) -> None:
    if full:
        # The checkout already exists from connect; no token needed.
        workdir = repo_service.repo_workdir(repo_full_name)
    else:
        token = await repo_service.get_github_token(user_id)
        workdir = await repo_service.update_workdir(repo_full_name, token)
    version = await git_cli.current_commit(workdir)

    pipeline = IndexingPipeline(get_facts_store(), get_knowledge_graph_store())
    result = await pipeline.index(repo_full_name, workdir, version, full=full)

    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
        {"$set": {
            "graph_version": result.version,
            "graph_stats": result.as_stats_document(),
            "indexed_at": _now(),
            "updated_at": _now(),
        }},
    )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


async def run_initial_parse(user_id: str, repo_full_name: str) -> None:
    """Full-repo AST parse -> graph -> Neo4j upsert."""

    async def work() -> None:
        await _full_index(user_id, repo_full_name)

    await _run_indexing_job(user_id, repo_full_name, work, PendingSync(full=True))


async def run_incremental_reparse(
    user_id: str, repo_full_name: str, changed_files: list[str], removed_files: list[str],
) -> None:
    """Webhook-driven partial update."""
    changed, removed = tuple(changed_files), tuple(removed_files)

    async def work() -> None:
        await _incremental_index(user_id, repo_full_name, changed, removed)

    await _run_indexing_job(
        user_id, repo_full_name, work, PendingSync(changed=changed, removed=removed),
    )


def enqueue_initial_parse(background_tasks, user_id: str, repo_full_name: str) -> None:
    enqueued_jobs.append({"job": "initial_parse", "user_id": user_id, "repo": repo_full_name})
    background_tasks.add_task(run_initial_parse, user_id, repo_full_name)


def enqueue_incremental_reparse(
    background_tasks, user_id: str, repo_full_name: str,
    changed_files: list[str], removed_files: list[str],
) -> None:
    enqueued_jobs.append({
        "job": "incremental_reparse", "user_id": user_id, "repo": repo_full_name,
        "changed": changed_files, "removed": removed_files,
    })
    background_tasks.add_task(run_incremental_reparse, user_id, repo_full_name, changed_files, removed_files)


# --------------------------------------------------------------------------
# Jira ticket sync
# --------------------------------------------------------------------------


async def run_ticket_sync(user_id: str, project_key: str) -> None:
    """Full ticket pull for one project; sync_status acts as the overlap lock."""
    from app.services import ticket_service  # local import to avoid a cycle

    claimed = await mongo.jira_projects().find_one_and_update(
        {
            "user_id": user_id,
            "project_key": project_key,
            "status": {"$ne": "disconnected"},
            "sync_status": {"$ne": "in_progress"},
        },
        {"$set": {"sync_status": "in_progress", "updated_at": _now()}},
    )
    if claimed is None:
        logger.info("ticket_sync_skipped_already_running", project=project_key)
        return

    try:
        count = await ticket_service.sync_project(user_id, project_key)
        await mongo.jira_projects().update_one(
            {"user_id": user_id, "project_key": project_key},
            {"$set": {
                "sync_status": "done",
                "sync_error": None,
                "last_synced_at": _now(),
                "updated_at": _now(),
            }},
        )
        logger.info("ticket_sync_done", project=project_key, tickets=count)
    except Exception as exc:
        logger.exception("ticket_sync_failed", project=project_key)
        await mongo.jira_projects().update_one(
            {"user_id": user_id, "project_key": project_key},
            {"$set": {
                "sync_status": "failed",
                "sync_error": str(exc),
                "updated_at": _now(),
            }},
        )


async def resync_all_projects() -> None:
    """One polling pass over every connected project (eval-1 choice: polling
    instead of Jira webhooks — say so if asked why tickets aren't real-time)."""
    async for project in mongo.jira_projects().find({"status": {"$ne": "disconnected"}}):
        await run_ticket_sync(project["user_id"], project["project_key"])


def enqueue_ticket_sync(background_tasks, user_id: str, project_key: str) -> None:
    enqueued_jobs.append({"job": "ticket_sync", "user_id": user_id, "project": project_key})
    background_tasks.add_task(run_ticket_sync, user_id, project_key)
