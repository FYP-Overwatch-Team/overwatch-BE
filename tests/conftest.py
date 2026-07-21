import httpx
import pytest
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

from app.core.config import get_settings
from app.db import mongo
from app.main import create_app
from app.integrations import gemini_client
from app.services import graph_service
from app.services.graph_service import InMemoryGraphRepository
from tests.fakes import FakeGeminiClient


@pytest.fixture(autouse=True)
def test_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("JIRA_CLIENT_ID", "test-jira-client-id")
    monkeypatch.setenv("JIRA_CLIENT_SECRET", "test-jira-client-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def mock_mongo():
    client = AsyncMongoMockClient()
    mongo.use_db(client["overwatch_test"])
    yield
    mongo.use_db(None)


@pytest.fixture(autouse=True)
def graph_repo():
    repo = InMemoryGraphRepository()
    graph_service.use_graph_repository(repo)
    yield repo
    graph_service.use_graph_repository(None)


@pytest.fixture
def fake_gemini():
    fake = FakeGeminiClient()
    gemini_client.use_gemini_client(fake)
    yield fake
    gemini_client.use_gemini_client(None)


@pytest.fixture
def app(test_env):
    return create_app(manage_db=False)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        yield c
