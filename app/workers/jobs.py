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
        graph = graph_builder.build_graph(repo_full_name, parses)
        await get_graph_repository().upsert_graph(repo_full_name, graph)
        await mongo.repos().update_one(
            {"user_id": user_id, "repo_full_name": repo_full_name},
            {"$set": {
                "parse_status": "done",
                "parse_error": None,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        logger.info("parse_done", repo=repo_full_name,
                    nodes=len(graph["nodes"]), edges=len(graph["edges"]))
    except Exception as exc:
        logger.exception("parse_failed", repo=repo_full_name)
        await mongo.repos().update_one(
            {"user_id": user_id, "repo_full_name": repo_full_name},
            {"$set": {
                "parse_status": "failed",
                "parse_error": str(exc),
                "updated_at": datetime.now(timezone.utc),
            }},
        )


def enqueue_initial_parse(background_tasks, user_id: str, repo_full_name: str) -> None:
    enqueued_jobs.append({"job": "initial_parse", "user_id": user_id, "repo": repo_full_name})
    background_tasks.add_task(run_initial_parse, user_id, repo_full_name)


async def run_ticket_sync(user_id: str, project_key: str) -> None:
    """Placeholder until Phase 7 wires in the Jira ticket sync."""
    logger.info("ticket_sync_requested", project=project_key)


def enqueue_ticket_sync(background_tasks, user_id: str, project_key: str) -> None:
    enqueued_jobs.append({"job": "ticket_sync", "user_id": user_id, "project": project_key})
    background_tasks.add_task(run_ticket_sync, user_id, project_key)
