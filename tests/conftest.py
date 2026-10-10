import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mongomock_motor import AsyncMongoMockClient

from app.core.config import get_settings
from app.db import mongo
from app.main import create_app
from app.integrations import gemini_client, github_app
from app.workers import jobs
from app.services.facts_repository import InMemoryFactsStore, use_facts_store
from app.db.knowledge_graph_store import (
    InMemoryKnowledgeGraphStore,
    use_knowledge_graph_store,
)
from tests.fakes import FakeGeminiClient


@pytest.fixture(scope="session")
def github_app_key(tmp_path_factory):
    """An RSA key standing in for the GitHub App private key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("github-app") / "app.pem"
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    return key, path


@pytest.fixture(autouse=True)
def test_env(monkeypatch, github_app_key):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GITHUB_APP_SLUG", "overwatch-test")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(github_app_key[1]))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("JIRA_CLIENT_ID", "test-jira-client-id")
    monkeypatch.setenv("JIRA_CLIENT_SECRET", "test-jira-client-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def mock_mongo():
    client = AsyncMongoMockClient(tz_aware=True)
    mongo.use_db(client["overwatch_test"])
    await mongo.ensure_indexes()  # unique indexes behave as in production
    # mongomock expires TTL docs instantly; real Mongo sweeps about once a minute
    await mongo.refresh_tokens().drop_index("expires_at_1")
    await mongo.webhook_deliveries().drop_index("received_at_1")
    yield
    mongo.use_db(None)


@pytest.fixture(autouse=True)
def graph_store():
    """Every test gets an empty knowledge graph and facts cache."""
    store = InMemoryKnowledgeGraphStore()
    use_knowledge_graph_store(store)
    use_facts_store(InMemoryFactsStore())
    yield store
    use_knowledge_graph_store(None)
    use_facts_store(None)


@pytest.fixture(autouse=True)
def fresh_github_app(monkeypatch):
    """No installation tokens cached across tests."""
    monkeypatch.setattr(github_app, "_app", None)


@pytest.fixture
def record_jobs(monkeypatch):
    """Background jobs a request scheduled, recorded instead of run."""
    calls: list[tuple] = []

    async def run_index(user_id, repo_full_name, *, full=False):
        calls.append(("index", user_id, repo_full_name, full))

    async def run_ticket_sync(user_id, project_key):
        calls.append(("ticket_sync", user_id, project_key))

    monkeypatch.setattr(jobs, "run_index", run_index)
    monkeypatch.setattr(jobs, "run_ticket_sync", run_ticket_sync)
    return calls


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
