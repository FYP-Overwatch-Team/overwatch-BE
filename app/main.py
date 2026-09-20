import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_current_user
from app.api.routes import auth, github, graph, jira, onboarding, query, tickets, webhooks
from app.workers import jobs
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db import mongo
from app.services import repo_service
from app.services.knowledge_graph_store import get_knowledge_graph_store


async def _ticket_sync_loop(interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await jobs.resync_all_projects()
        except Exception:
            structlog.get_logger("app.jobs").exception("ticket_sync_loop_error")


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
        cleaned = await repo_service.sanitize_existing_checkouts()
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
        _ticket_sync_loop(get_settings().ticket_sync_interval_seconds)
    )
    yield
    sync_task.cancel()
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

    app.include_router(auth.router)
    app.include_router(github.router)
    app.include_router(jira.router)
    app.include_router(onboarding.router)
    app.include_router(graph.router)
    app.include_router(webhooks.router)
    app.include_router(tickets.router)
    app.include_router(query.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.app_env}

    @app.get("/me")
    async def me(user: dict = Depends(get_current_user)) -> dict:
        return {
            "id": user["_id"],
            "github_login": user["github_login"],
            "name": user.get("name"),
            "avatar_url": user.get("avatar_url"),
        }

    return app


app = create_app()
