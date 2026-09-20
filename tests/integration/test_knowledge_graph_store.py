"""The graph store, checked against both implementations.

The Neo4j variant runs whenever a database is reachable (always in CI, where a
Neo4j service container is configured). It is skipped otherwise, so the suite
still runs on a laptop with nothing started.
"""

import os

import pytest

from app.db.neo4j_client import Neo4jStore
from app.knowledge_graph.build.ids import EXTERNAL_MODULE, module_id
from app.knowledge_graph.build.model import (
    EdgeType,
    GraphDelta,
    GraphEdge,
    GraphNode,
    NodeLabel,
)
from app.services.knowledge_graph_store import (
    InMemoryKnowledgeGraphStore,
    Neo4jKnowledgeGraphStore,
)

REPO = "octocat/hello-world"
OTHER_REPO = "someone-else/private"


def module_node(repo: str, name: str, kind: str = "module", **properties) -> GraphNode:
    return GraphNode(
        id=module_id(repo, name),
        label=NodeLabel.MODULE,
        properties={
            "name": name, "kind": kind, "file_count": 1, "definition_count": 2, **properties,
        },
    )


def file_node(repo: str, path: str) -> GraphNode:
    return GraphNode(
        id=f"{repo}:{path}",
        label=NodeLabel.FILE,
        properties={"path": path, "language": "python", "loc": 10},
    )


def starter_delta(repo: str = REPO, version: str = "sha-1") -> GraphDelta:
    return GraphDelta(
        repo_full_name=repo,
        version=version,
        upserted_nodes=(
            module_node(repo, "api"),
            module_node(repo, "services"),
            module_node(repo, EXTERNAL_MODULE, kind="external", file_count=0, definition_count=0),
            file_node(repo, "api/handlers.py"),
        ),
        upserted_edges=(
            GraphEdge(module_id(repo, "api"), module_id(repo, "services"), EdgeType.DEPENDS_ON, {"weight": 3}),
            GraphEdge(module_id(repo, "api"), module_id(repo, EXTERNAL_MODULE), EdgeType.USES_EXTERNAL, {"weight": 5}),
            GraphEdge(module_id(repo, "api"), f"{repo}:api/handlers.py", EdgeType.CONTAINS, origin_file="api/handlers.py"),
        ),
    )


async def _neo4j_store():
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    password = os.getenv("NEO4J_PASSWORD", "localdevpassword")
    store = Neo4jStore(uri=uri, user=os.getenv("NEO4J_USER", "neo4j"), password=password)
    try:
        await store.run("RETURN 1")
    except Exception as exc:  # not running locally
        await store.close()
        pytest.skip(f"Neo4j unavailable: {type(exc).__name__}")
    return store


@pytest.fixture(params=["memory", "neo4j"])
async def store(request):
    if request.param == "memory":
        yield InMemoryKnowledgeGraphStore()
        return

    driver = await _neo4j_store()
    graph_store = Neo4jKnowledgeGraphStore(driver)
    await graph_store.ensure_schema()
    for repo in (REPO, OTHER_REPO):
        await graph_store.delete_repository(repo)
    yield graph_store
    for repo in (REPO, OTHER_REPO):
        await graph_store.delete_repository(repo)
    await driver.close()


async def test_applying_a_delta_stores_nodes_and_edges(store):
    await store.apply(starter_delta())

    assert await store.node_ids(REPO) == {
        module_id(REPO, "api"), module_id(REPO, "services"),
        module_id(REPO, EXTERNAL_MODULE), f"{REPO}:api/handlers.py",
    }


async def test_module_view_returns_the_shape_the_dashboard_expects(store):
    await store.apply(starter_delta())

    view = await store.module_view(REPO)

    assert {node["name"] for node in view["nodes"]} == {"api", "services", EXTERNAL_MODULE}
    assert {node["kind"] for node in view["nodes"]} == {"module", "external"}
    assert next(node for node in view["nodes"] if node["name"] == "api")["file_count"] == 1

    by_type = {edge["type"]: edge for edge in view["edges"]}
    assert by_type["DEPENDS_ON"]["weight"] == 3
    # The wire name the frontend already knows, not the internal type.
    assert by_type["EXTERNAL_DEPENDENCY"]["weight"] == 5
    assert "CONTAINS" not in by_type  # structural edges stay out of the view


