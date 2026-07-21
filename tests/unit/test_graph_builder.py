from app.parsing.ast_service import FileParse
from app.parsing.graph_builder import EXTERNAL_NODE, build_graph, node_id

REPO = "octocat/hello-world"


def fp(path, imports=(), definitions=()):
    return FileParse(path=path, language="python", imports=list(imports), definitions=list(definitions))


def test_one_node_per_top_level_directory_and_root():
    graph = build_graph(REPO, [
        fp("main.py"),
        fp("api/handlers.py", definitions=[{"name": "h", "kind": "function"}]),
        fp("api/routes.py"),
        fp("services/users.py"),
    ])
    names = [n["name"] for n in graph["nodes"]]
    assert names == ["api", "root", "services"]
    api = next(n for n in graph["nodes"] if n["name"] == "api")
    assert api["file_count"] == 2
    assert api["definition_count"] == 1
    assert api["id"] == node_id(REPO, "api")


def test_internal_imports_become_cross_node_edges():
    graph = build_graph(REPO, [
        fp("api/handlers.py", imports=["services.users", "api.routes"]),
        fp("api/routes.py"),
        fp("services/users.py"),
    ])
    assert graph["edges"] == [{
        "source": node_id(REPO, "api"),
        "target": node_id(REPO, "services"),
        "type": "DEPENDS_ON",
        "weight": 1,
    }]  # same-node import (api -> api) produces no edge


def test_external_imports_collapse_into_single_node():
    graph = build_graph(REPO, [
        fp("api/handlers.py", imports=["fastapi", "pydantic", "numpy"]),
    ])
    external = [n for n in graph["nodes"] if n["kind"] == "external"]
    assert len(external) == 1
    assert external[0]["id"] == node_id(REPO, EXTERNAL_NODE)
    assert graph["edges"] == [{
        "source": node_id(REPO, "api"),
        "target": node_id(REPO, EXTERNAL_NODE),
        "type": "EXTERNAL_DEPENDENCY",
        "weight": 3,
    }]


def test_relative_imports_resolved_including_index_files():
    parses = [
        FileParse(path="web/app.ts", language="typescript", imports=["../shared", "./view"]),
        FileParse(path="web/view.ts", language="typescript"),
        FileParse(path="shared/index.ts", language="typescript"),
    ]
    graph = build_graph(REPO, parses)
    assert {(e["source"], e["target"]) for e in graph["edges"]} == {
        (node_id(REPO, "web"), node_id(REPO, "shared")),
    }


def test_ids_stable_across_reparse():
    parses = [fp("api/handlers.py", imports=["services.users"]), fp("services/users.py")]
    first = build_graph(REPO, parses)
    second = build_graph(REPO, list(reversed(parses)))
    assert first == second


def test_edge_weights_accumulate():
    graph = build_graph(REPO, [
        fp("api/a.py", imports=["services.x"]),
        fp("api/b.py", imports=["services.y"]),
        fp("services/x.py"),
        fp("services/y.py"),
    ])
    assert graph["edges"][0]["weight"] == 2
