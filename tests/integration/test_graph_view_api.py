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
#: More than one page of children, so the endpoint has to gather some.
WIDE_COUNT = 20
PAGE = 12
#: Widget `i` is imported `i + 1` times.
WIDE_TOTAL = WIDE_COUNT * (WIDE_COUNT + 1) // 2
#: What the eight that do not fit are worth between them.
WIDE_HIDDEN = WIDE_TOTAL - PAGE * (PAGE + 1) // 2


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

    # A third folder wide enough that drawing all of it would be unkind.
    widgets = module_id(repo, "widgets")
    wide = [f"{repo}:widgets/w{index:02d}.tsx" for index in range(WIDE_COUNT)]

    return GraphDelta(
        repo_full_name=repo,
        version="sha-1",
        upserted_nodes=(
            GraphNode(repo, NodeLabel.REPOSITORY, {"name": repo}),
            module(api, "api", 1, 2),
            module(routes, "api/routes", 2, 1),
            module(services, "api/services", 2, 1),
            module(web, "web", 1, 1),
            module(widgets, "widgets", 1, WIDE_COUNT),
            source(users, "api/routes/users.py"),
            source(store_file, "api/services/store.py"),
            source(page, "web/page.tsx"),
            *(source(node_id, f"widgets/w{index:02d}.tsx")
              for index, node_id in enumerate(wide)),
        ),
        upserted_edges=(
            GraphEdge(repo, api, EdgeType.CONTAINS),
            GraphEdge(repo, web, EdgeType.CONTAINS),
            GraphEdge(repo, widgets, EdgeType.CONTAINS),
            GraphEdge(api, routes, EdgeType.CONTAINS),
            GraphEdge(api, services, EdgeType.CONTAINS),
            GraphEdge(routes, users, EdgeType.CONTAINS),
            GraphEdge(services, store_file, EdgeType.CONTAINS),
            GraphEdge(web, page, EdgeType.CONTAINS),
            *(GraphEdge(widgets, node_id, EdgeType.CONTAINS) for node_id in wide),
            GraphEdge(page, users, EdgeType.IMPORTS, {"count": 3}),
            GraphEdge(users, store_file, EdgeType.IMPORTS, {"count": 5}),
            # Every widget is imported, so they are equally worth drawing and
            # the page boundary falls on the tie-break — which is what puts
            # connected children behind the marker, where they can be counted.
            *(GraphEdge(page, node_id, EdgeType.IMPORTS, {"count": index + 1})
              for index, node_id in enumerate(wide)),
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

    assert ids(body) == [
        module_id(REPO, "api"), module_id(REPO, "web"), module_id(REPO, "widgets"),
    ]
    assert body["expanded"] == []
    assert body["truncated"] is False
    # The repository is the frame the view is drawn in, never a box inside it.
    assert REPO not in ids(body)


async def test_a_collapsed_folder_carries_the_edges_of_everything_inside_it(client, ready):
    body = await view(client, ready)

    assert {(edge["source"], edge["target"], edge["weight"]) for edge in body["edges"]} == {
        (module_id(REPO, "web"), module_id(REPO, "api"), 3),
        # Every import of every widget, collapsed onto the one folder.
        (module_id(REPO, "web"), module_id(REPO, "widgets"), WIDE_TOTAL),
    }


async def test_opening_a_folder_draws_what_is_inside_it_beneath_it(client, ready):
    body = await view(client, ready, expand=[module_id(REPO, "api")])

    # The folder stays put; the tree grows downwards from it.
    assert module_id(REPO, "api") in ids(body)
    assert module_id(REPO, "api/routes") in ids(body)
    assert module_id(REPO, "web") in ids(body)
    assert {(line["parent"], line["child"]) for line in body["containment"]} == {
        (module_id(REPO, "api"), module_id(REPO, "api/routes")),
        (module_id(REPO, "api"), module_id(REPO, "api/services")),
    }
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
    # Every file sits under the folder that holds it.
    held = {line["child"] for line in body["containment"]}
    assert f"{REPO}:api/routes/users.py" in held


async def test_folders_are_drawn_before_the_files_beside_them(client, ready):
    body = await view(client, ready, expand=[module_id(REPO, "widgets")])

    labels = [node["label"] for node in body["nodes"]]
    assert labels.index("Module") < labels.index("File")


async def test_filtering_relationship_types_leaves_the_nodes_alone(client, ready):
    body = await view(client, ready, edge_types=["CALLS"])

    assert body["edges"] == []
    assert ids(body) == [
        module_id(REPO, "api"), module_id(REPO, "web"), module_id(REPO, "widgets"),
    ]


async def test_filtering_node_labels_keeps_only_those_nodes(client, ready):
    """Open folders survive a filter — they are the scaffolding for what is
    inside them, and hiding them would leave it hanging off nothing."""
    body = await view(client, ready, expand_to="file", node_labels=["Module"])

    assert all(node["label"] == "Module" for node in body["nodes"])
    assert module_id(REPO, "api") in ids(body)


async def test_a_shut_repository_has_no_tree_to_draw(client, ready):
    body = await view(client, ready)

    assert body["containment"] == []


async def test_every_node_says_whether_it_can_be_opened_and_whether_it_is(client, ready):
    shut = await view(client, ready)
    assert all(node["expandable"] for node in shut["nodes"])
    assert not any(node["expanded"] for node in shut["nodes"])

    opened = await view(client, ready, expand=[module_id(REPO, "api")])
    by_id = {node["id"]: node for node in opened["nodes"]}
    assert by_id[module_id(REPO, "api")]["expanded"] is True
    assert by_id[module_id(REPO, "web")]["expanded"] is False

    # Files hold nothing here, so nothing offers to open them.
    leaves = await view(client, ready, expand_to="file")
    assert not any(
        node["expandable"] for node in leaves["nodes"] if node["label"] == "File"
    )


async def test_the_filter_vocabulary_is_served_to_the_client(client, ready):
    response = await client.get("/graph/view/options", headers=ready)

    body = response.json()
    assert "IMPORTS" in body["edge_types"]
    # Containment builds the hierarchy and is never drawn as a dependency.
    assert "CONTAINS" not in body["edge_types"]
    # The materialised summary would double-count the imports it is made of.
    assert "DEPENDS_ON" not in body["edge_types"]
    assert body["granularities"] == ["module", "file", "symbol"]
    # The UI warns before a click using this, rather than repeating the number.
    assert body["page_size"] == PAGE


# -- Isolation and input handling -----------------------------------------

async def test_a_repository_the_user_has_not_connected_is_not_viewable(client, ready):
    response = await client.post(
        f"/graph/view?repo_full_name={OTHER_REPO}", json={}, headers=ready,
    )

    assert response.status_code == 404


async def test_an_id_from_another_repository_opens_nothing(client, ready):
    body = await view(client, ready, expand=[module_id(OTHER_REPO, "api")])

    assert body["expanded"] == []
    assert ids(body) == [
        module_id(REPO, "api"), module_id(REPO, "web"), module_id(REPO, "widgets"),
    ]


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


# -- Opening one subtree, and paging a wide one ---------------------------

async def test_expand_to_can_be_confined_to_one_folder(client, ready):
    """The point of the whole exercise: opening `api` costs `api`, not the repo."""
    body = await view(
        client, ready, expand_to="file", expand_from=module_id(REPO, "api"),
    )

    assert f"{REPO}:api/routes/users.py" in ids(body)
    # Everything outside the chosen subtree is untouched.
    assert module_id(REPO, "web") in ids(body)
    assert module_id(REPO, "widgets") in ids(body)
    assert f"{REPO}:web/page.tsx" not in ids(body)


async def test_confining_to_a_nested_folder_opens_the_way_down_to_it(client, ready):
    body = await view(
        client, ready, expand_to="file", expand_from=module_id(REPO, "api/routes"),
    )

    assert f"{REPO}:api/routes/users.py" in ids(body)
    # `api/services` stayed shut; `api` had to open to reach routes, and shows
    # as the folder holding both.
    assert module_id(REPO, "api/services") in ids(body)
    assert module_id(REPO, "api") in ids(body)


async def test_confining_to_another_repository_opens_nothing(client, ready):
    body = await view(
        client, ready, expand_to="symbol", expand_from=module_id(OTHER_REPO, "api"),
    )

    assert body["expanded"] == []
    assert all(node["label"] == "Module" for node in body["nodes"])


async def test_a_wide_folder_is_paged_rather_than_dumped(client, ready):
    body = await view(client, ready, expand=[module_id(REPO, "widgets")])

    widgets = [node for node in body["nodes"] if node["label"] == "File"]
    assert len(widgets) == PAGE
    assert body["overflows"] == [{
        "id": f"{module_id(REPO, 'widgets')}::more",
        "container_id": module_id(REPO, "widgets"),
        "container_name": "widgets",
        "shown": PAGE,
        "hidden": WIDE_COUNT - PAGE,
    }]
    # Paging is not a loss: the rest is on screen as a marker.
    assert body["truncated"] is False


async def test_the_marker_carries_what_it_stands_for(client, ready):
    body = await view(client, ready, expand=[module_id(REPO, "widgets")])
    marker = body["overflows"][0]["id"]

    into_marker = [edge for edge in body["edges"] if edge["target"] == marker]
    assert into_marker and into_marker[0]["weight"] == WIDE_HIDDEN
    assert into_marker[0]["collapsed"] is True


async def test_revealing_a_folder_draws_all_of_it(client, ready):
    body = await view(
        client, ready,
        expand=[module_id(REPO, "widgets")],
        reveal=[module_id(REPO, "widgets")],
    )

    assert len([node for node in body["nodes"] if node["label"] == "File"]) == WIDE_COUNT
    assert body["overflows"] == []


async def test_nothing_is_lost_between_paged_and_revealed(client, ready):
    def weight(body):
        return sum(edge["weight"] for edge in body["edges"])

    paged = await view(client, ready, expand=[module_id(REPO, "widgets")])
    revealed = await view(
        client, ready,
        expand=[module_id(REPO, "widgets")],
        reveal=[module_id(REPO, "widgets")],
    )

    assert weight(paged) == weight(revealed)


async def test_too_many_reveals_are_rejected_at_the_edge(client, ready):
    response = await client.post(
        f"/graph/view?repo_full_name={REPO}",
        json={"reveal": [f"{REPO}:m{index}" for index in range(200)]},
        headers=ready,
    )

    assert response.status_code == 422