async def test_re_applying_the_same_delta_is_idempotent(store):
    await store.apply(starter_delta())
    await store.apply(starter_delta())

    view = await store.module_view(REPO)
    assert len(view["nodes"]) == 3
    assert len(view["edges"]) == 2


async def test_upserts_update_properties_in_place(store):
    await store.apply(starter_delta())

    await store.apply(
        GraphDelta(
            repo_full_name=REPO,
            version="sha-2",
            upserted_nodes=(module_node(REPO, "api", file_count=42),),
            upserted_edges=(
                GraphEdge(module_id(REPO, "api"), module_id(REPO, "services"), EdgeType.DEPENDS_ON, {"weight": 9}),
            ),
        )
    )

    view = await store.module_view(REPO)
    api = next(node for node in view["nodes"] if node["name"] == "api")
    assert api["file_count"] == 42
    assert next(e for e in view["edges"] if e["type"] == "DEPENDS_ON")["weight"] == 9
    assert len(view["nodes"]) == 3  # no duplicates created


async def test_deletions_remove_nodes_and_their_edges(store):
    await store.apply(starter_delta())

    await store.apply(
        GraphDelta(
            repo_full_name=REPO,
            version="sha-2",
            deleted_node_ids=(f"{REPO}:api/handlers.py",),
            deleted_edge_keys=((module_id(REPO, "api"), module_id(REPO, "services"), "DEPENDS_ON"),),
        )
    )

    assert f"{REPO}:api/handlers.py" not in await store.node_ids(REPO)
    view = await store.module_view(REPO)
    assert [edge["type"] for edge in view["edges"]] == ["EXTERNAL_DEPENDENCY"]


async def test_one_repository_cannot_touch_another(store):
    await store.apply(starter_delta(REPO))
    await store.apply(starter_delta(OTHER_REPO))

    # A delta that names another repository's node ids must not delete them.
    await store.apply(
        GraphDelta(
            repo_full_name=REPO,
            version="sha-2",
            deleted_node_ids=(module_id(OTHER_REPO, "api"),),
            deleted_edge_keys=((module_id(OTHER_REPO, "api"), module_id(OTHER_REPO, "services"), "DEPENDS_ON"),),
        )
    )

    assert module_id(OTHER_REPO, "api") in await store.node_ids(OTHER_REPO)
    assert len((await store.module_view(OTHER_REPO))["edges"]) == 2


async def test_deleting_a_repository_leaves_others_intact(store):
    await store.apply(starter_delta(REPO))
    await store.apply(starter_delta(OTHER_REPO))

    await store.delete_repository(REPO)

    assert await store.node_ids(REPO) == set()
    assert len(await store.node_ids(OTHER_REPO)) == 4


async def test_an_empty_delta_is_a_no_op(store):
    await store.apply(starter_delta())

    await store.apply(GraphDelta(repo_full_name=REPO, version="sha-2"))

    assert len(await store.node_ids(REPO)) == 4


def detail_delta(repo: str = REPO) -> GraphDelta:
    """A module containing a file that defines two symbols, one calling the other."""
    module, file = module_id(repo, "api"), f"{repo}:api/handlers.py"
    handler, helper = f"{file}#handle", f"{file}#helper"
    return GraphDelta(
        repo_full_name=repo,
        version="sha-1",
        upserted_nodes=(
            module_node(repo, "api"),
            GraphNode(file, NodeLabel.FILE, {"name": "handlers.py", "path": "api/handlers.py", "language": "python"}),
            GraphNode(handler, NodeLabel.SYMBOL, {"name": "handle", "kind": "function", "signature": "handle(request)"}),
            GraphNode(helper, NodeLabel.SYMBOL, {"name": "helper", "kind": "function", "signature": "helper()"}),
        ),
        upserted_edges=(
            GraphEdge(module, file, EdgeType.CONTAINS),
            GraphEdge(file, handler, EdgeType.DEFINES),
            GraphEdge(file, helper, EdgeType.DEFINES),
            GraphEdge(handler, helper, EdgeType.CALLS, {"count": 2}),
        ),
    )


