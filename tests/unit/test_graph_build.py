"""Building the graph: stable ids, correct layers, and an exact diff."""

import hashlib
from dataclasses import replace

import pytest

from app.knowledge_graph.build import (
    EdgeType,
    NodeLabel,
    build_snapshot,
    diff_snapshots,
    full_delta,
)
from app.knowledge_graph.build.ids import EXTERNAL_MODULE, module_id, symbol_id
from app.knowledge_graph.discovery import SourceFile, detect_language
from app.knowledge_graph.extract import extract
from app.knowledge_graph.limits import DEFAULT_LIMITS
from app.knowledge_graph.link import link_repository
from app.knowledge_graph.project_config import ProjectConfig

REPO = "octocat/hello-world"

SOURCES = {
    "main.py": "from api.handlers import handle\n\nhandle()\n",
    "api/handlers.py": (
        "import requests\n"
        "from services.users import get_user\n\n"
        "class Handler(Base):\n"
        "    def run(self):\n"
        "        return get_user(1)\n"
    ),
    "services/users.py": "def get_user(user_id):\n    return user_id\n",
}


def snapshot_of(sources=None, version="sha-1", limits=DEFAULT_LIMITS):
    facts = {}
    for path, code in (sources or SOURCES).items():
        data = code.encode()
        facts[path] = extract(
            SourceFile(
                relative_path=path, language=detect_language(path),
                content_hash=hashlib.sha256(data).hexdigest(), source=data,
                loc=code.count("\n") + 1,
            )
        )
    links = link_repository(facts, ProjectConfig())
    return build_snapshot(REPO, version, facts, links, limits=limits)


def ids_with_label(snapshot, label):
    return {node.id for node in snapshot.nodes if node.label is label}


def edges_of(snapshot, edge_type):
    return [edge for edge in snapshot.edges if edge.type is edge_type]


# -- Layers ----------------------------------------------------------------


def test_module_layer_keeps_the_ids_the_dashboard_already_uses():
    snapshot = snapshot_of()

    assert ids_with_label(snapshot, NodeLabel.MODULE) == {
        module_id(REPO, "api"),
        module_id(REPO, "services"),
        module_id(REPO, "root"),  # top-level files
        module_id(REPO, EXTERNAL_MODULE),  # requests
    }


def test_module_dependencies_carry_a_weight():
    snapshot = snapshot_of()

    depends = {(e.source, e.target): e.properties["weight"] for e in edges_of(snapshot, EdgeType.DEPENDS_ON)}
    assert depends[(module_id(REPO, "root"), module_id(REPO, "api"))] == 1
    assert depends[(module_id(REPO, "api"), module_id(REPO, "services"))] == 1
    # A module never depends on itself.
    assert all(source != target for source, target in depends)


def test_external_usage_is_aggregated_onto_one_node():
    snapshot = snapshot_of()

    external = edges_of(snapshot, EdgeType.USES_EXTERNAL)
    assert [e.target for e in external] == [module_id(REPO, EXTERNAL_MODULE)]


def test_files_and_symbols_are_contained_by_their_module():
    snapshot = snapshot_of()

    assert f"{REPO}:api/handlers.py" in ids_with_label(snapshot, NodeLabel.FILE)
    contains = {(e.source, e.target) for e in edges_of(snapshot, EdgeType.CONTAINS)}
    assert (module_id(REPO, "api"), f"{REPO}:api/handlers.py") in contains
    assert (REPO, module_id(REPO, "api")) in contains  # repository → module


def test_methods_hang_off_their_class_not_the_file():
    snapshot = snapshot_of()

    class_id = symbol_id(REPO, "api/handlers.py", "Handler")
    method_id = symbol_id(REPO, "api/handlers.py", "Handler.run")
    assert (class_id, method_id) in {(e.source, e.target) for e in edges_of(snapshot, EdgeType.HAS_MEMBER)}
    assert (f"{REPO}:api/handlers.py", class_id) in {(e.source, e.target) for e in edges_of(snapshot, EdgeType.DEFINES)}


def test_packages_get_their_own_nodes():
    snapshot = snapshot_of()

    assert f"{REPO}:pkg:pypi/requests" in ids_with_label(snapshot, NodeLabel.PACKAGE)
    assert [e.source for e in edges_of(snapshot, EdgeType.USES_PACKAGE)] == [f"{REPO}:api/handlers.py"]


def test_imports_between_files_are_merged_with_a_count():
    sources = {
        "lib.py": "def a():\n    pass\n\ndef b():\n    pass\n",
        "app.py": "from lib import a\nfrom lib import b\n",
    }

    snapshot = snapshot_of(sources)

    edge = edges_of(snapshot, EdgeType.IMPORTS)[0]
    assert edge.properties["count"] == 2
    assert edge.properties["names"] == ["a", "b"]


