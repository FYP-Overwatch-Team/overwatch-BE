import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.accounts import merge as merge_engine
from app.accounts.router import router as accounts_router
from app.auth.router import router as auth_router
from app.boards.router import router as boards_router
from app.comments.router import router as comments_router
from app.sprints.router import router as sprints_router
from app.chat.router import router as chat_router
from app.documents.attachments import router as attachments_router
from app.decisions.router import router as decisions_router
from app.documents.router import router as documents_router
from app.connectors.router import router as connections_router
from app.github import checkout
from app.github.router import router as github_router
from app.github.webhooks import router as webhooks_router
from app.graph.router import router as graph_router
from app.jira.router import router as jira_router
from app.link_previews.router import router as link_previews_router
from app.onboarding.router import router as onboarding_router
from app.usage.router import router as usage_router
from app.waitlist.router import router as waitlist_router
from app.work_item_types.router import router as work_item_types_router
from app.work_items.router import router as work_items_router
from app.workspaces.router import router as workspaces_router
from app.workers import jobs
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.responses import EnvelopeRoute
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db import mongo
from app.db.knowledge_graph_store import get_knowledge_graph_store


async def _tracker_sync_loop(interval_seconds: int) -> None:
    # a pass at once, so a restart doesn't leave boards a whole interval behind
    while True:
        try:
            await jobs.resync_tracker_targets()
        except Exception:
            structlog.get_logger("app.jobs").exception("tracker_sync_loop_error")
        await asyncio.sleep(interval_seconds)


async def _doc_sync_loop(interval_seconds: int) -> None:
    """Poll document sources (Confluence etc.) on their own shorter interval."""
    # a pass at once, so a restart doesn't leave spaces a whole interval behind
    while True:
        try:
            await jobs.resync_doc_targets()
        except Exception:
            structlog.get_logger("app.jobs").exception("doc_sync_loop_error")
        await asyncio.sleep(interval_seconds)


async def _merge_loop() -> None:
    """Finish account merges a crash or restart interrupted: at once, then every few minutes."""
    while True:
        try:
            await merge_engine.resume_stalled()
        except Exception:
            structlog.get_logger("app.accounts").exception("merge_loop_error")
        await asyncio.sleep(merge_engine.STALL_AFTER.total_seconds())


async def _docs_loop(push_seconds: int) -> None:
    """Push settled docs edits every few seconds; purge expired archives about hourly."""
    purged_at = 0.0
    while True:
        await asyncio.sleep(push_seconds)
        try:
            await jobs.push_documents()
            if asyncio.get_running_loop().time() - purged_at >= 3600:
                await jobs.purge_archived_documents()
                purged_at = asyncio.get_running_loop().time()
        except Exception:
            structlog.get_logger("app.jobs").exception("docs_loop_error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo.connect()
    try:
        await mongo.ensure_indexes()
    except Exception:
        # Index creation must never block boot — a running API with slow queries
        # beats one that refuses to start.
        structlog.get_logger("app.db").exception("ensure_indexes_failed")

    try:
        # Earlier versions cloned with the token inside the remote URL, which
        # git persists in .git/config. Remove any that are still on disk.
        cleaned = await checkout.sanitize_existing_checkouts()
        if cleaned:
            structlog.get_logger("app.repos").warning(
                "checkout_credentials_scrubbed", checkouts=cleaned,
            )
    except Exception:
        structlog.get_logger("app.repos").exception("checkout_sanitize_failed")

    try:
        store = get_knowledge_graph_store()
        await store.ensure_schema()
        if removed := await store.purge_legacy_nodes():
            structlog.get_logger("app.knowledge_graph").warning(
                "legacy_graph_nodes_removed", nodes=removed,
            )
    except Exception:
        # Constraints are an optimisation and a safety net, not a
        # prerequisite: a database that is briefly unreachable at boot must
        # not stop the API from serving.
        structlog.get_logger("app.knowledge_graph").exception("ensure_schema_failed")

    sync_task = asyncio.create_task(
        _tracker_sync_loop(get_settings().ticket_sync_interval_seconds)
    )
    doc_sync_task = asyncio.create_task(
        _doc_sync_loop(get_settings().doc_sync_interval_seconds)
    )
    docs_task = asyncio.create_task(_docs_loop(get_settings().doc_push_interval_seconds))
    merge_task = asyncio.create_task(_merge_loop())
    yield
    sync_task.cancel()
    doc_sync_task.cancel()
    docs_task.cancel()
    merge_task.cancel()
    mongo.close()


def create_app(manage_db: bool = True) -> FastAPI:
    settings = get_settings()
    configure_logging(json_logs=settings.app_env != "dev")

    app = FastAPI(title="Overwatch Backend", version="0.1.0", lifespan=lifespan if manage_db else None)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    # Locked to the configured frontend origin, never "*" — GitHub/Jira tokens
    # flow through this API, so an open CORS policy is a real risk, not a lint warning.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)

    app.include_router(auth_router)
    app.include_router(accounts_router)
    app.include_router(github_router)
    app.include_router(jira_router)
    app.include_router(onboarding_router)
    app.include_router(graph_router)
    app.include_router(webhooks_router)
    app.include_router(connections_router)
    app.include_router(work_items_router)
    app.include_router(work_item_types_router)
    app.include_router(boards_router)
    app.include_router(sprints_router)
    app.include_router(comments_router)
    app.include_router(documents_router)
    app.include_router(decisions_router)
    app.include_router(attachments_router)
    app.include_router(link_previews_router)
    app.include_router(chat_router)
    app.include_router(usage_router)
    app.include_router(workspaces_router)
    app.include_router(waitlist_router)

    app.router.route_class = EnvelopeRoute  # app-level routes return the envelope too

    @app.get("/health")
    async def health() -> dict:
        return {"env": settings.app_env}

    return app


app = create_app()