async def test_node_detail_returns_the_node_and_its_children(store):
    await store.apply(detail_delta())

    detail = await store.node_detail(REPO, f"{REPO}:api/handlers.py")

    assert detail["properties"]["path"] == "api/handlers.py"
    assert "File" in detail["labels"]
    assert {child["name"] for child in detail["children"]} == {"handle", "helper"}


async def test_node_detail_is_scoped_to_the_repository(store):
    await store.apply(detail_delta(OTHER_REPO))

    assert await store.node_detail(REPO, f"{OTHER_REPO}:api/handlers.py") is None
    assert await store.node_detail(REPO, "no/such:node") is None


async def test_neighbours_follow_direction(store):
    await store.apply(detail_delta())
    handler = f"{REPO}:api/handlers.py#handle"

    outgoing = await store.neighbours(REPO, handler, direction="out", depth=1)
    incoming = await store.neighbours(REPO, handler, direction="in", depth=1)

    assert {node["name"] for node in outgoing["nodes"]} == {"helper"}
    assert {node["name"] for node in incoming["nodes"]} == {"handlers.py"}


async def test_neighbours_can_filter_by_edge_type(store):
    await store.apply(detail_delta())
    file_id = f"{REPO}:api/handlers.py"

    only_calls = await store.neighbours(REPO, file_id, edge_types=["CALLS"], depth=1)

    assert only_calls["nodes"] == []  # the file defines, it does not call


async def test_neighbours_reach_further_with_depth(store):
    await store.apply(detail_delta())
    module = module_id(REPO, "api")

    shallow = await store.neighbours(REPO, module, depth=1)
    deeper = await store.neighbours(REPO, module, depth=2)

    assert {node["name"] for node in shallow["nodes"]} == {"handlers.py"}
    assert {node["name"] for node in deeper["nodes"]} == {"handlers.py", "handle", "helper"}


async def test_neighbours_stay_inside_the_repository(store):
    await store.apply(detail_delta())
    await store.apply(detail_delta(OTHER_REPO))

    result = await store.neighbours(REPO, module_id(OTHER_REPO, "api"), depth=2)

    assert result == {"nodes": [], "edges": []}


async def test_search_matches_symbols_and_files_by_name(store):
    await store.apply(detail_delta())

    assert {row["name"] for row in await store.search(REPO, "help")} == {"helper"}
    assert {row["name"] for row in await store.search(REPO, "handlers")} == {"handlers.py"}
    assert await store.search(REPO, "nothing-matches") == []


async def test_search_does_not_cross_repositories(store):
    await store.apply(detail_delta(OTHER_REPO))

    assert await store.search(REPO, "helper") == []


async def test_counts_report_what_is_stored(store):
    await store.apply(detail_delta())

    counts = await store.counts(REPO)

    assert counts["nodes_by_label"] == {"Module": 1, "File": 1, "Symbol": 2}
    assert counts["edges_by_type"] == {"CONTAINS": 1, "DEFINES": 2, "CALLS": 1}


def test_labels_and_types_are_rejected_unless_they_come_from_the_enums():
    from app.services.knowledge_graph_store import _safe_label, _safe_type

    assert _safe_label(NodeLabel.SYMBOL) == "Symbol"
    assert _safe_type(EdgeType.CALLS) == "CALLS"
    for hostile in ("Symbol) DETACH DELETE (n", "`Symbol`", 1, None):
        with pytest.raises(ValueError):
            _safe_label(hostile)
        with pytest.raises(ValueError):
            _safe_type(hostile)


# -- Queries the interactive view is built from ----------------------------

