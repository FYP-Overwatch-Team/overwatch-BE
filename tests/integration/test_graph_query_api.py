"""Drill-down, neighbours, search and stats — and who may read them."""

from datetime import datetime, timezone

import pytest

from app.core.config import get_settings
from app.db import mongo
from app.knowledge_graph.build.model import EdgeType, GraphDelta, GraphEdge, GraphNode, NodeLabel
from app.services.knowledge_graph_store import (
    InMemoryKnowledgeGraphStore,
    use_knowledge_graph_store,
)
from tests.integration.test_auth_flow import login

REPO = "octocat/hello-world"
OTHER_REPO = "someone-else/private"


@pytest.fixture(autouse=True)
def knowledge_graph_enabled(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_V2", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def graph():
    store = InMemoryKnowledgeGraphStore()
    use_knowledge_graph_store(store)
    yield store
    use_knowledge_graph_store(None)


def seed_delta(repo: str) -> GraphDelta:
    """A module with one file, two symbols and a call between them."""
    module, file = f"{repo}:api", f"{repo}:api/handlers.py"
    handler, helper = f"{file}#handle", f"{file}#helper"
    return GraphDelta(
        repo_full_name=repo,
        version="sha-1",
        upserted_nodes=(
            GraphNode(module, NodeLabel.MODULE, {"name": "api", "kind": "module", "file_count": 1, "definition_count": 2}),
            GraphNode(file, NodeLabel.FILE, {"name": "handlers.py", "path": "api/handlers.py", "language": "python"}),
            GraphNode(handler, NodeLabel.SYMBOL, {"name": "handle", "kind": "function", "signature": "handle(request)", "file_path": "api/handlers.py"}),
            GraphNode(helper, NodeLabel.SYMBOL, {"name": "helper", "kind": "function", "signature": "helper()", "file_path": "api/handlers.py"}),
        ),
        upserted_edges=(
            GraphEdge(module, file, EdgeType.CONTAINS),
            GraphEdge(file, handler, EdgeType.DEFINES),
            GraphEdge(file, helper, EdgeType.DEFINES),
            GraphEdge(handler, helper, EdgeType.CALLS, {"count": 2}),
        ),
    )


async def connect_repo(user_id: str, repo: str = REPO, **extra) -> None:
    now = datetime.now(timezone.utc)
    await mongo.repos().insert_one({
        "user_id": user_id, "repo_full_name": repo, "default_branch": "main",
        "parse_status": "done", "connected_at": now, "updated_at": now, **extra,
    })


@pytest.fixture
async def ready(client, graph):
    access = await login(client)
    user = await mongo.users().find_one({})
    await connect_repo(
        user["_id"],
        graph_version="sha-1",
        graph_stats={"files": 1, "symbols": 2, "resolution": {"internal_import_resolution": 1.0}},
    )
    await graph.apply(seed_delta(REPO))
    return {"Authorization": f"Bearer {access}"}


async def test_module_view_is_served_from_the_knowledge_graph(client, ready):
    response = await client.get(f"/graph?repo_full_name={REPO}", headers=ready)

    assert response.status_code == 200
    body = response.json()
    assert [node["name"] for node in body["nodes"]] == ["api"]
    assert body["parse_status"] == "done"
    assert response.headers["ETag"] == 'W/"sha-1"'


async def test_node_detail_lists_what_a_node_contains(client, ready):
    response = await client.get(
        f"/graph/node?repo_full_name={REPO}&node_id={REPO}:api/handlers.py", headers=ready,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["properties"]["path"] == "api/handlers.py"
    assert {child["name"] for child in body["children"]} == {"handle", "helper"}


async def test_neighbours_returns_the_subgraph_around_a_node(client, ready):
    response = await client.get(
        f"/graph/neighbours?repo_full_name={REPO}&node_id={REPO}:api/handlers.py%23handle"
        "&direction=out&depth=1",
        headers=ready,
    )

    assert response.status_code == 200
    body = response.json()
    assert any(edge["type"] == "CALLS" for edge in body["edges"])
    assert any(node["name"] == "helper" for node in body["nodes"])


async def test_search_finds_symbols_by_name(client, ready):
    response = await client.get(f"/graph/search?repo_full_name={REPO}&q=help", headers=ready)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["name"] == "helper"


async def test_stats_report_contents_and_coverage(client, ready):
    response = await client.get(f"/graph/stats?repo_full_name={REPO}", headers=ready)

    assert response.status_code == 200
    body = response.json()
    assert body["nodes_by_label"]["Symbol"] == 2
    assert body["edges_by_type"]["CALLS"] == 1
    assert body["resolution"]["internal_import_resolution"] == 1.0
    assert body["graph_version"] == "sha-1"


# -- Authorisation ---------------------------------------------------------


ENDPOINTS = [
    "/graph?repo_full_name={repo}",
    "/graph/node?repo_full_name={repo}&node_id={repo}:api",
    "/graph/neighbours?repo_full_name={repo}&node_id={repo}:api",
    "/graph/search?repo_full_name={repo}&q=handle",
    "/graph/stats?repo_full_name={repo}",
]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_every_endpoint_requires_authentication(client, graph, endpoint):
    response = await client.get(endpoint.format(repo=REPO))

    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_every_endpoint_refuses_a_repository_you_have_not_connected(
    client, ready, graph, endpoint,
):
    await graph.apply(seed_delta(OTHER_REPO))

    response = await client.get(endpoint.format(repo=OTHER_REPO), headers=ready)

    assert response.status_code == 404
    assert response.json()["error_code"] == "repo_not_connected"


async def test_a_node_id_from_another_repository_is_not_readable(client, ready, graph):
    """The id names another repository; the authorised repository is what filters."""
    await graph.apply(seed_delta(OTHER_REPO))

    response = await client.get(
        f"/graph/node?repo_full_name={REPO}&node_id={OTHER_REPO}:api/handlers.py",
        headers=ready,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "node_not_found"


async def test_neighbours_cannot_traverse_into_another_repository(client, ready, graph):
    await graph.apply(seed_delta(OTHER_REPO))

    response = await client.get(
        f"/graph/neighbours?repo_full_name={REPO}&node_id={OTHER_REPO}:api", headers=ready,
    )

    assert response.json() == {"nodes": [], "edges": []}


# -- Bounds ----------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("depth=0", 422), ("depth=9", 422), ("direction=sideways", 422),
        ("limit=0", 422), ("limit=99999", 422),
    ],
)
async def test_neighbour_parameters_are_bounded(client, ready, query, expected):
    response = await client.get(
        f"/graph/neighbours?repo_full_name={REPO}&node_id={REPO}:api&{query}", headers=ready,
    )

    assert response.status_code == expected


@pytest.mark.parametrize("query", ["q=a", "q=", "limit=500&q=handle"])
async def test_search_parameters_are_bounded(client, ready, query):
    response = await client.get(f"/graph/search?repo_full_name={REPO}&{query}", headers=ready)

    assert response.status_code == 422


async def test_a_malformed_repository_name_is_rejected(client, ready):
    response = await client.get("/graph?repo_full_name=../../etc/passwd", headers=ready)

    assert response.status_code in (400, 404)
