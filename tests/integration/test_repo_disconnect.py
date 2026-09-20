"""Disconnecting a repository must leave nothing of it behind."""

from datetime import datetime, timezone

import pytest

from app.core.config import get_settings
from app.db import mongo
from app.knowledge_graph.build.model import EdgeType, GraphDelta, GraphEdge, GraphNode, NodeLabel
from app.knowledge_graph.facts import FileFacts
from app.services import repo_service
from app.services.facts_repository import InMemoryFactsStore, use_facts_store
from app.services.knowledge_graph_store import (
    InMemoryKnowledgeGraphStore,
    use_knowledge_graph_store,
)
from tests.integration.test_auth_flow import login

REPO = "octocat/hello-world"


@pytest.fixture
def stores():
    facts, graph = InMemoryFactsStore(), InMemoryKnowledgeGraphStore()
    use_facts_store(facts)
    use_knowledge_graph_store(graph)
    yield facts, graph
    use_facts_store(None)
    use_knowledge_graph_store(None)


async def seed_everything(user_id: str, facts, graph, checkout_root) -> None:
    now = datetime.now(timezone.utc)
    await mongo.repos().insert_one({
        "user_id": user_id, "repo_full_name": REPO, "default_branch": "main",
        "webhook_id": 42, "webhook_status": "created", "parse_status": "done",
        "connected_at": now, "updated_at": now,
    })
    await facts.save_many(REPO, [FileFacts(path="a.py", language="python", content_hash="h")])
    await graph.apply(
        GraphDelta(
            repo_full_name=REPO,
            version="sha-1",
            upserted_nodes=(GraphNode(f"{REPO}:api", NodeLabel.MODULE, {"name": "api", "kind": "module"}),),
        )
    )
    checkout = repo_service.repo_workdir(REPO)
    (checkout / "src").mkdir(parents=True, exist_ok=True)
    (checkout / "src" / "a.py").write_text("x = 1\n")


@pytest.fixture
def checkout_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_disconnect_removes_every_trace(client, stores, checkout_root):
    facts, graph = stores
    access = await login(client)
    user = await mongo.users().find_one({})
    await seed_everything(user["_id"], facts, graph, checkout_root)
    checkout = repo_service.repo_workdir(REPO)
    assert checkout.exists()

    response = await client.delete(
        f"/github/repos/{REPO}", headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 204
    assert await mongo.repos().find_one({"repo_full_name": REPO}) is None
    assert await facts.load_all(REPO) == {}
    assert await graph.node_ids(REPO) == set()
    assert not checkout.exists()  # the source code is gone from disk


async def test_disconnecting_an_unconnected_repository_is_a_404(client, stores, checkout_root):
    access = await login(client)

    response = await client.delete(
        "/github/repos/someone/never-connected",
        headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "repo_not_connected"


async def test_one_user_cannot_disconnect_another_users_repository(client, stores, checkout_root):
    facts, graph = stores
    access = await login(client)
    user = await mongo.users().find_one({})
    await seed_everything("someone-else", facts, graph, checkout_root)

    response = await client.delete(
        f"/github/repos/{REPO}", headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 404
    # The other user's connection and data are untouched.
    assert await mongo.repos().find_one({"user_id": "someone-else"}) is not None
    assert await graph.node_ids(REPO) != set()


async def test_shared_repositories_keep_their_data_until_the_last_user_leaves(
    client, stores, checkout_root,
):
    facts, graph = stores
    access = await login(client)
    user = await mongo.users().find_one({})
    await seed_everything(user["_id"], facts, graph, checkout_root)
    now = datetime.now(timezone.utc)
    await mongo.repos().insert_one({
        "user_id": "other-user", "repo_full_name": REPO, "default_branch": "main",
        "webhook_status": "created", "parse_status": "done",
        "connected_at": now, "updated_at": now,
    })

    await client.delete(f"/github/repos/{REPO}", headers={"Authorization": f"Bearer {access}"})

    # The graph and facts are shared; the other user still has it connected.
    assert await graph.node_ids(REPO) != set()
    assert await facts.load_all(REPO) != {}
    assert await mongo.repos().find_one({"user_id": "other-user"}) is not None


@pytest.mark.parametrize("path", ["/github/repos/../../etc/passwd", "/github/repos/owner/..%2F.."])
async def test_disconnect_rejects_path_traversal(client, stores, checkout_root, path):
    access = await login(client)

    response = await client.delete(path, headers={"Authorization": f"Bearer {access}"})

    assert response.status_code in (400, 404, 422)
