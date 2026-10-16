import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mongomock.collection import BulkOperationBuilder, Collection
from pymongo.errors import DuplicateKeyError
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
from app.mail.memory import Outbox
from app.mail.sender import use_email_sender
from app.storage.memory import InMemoryStore
from app.storage.store import use_object_store
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
    monkeypatch.setenv("HIBP_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def bulk_updates_take_sort():
    """pymongo's UpdateOne passes `sort`, which mongomock's bulk builder predates."""
    add_update = BulkOperationBuilder.add_update

    def accepting_sort(self, *args, sort=None, **kwargs):
        return add_update(self, *args, **kwargs)

    BulkOperationBuilder.add_update = accepting_sort
    yield
    BulkOperationBuilder.add_update = add_update


@pytest.fixture(scope="session", autouse=True)
def partial_indexes_rebuild():
    """mongomock checks every document when an index is created again, ignoring its partial filter;
    Mongo only indexes the documents that match it."""
    create_index = Collection.create_index

    def respecting_partial(self, *args, **kwargs):
        try:
            return create_index(self, *args, **kwargs)
        except DuplicateKeyError:
            if "partialFilterExpression" not in kwargs:
                raise
            return kwargs.get("name")

    Collection.create_index = respecting_partial
    yield
    Collection.create_index = create_index


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
def object_store():
    """Every test uploads into an empty in-memory store."""
    store = InMemoryStore()
    use_object_store(store)
    yield store
    use_object_store(None)


@pytest.fixture(autouse=True)
def outbox():
    """Every test's emails, kept instead of sent."""
    sent = Outbox()
    use_email_sender(sent)
    yield sent
    use_email_sender(None)


@pytest.fixture(autouse=True)
def fresh_github_app(monkeypatch):
    """No installation tokens cached across tests."""
    monkeypatch.setattr(github_app, "_app", None)


@pytest.fixture
def record_jobs(monkeypatch):
    """Background jobs a request scheduled, recorded instead of run."""
    calls: list[tuple] = []

    async def run_index(repo_full_name, *, full=False):
        calls.append(("index", repo_full_name, full))

    async def run_connector_sync(workspace_id, provider, target_key):
        calls.append(("sync", workspace_id, provider, target_key))

    monkeypatch.setattr(jobs, "run_index", run_index)
    monkeypatch.setattr(jobs, "run_connector_sync", run_connector_sync)
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
