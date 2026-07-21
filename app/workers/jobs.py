import structlog

logger = structlog.get_logger("app.jobs")

# In-memory record of enqueued jobs; also what tests assert against.
# For eval 1 a single-instance BackgroundTasks runner is sufficient (see plan Phase 5).
enqueued_jobs: list[dict] = []


async def run_initial_parse(user_id: str, repo_full_name: str) -> None:
    """Placeholder until Phase 5 wires in the AST parse pipeline."""
    logger.info("initial_parse_requested", repo=repo_full_name)


def enqueue_initial_parse(background_tasks, user_id: str, repo_full_name: str) -> None:
    enqueued_jobs.append({"job": "initial_parse", "user_id": user_id, "repo": repo_full_name})
    background_tasks.add_task(run_initial_parse, user_id, repo_full_name)


async def run_ticket_sync(user_id: str, project_key: str) -> None:
    """Placeholder until Phase 7 wires in the Jira ticket sync."""
    logger.info("ticket_sync_requested", project=project_key)


def enqueue_ticket_sync(background_tasks, user_id: str, project_key: str) -> None:
    enqueued_jobs.append({"job": "ticket_sync", "user_id": user_id, "project": project_key})
    background_tasks.add_task(run_ticket_sync, user_id, project_key)