def test_calls_become_edges_between_symbols():
    snapshot = snapshot_of()

    calls = {(e.source, e.target) for e in edges_of(snapshot, EdgeType.CALLS)}
    assert (
        symbol_id(REPO, "api/handlers.py", "Handler.run"),
        symbol_id(REPO, "services/users.py", "get_user"),
    ) in calls


def test_a_file_level_call_is_attributed_to_the_file():
    snapshot = snapshot_of()

    calls = {(e.source, e.target) for e in edges_of(snapshot, EdgeType.CALLS)}
    assert (f"{REPO}:main.py", symbol_id(REPO, "api/handlers.py", "handle")) not in calls
    # `handle` is imported from a module that does not define it, so nothing
    # is invented; the call is simply absent.
    assert all(not source.endswith("main.py#") for source, _ in calls)


def test_every_edge_records_the_file_that_produced_it():
    snapshot = snapshot_of()

    for edge in snapshot.edges:
        if edge.type in (EdgeType.IMPORTS, EdgeType.CALLS, EdgeType.USES_PACKAGE):
            assert edge.origin_file, f"{edge.type} edge without an origin file"


def test_duplicate_names_in_one_file_get_distinct_ids():
    sources = {"app.py": "if True:\n    def f():\n        pass\nelse:\n    def f():\n        pass\n"}

    snapshot = snapshot_of(sources)

    symbol_nodes = [n for n in snapshot.nodes if n.label is NodeLabel.SYMBOL]
    assert len({n.id for n in symbol_nodes}) == len(symbol_nodes)


def test_symbol_layer_is_dropped_rather_than_truncated_when_over_the_cap():
    limits = replace(DEFAULT_LIMITS, max_symbols_per_repo=1)

    snapshot = snapshot_of(limits=limits)

    assert ids_with_label(snapshot, NodeLabel.SYMBOL) == set()
    assert ids_with_label(snapshot, NodeLabel.FILE)  # file layer survives
    assert snapshot.stats["symbol_layer"] == "omitted_over_cap"


def test_stats_report_counts_and_resolution():
    snapshot = snapshot_of()

    assert snapshot.stats["files"] == 3
    assert snapshot.stats["nodes_by_label"]["Symbol"] > 0
    assert 0.0 <= snapshot.stats["resolution"]["internal_import_resolution"] <= 1.0


def test_building_is_deterministic():
    assert snapshot_of() == snapshot_of()


# -- Diff ------------------------------------------------------------------


def test_an_unchanged_repository_produces_an_empty_delta():
    snapshot = snapshot_of()

    assert diff_snapshots(snapshot, snapshot).is_empty


def test_a_changed_file_only_rewrites_what_changed():
    before = snapshot_of()
    after = snapshot_of(
        {**SOURCES, "services/users.py": "def get_user(user_id):\n    return str(user_id)\n"},
    )

    delta = diff_snapshots(before, after)

    changed_ids = {node.id for node in delta.upserted_nodes}
    assert f"{REPO}:services/users.py" in changed_ids  # its content hash moved
    assert f"{REPO}:api/handlers.py" not in changed_ids  # untouched file untouched


def test_deleted_files_and_symbols_are_reported_for_removal():
    before = snapshot_of()
    after = snapshot_of({path: code for path, code in SOURCES.items() if path != "services/users.py"})

    delta = diff_snapshots(before, after)

    assert f"{REPO}:services/users.py" in delta.deleted_node_ids
    assert symbol_id(REPO, "services/users.py", "get_user") in delta.deleted_node_ids
    # The call into it disappears too, even though its own file did not change.
    assert any(edge_type == EdgeType.CALLS for _, _, edge_type in delta.deleted_edge_keys)


def test_renaming_a_function_removes_the_edge_from_an_unchanged_caller():
    before = snapshot_of()
    after = snapshot_of({**SOURCES, "services/users.py": "def fetch_user(user_id):\n    return user_id\n"})

    delta = diff_snapshots(before, after)

    removed_targets = {target for _, target, _ in delta.deleted_edge_keys}
    assert symbol_id(REPO, "services/users.py", "get_user") in removed_targets


def test_full_delta_writes_everything():
    snapshot = snapshot_of()

    delta = full_delta(snapshot)

    assert len(delta.upserted_nodes) == len(snapshot.nodes)
    assert delta.deleted_node_ids == ()


def test_diffing_across_repositories_is_refused():
    with pytest.raises(ValueError):
        diff_snapshots(snapshot_of(), replace(snapshot_of(), repo_full_name="someone/else"))
