"""The interactive view endpoint: opening, closing, filtering — and isolation."""

from datetime import datetime, timezone

import pytest

from app.db import mongo
from app.knowledge_graph.build.ids import module_id
from app.knowledge_graph.build.model import (
    EdgeType,
    GraphDelta,
    GraphEdge,
    GraphNode,
    NodeLabel,
)
from app.services.knowledge_graph_store import (
    InMemoryKnowledgeGraphStore,
    use_knowledge_graph_store,
)
from tests.integration.test_auth_flow import login

REPO = "octocat/hello-world"
OTHER_REPO = "someone-else/private"


@pytest.fixture
def graph():
    store = InMemoryKnowledgeGraphStore()
    use_knowledge_graph_store(store)
    yield store
    use_knowledge_graph_store(None)


def seed_delta(repo: str = REPO) -> GraphDelta:
    """`api` nests two folders; `web` imports from one of them."""
    api, routes, services = (
        module_id(repo, "api"), module_id(repo, "api/routes"), module_id(repo, "api/services"),
    )
    web = module_id(repo, "web")
    users, store_file = f"{repo}:api/routes/users.py", f"{repo}:api/services/store.py"
    page = f"{repo}:web/page.tsx"

    def module(node_id, name, depth, files):
        return GraphNode(node_id, NodeLabel.MODULE, {
            "name": name, "kind": "module", "path": name,
            "depth": depth, "file_count": files, "definition_count": files,
        })

    def source(node_id, path):
        return GraphNode(node_id, NodeLabel.FILE, {
            "name": path.rpartition("/")[2], "path": path, "language": "python",
        })

    return GraphDelta(
        repo_full_name=repo,
        version="sha-1",
        upserted_nodes=(
            GraphNode(repo, NodeLabel.REPOSITORY, {"name": repo}),
            module(api, "api", 1, 2),
            module(routes, "api/routes", 2, 1),
            module(services, "api/services", 2, 1),
            module(web, "web", 1, 1),
            source(users, "api/routes/users.py"),
            source(store_file, "api/services/store.py"),
            source(page, "web/page.tsx"),
        ),
        upserted_edges=(
            GraphEdge(repo, api, EdgeType.CONTAINS),
            GraphEdge(repo, web, EdgeType.CONTAINS),
            GraphEdge(api, routes, EdgeType.CONTAINS),
            GraphEdge(api, services, EdgeType.CONTAINS),
            GraphEdge(routes, users, EdgeType.CONTAINS),
            GraphEdge(services, store_file, EdgeType.CONTAINS),
            GraphEdge(web, page, EdgeType.CONTAINS),
            GraphEdge(page, users, EdgeType.IMPORTS, {"count": 3}),
            GraphEdge(users, store_file, EdgeType.IMPORTS, {"count": 5}),
        ),
    )


@pytest.fixture
async def ready(client, graph):
    access = await login(client)
    user = await mongo.users().find_one({})
    now = datetime.now(timezone.utc)
    await mongo.repos().insert_one({
        "user_id": user["_id"], "repo_full_name": REPO, "default_branch": "main",
        "parse_status": "done", "graph_version": "sha-1",
        "connected_at": now, "updated_at": now,
    })
    await graph.apply(seed_delta(REPO))
    await graph.apply(seed_delta(OTHER_REPO))
    return {"Authorization": f"Bearer {access}"}


