from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.api.deps import get_current_user
from app.api.routes import auth, github, jira, onboarding
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db import mongo


@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo.connect()
    yield
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
