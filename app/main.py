import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI

from app.api.deps import get_current_user
from app.api.routes import auth, github, graph, jira, onboarding, query, tickets, webhooks
from app.workers import jobs
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db import mongo


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
