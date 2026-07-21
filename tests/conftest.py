import httpx
import pytest

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def app():
    get_settings.cache_clear()
    return create_app()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
