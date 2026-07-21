from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Overwatch Backend", version="0.1.0")
    app.state.settings = settings

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()
