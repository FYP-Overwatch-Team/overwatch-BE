from datetime import datetime, timezone

import structlog

from app.db import mongo
from app.parsing import ast_service, graph_builder
from app.services import repo_service
from app.services.graph_service import get_graph_repository

logger = structlog.get_logger("app.jobs")

# In-memory record of enqueued jobs; also what tests assert against.
# For eval 1 a single-instance BackgroundTasks runner is sufficient (see plan Phase 5).
enqueued_jobs: list[dict] = []


async def run_initial_parse(user_id: str, repo_full_name: str) -> None:
    """Full-repo AST parse -> graph -> Neo4j upsert.

    A find_one_and_update on parse_status acts as the lock: if another parse of
    this repo is already in_progress (e.g. a webhook fired mid-onboarding), skip.
    """
    claimed = await mongo.repos().find_one_and_update(
        {
            "user_id": user_id,
            "repo_full_name": repo_full_name,
            "parse_status": {"$ne": "in_progress"},
        },
        {"$set": {"parse_status": "in_progress", "updated_at": datetime.now(timezone.utc)}},
    )
    if claimed is None:
        logger.info("parse_skipped_already_running", repo=repo_full_name)
        return

    try:
        workdir = repo_service.repo_workdir(repo_full_name)
        parses = ast_service.parse_repo(workdir)
        # replace the per-file parse cache wholesale; incremental re-parse patches it
        await mongo.file_parses().delete_many({"repo_full_name": repo_full_name})
        if parses:
            await mongo.file_parses().insert_many([
                {
                    "repo_full_name": repo_full_name,
                    "path": p.path,
                    "language": p.language,
                    "imports": p.imports,
                    "definitions": p.definitions,
                }
                for p in parses
            ])
        await _rebuild_and_store_graph(repo_full_name)
        await _set_parse_status(user_id, repo_full_name, "done")
        logger.info("parse_done", repo=repo_full_name, files=len(parses))
    except Exception as exc:
        logger.exception("parse_failed", repo=repo_full_name)
        await _set_parse_status(user_id, repo_full_name, "failed", error=str(exc))


async def run_incremental_reparse(
    user_id: str, repo_full_name: str, changed_files: list[str], removed_files: list[str],
) -> None:
    """Webhook-driven partial update: parse only the touched files, then rebuild
    the (directory-level) graph from the per-file cache."""
    claimed = await mongo.repos().find_one_and_update(
        {
            "user_id": user_id,
            "repo_full_name": repo_full_name,
            "parse_status": {"$ne": "in_progress"},
        },
        {"$set": {"parse_status": "in_progress", "updated_at": datetime.now(timezone.utc)}},
    )
    if claimed is None:
        logger.info("reparse_skipped_already_running", repo=repo_full_name)
        return

    try:
        token = await repo_service.get_github_token(user_id)
        workdir = await repo_service.update_workdir(repo_full_name, token)

        for rel_path in removed_files:
            await mongo.file_parses().delete_one({"repo_full_name": repo_full_name, "path": rel_path})

        for rel_path in changed_files:
            abs_path = workdir / rel_path
            if abs_path.suffix not in ast_service.LANGUAGE_BY_EXT:
                continue
            if not abs_path.is_file():
                await mongo.file_parses().delete_one({"repo_full_name": repo_full_name, "path": rel_path})
                continue
            parse = ast_service.parse_file(workdir, abs_path)
            await mongo.file_parses().update_one(
                {"repo_full_name": repo_full_name, "path": parse.path},
                {"$set": {
                    "language": parse.language,
                    "imports": parse.imports,
                    "definitions": parse.definitions,
                }},
                upsert=True,
            )

        await _rebuild_and_store_graph(repo_full_name)
        await _set_parse_status(user_id, repo_full_name, "done")
        logger.info("incremental_reparse_done", repo=repo_full_name,
                    changed=len(changed_files), removed=len(removed_files))
    except Exception as exc:
        logger.exception("incremental_reparse_failed", repo=repo_full_name)
        await _set_parse_status(user_id, repo_full_name, "failed", error=str(exc))


def enqueue_incremental_reparse(
    background_tasks, user_id: str, repo_full_name: str,
    changed_files: list[str], removed_files: list[str],
) -> None:
    enqueued_jobs.append({
        "job": "incremental_reparse", "user_id": user_id, "repo": repo_full_name,
        "changed": changed_files, "removed": removed_files,
    })
    background_tasks.add_task(run_incremental_reparse, user_id, repo_full_name, changed_files, removed_files)


async def _rebuild_and_store_graph(repo_full_name: str) -> None:
    parses = [
        ast_service.FileParse(
            path=doc["path"], language=doc["language"],
            imports=doc["imports"], definitions=doc["definitions"],
        )
        async for doc in mongo.file_parses().find({"repo_full_name": repo_full_name})
    ]
    graph = graph_builder.build_graph(repo_full_name, parses)
    await get_graph_repository().upsert_graph(repo_full_name, graph)


async def _set_parse_status(user_id: str, repo_full_name: str, status: str, error: str | None = None) -> None:
    await mongo.repos().update_one(
        {"user_id": user_id, "repo_full_name": repo_full_name},
        {"$set": {
            "parse_status": status,
            "parse_error": error,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


def enqueue_initial_parse(background_tasks, user_id: str, repo_full_name: str) -> None:
    enqueued_jobs.append({"job": "initial_parse", "user_id": user_id, "repo": repo_full_name})
    background_tasks.add_task(run_initial_parse, user_id, repo_full_name)


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
        {"$set": {"sync_status": "in_progress", "updated_at": datetime.now(timezone.utc)}},
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
                "last_synced_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
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
                "updated_at": datetime.now(timezone.utc),
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