async def view(client, headers, **body):
    response = await client.post(
        f"/graph/view?repo_full_name={REPO}", json=body, headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def ids(body) -> list[str]:
    return [node["id"] for node in body["nodes"]]


async def test_the_default_view_is_the_outermost_folders(client, ready):
    body = await view(client, ready)

    assert ids(body) == [module_id(REPO, "api"), module_id(REPO, "web")]
    assert body["expanded"] == []
    assert body["truncated"] is False
    # The repository is the frame the view is drawn in, never a box inside it.
    assert REPO not in ids(body)


async def test_a_collapsed_folder_carries_the_edges_of_everything_inside_it(client, ready):
    body = await view(client, ready)

    assert body["edges"] == [{
        "source": module_id(REPO, "web"), "target": module_id(REPO, "api"),
        "type": "IMPORTS", "weight": 3, "collapsed": True,
    }]


async def test_opening_a_folder_swaps_it_for_what_is_inside(client, ready):
    body = await view(client, ready, expand=[module_id(REPO, "api")])

    assert module_id(REPO, "api") not in ids(body)
    assert module_id(REPO, "api/routes") in ids(body)
    assert module_id(REPO, "web") in ids(body)
    # Open containers come back described, not just named: they are no longer
    # on screen, so the client cannot look their names up.
    assert body["expanded"] == [{
        "id": module_id(REPO, "api"), "name": "api", "label": "Module",
        "kind": "module", "path": "api", "parent_id": REPO,
    }]


async def test_a_node_names_the_container_it_came_out_of(client, ready):
    """Which is how the UI offers to close it again."""
    body = await view(client, ready, expand=[module_id(REPO, "api")])

    by_id = {node["id"]: node for node in body["nodes"]}
    assert by_id[module_id(REPO, "api/routes")]["parent_id"] == module_id(REPO, "api")
    assert by_id[module_id(REPO, "web")]["parent_id"] == REPO


async def test_opening_reveals_the_dependency_that_was_hidden_inside(client, ready):
    body = await view(client, ready, expand=[module_id(REPO, "api")])

    internal = [
        edge for edge in body["edges"]
        if edge["source"] == module_id(REPO, "api/routes")
        and edge["target"] == module_id(REPO, "api/services")
    ]
    assert internal and internal[0]["weight"] == 5


async def test_closing_it_again_gives_back_the_original_view(client, ready):
    opened = await view(client, ready, expand=[module_id(REPO, "api")])
    closed = await view(client, ready, expand=[])
    first = await view(client, ready)

    assert ids(opened) != ids(closed)
    assert closed == first


async def test_expand_to_files_opens_every_folder_in_one_step(client, ready):
    body = await view(client, ready, expand_to="file")

    assert f"{REPO}:api/routes/users.py" in ids(body)
    assert f"{REPO}:web/page.tsx" in ids(body)
    assert all(node["label"] == "File" for node in body["nodes"])


async def test_filtering_relationship_types_leaves_the_nodes_alone(client, ready):
    body = await view(client, ready, edge_types=["CALLS"])

    assert body["edges"] == []
    assert ids(body) == [module_id(REPO, "api"), module_id(REPO, "web")]


async def test_filtering_node_labels_keeps_only_those_nodes(client, ready):
    body = await view(client, ready, expand_to="file", node_labels=["Module"])

    assert ids(body) == []


async def test_every_node_says_whether_it_can_be_opened(client, ready):
    body = await view(client, ready)

    assert all(node["expandable"] for node in body["nodes"])
    leaves = await view(client, ready, expand_to="file")
    assert not any(node["expandable"] for node in leaves["nodes"])


async def test_the_filter_vocabulary_is_served_to_the_client(client, ready):
    response = await client.get("/graph/view/options", headers=ready)

    body = response.json()
    assert "IMPORTS" in body["edge_types"]
    # Containment builds the hierarchy and is never drawn as a dependency.
    assert "CONTAINS" not in body["edge_types"]
    # The materialised summary would double-count the imports it is made of.
    assert "DEPENDS_ON" not in body["edge_types"]
    assert body["granularities"] == ["module", "file", "symbol"]


# -- Isolation and input handling -----------------------------------------

async def test_a_repository_the_user_has_not_connected_is_not_viewable(client, ready):
    response = await client.post(
        f"/graph/view?repo_full_name={OTHER_REPO}", json={}, headers=ready,
    )

    assert response.status_code == 404


async def test_an_id_from_another_repository_opens_nothing(client, ready):
    body = await view(client, ready, expand=[module_id(OTHER_REPO, "api")])

    assert body["expanded"] == []
    assert ids(body) == [module_id(REPO, "api"), module_id(REPO, "web")]


async def test_an_unknown_relationship_type_is_rejected_at_the_edge(client, ready):
    response = await client.post(
        f"/graph/view?repo_full_name={REPO}",
        json={"edge_types": ["CONTAINS; DROP"]},
        headers=ready,
    )

    assert response.status_code == 422


async def test_asking_to_open_more_than_the_cap_is_rejected(client, ready):
    response = await client.post(
        f"/graph/view?repo_full_name={REPO}",
        json={"expand": [f"{REPO}:m{index}" for index in range(200)]},
        headers=ready,
    )

    assert response.status_code == 422


async def test_an_unexpected_field_is_rejected_rather_than_ignored(client, ready):
    response = await client.post(
        f"/graph/view?repo_full_name={REPO}", json={"depth": 99}, headers=ready,
    )

    assert response.status_code == 422


async def test_the_view_needs_a_session(client, graph):
    response = await client.post(f"/graph/view?repo_full_name={REPO}", json={})

    assert response.status_code == 401
