from fastapi import FastAPI

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(json_logs=settings.app_env != "dev")

    app = FastAPI(title="Overwatch Backend", version="0.1.0")
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()