def nested_delta(repo: str = REPO) -> GraphDelta:
    """Two nested modules, a file in each, and a call across them."""
    routes, store_dir = module_id(repo, "api/routes"), module_id(repo, "api/services")
    users, storage = f"{repo}:api/routes/users.py", f"{repo}:api/services/store.py"
    list_users, fetch = f"{users}#list_users", f"{storage}#fetch"

    return GraphDelta(
        repo_full_name=repo,
        version="sha-2",
        upserted_nodes=(
            module_node(repo, "api", depth=1, file_count=2),
            module_node(repo, "api/routes", depth=2),
            module_node(repo, "api/services", depth=2),
            file_node(repo, "api/routes/users.py"),
            file_node(repo, "api/services/store.py"),
            GraphNode(list_users, NodeLabel.SYMBOL, {
                "name": "list_users", "kind": "function", "file_path": "api/routes/users.py",
            }),
            GraphNode(fetch, NodeLabel.SYMBOL, {
                "name": "fetch", "kind": "function", "file_path": "api/services/store.py",
            }),
        ),
        upserted_edges=(
            GraphEdge(module_id(repo, "api"), routes, EdgeType.CONTAINS),
            GraphEdge(module_id(repo, "api"), store_dir, EdgeType.CONTAINS),
            GraphEdge(routes, users, EdgeType.CONTAINS),
            GraphEdge(store_dir, storage, EdgeType.CONTAINS),
            GraphEdge(users, list_users, EdgeType.DEFINES),
            GraphEdge(storage, fetch, EdgeType.DEFINES),
            GraphEdge(list_users, fetch, EdgeType.CALLS, {"count": 4}),
        ),
    )


async def test_view_children_returns_one_level_with_labels_and_counts(store):
    await store.apply(nested_delta())

    children = await store.view_children(REPO, [module_id(REPO, "api")])
    inside_api = children[module_id(REPO, "api")]

    assert [node.id for node in inside_api] == [
        module_id(REPO, "api/routes"), module_id(REPO, "api/services"),
    ]
    assert all(node.label is NodeLabel.MODULE for node in inside_api)
    # Each holds one file, so each is still worth opening.
    assert all(node.child_count == 1 for node in inside_api)


async def test_view_children_reports_a_leaf_as_having_nothing_to_open(store):
    await store.apply(nested_delta())

    children = await store.view_children(REPO, [f"{REPO}:api/routes/users.py"])
    symbols = children[f"{REPO}:api/routes/users.py"]

    assert [node.name for node in symbols] == ["list_users"]
    assert symbols[0].child_count == 0


async def test_view_children_never_crosses_into_another_repository(store):
    await store.apply(nested_delta(REPO))
    await store.apply(nested_delta(OTHER_REPO))

    children = await store.view_children(REPO, [module_id(OTHER_REPO, "api")])

    assert children == {}


async def test_rollup_folds_a_call_into_the_files_that_hold_the_symbols(store):
    await store.apply(nested_delta())

    edges = await store.rollup_edges(REPO, edge_types=[str(EdgeType.CALLS)])

    assert len(edges) == 1
    assert edges[0].source == f"{REPO}:api/routes/users.py"
    assert edges[0].target == f"{REPO}:api/services/store.py"
    assert edges[0].source_label is NodeLabel.FILE
    assert edges[0].weight == 4  # the call count travels with the edge


async def test_rollup_keeps_symbols_apart_once_their_file_is_open(store):
    await store.apply(nested_delta())

    edges = await store.rollup_edges(
        REPO,
        edge_types=[str(EdgeType.CALLS)],
        expanded_files=["api/routes/users.py", "api/services/store.py"],
    )

    assert edges[0].source == f"{REPO}:api/routes/users.py#list_users"
    assert edges[0].source_label is NodeLabel.SYMBOL


async def test_rollup_is_scoped_to_one_repository(store):
    await store.apply(nested_delta(REPO))
    await store.apply(nested_delta(OTHER_REPO))

    edges = await store.rollup_edges(REPO, edge_types=[str(EdgeType.CALLS)])

    assert {edge.source for edge in edges} == {f"{REPO}:api/routes/users.py"}


async def test_rollup_rejects_an_edge_type_we_do_not_define(store):
    with pytest.raises(ValueError):
        await store.rollup_edges(REPO, edge_types=["DROP DATABASE"])
