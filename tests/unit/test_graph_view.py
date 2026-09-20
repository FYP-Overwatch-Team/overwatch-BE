"""The interactive view: nested modules, expansion, and rolled-up edges."""

import hashlib

import pytest

from app.knowledge_graph.build import EdgeType, NodeLabel, build_snapshot
from app.knowledge_graph.build.ids import (
    EXTERNAL_MODULE,
    ROUTES_MODULE,
    file_id,
    module_id,
    repository_id,
    symbol_id,
)
from app.knowledge_graph.discovery import SourceFile, detect_language
from app.knowledge_graph.extract import extract
from app.knowledge_graph.link import link_repository
from app.knowledge_graph.project_config import ProjectConfig
from app.knowledge_graph.view import (
    CONTAINMENT_EDGE_TYPES,
    SUMMARY_EDGE_TYPES,
    VIEWABLE_EDGE_TYPES,
    RollupEdge,
    ViewCaps,
    ViewFilters,
    ViewNode,
    ViewRequest,
    container_chain,
    parent_of,
    project,
)

REPO = "octocat/hello-world"

# A repository that actually nests, so "top-level only" is visibly wrong.
SOURCES = {
    "main.py": "from api.routes.users import list_users\n\nlist_users()\n",
    "api/routes/users.py": (
        "import requests\n"
        "from api.services.store import fetch\n\n"
        "def list_users():\n"
        "    return fetch()\n"
    ),
    "api/services/store.py": "def fetch():\n    return []\n",
}


def snapshot_of(sources=None):
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
    return build_snapshot(REPO, "sha-1", facts, links)


# -- The hierarchy ---------------------------------------------------------

def test_modules_exist_at_every_directory_depth():
    modules = {
        node.id for node in snapshot_of().nodes if node.label is NodeLabel.MODULE
    }
    assert module_id(REPO, "api") in modules
    assert module_id(REPO, "api/routes") in modules
    assert module_id(REPO, "api/services") in modules


def test_a_collapsed_module_reports_its_whole_subtree():
    by_id = {node.id: node for node in snapshot_of().nodes}
    api = by_id[module_id(REPO, "api")]
    # Two files live under api/, in two different sub-directories.
    assert api.properties["file_count"] == 2
    assert by_id[module_id(REPO, "api/routes")].properties["file_count"] == 1


def test_only_outermost_modules_hang_off_the_repository():
    snapshot = snapshot_of()
    from_repo = {
        edge.target for edge in snapshot.edges
        if edge.type is EdgeType.CONTAINS and edge.source == repository_id(REPO)
    }
    assert module_id(REPO, "api") in from_repo
    assert module_id(REPO, "api/routes") not in from_repo


def test_a_file_is_contained_by_its_own_directory_not_the_top_one():
    snapshot = snapshot_of()
    owner = next(
        edge.source for edge in snapshot.edges
        if edge.type is EdgeType.CONTAINS
        and edge.target == file_id(REPO, "api/routes/users.py")
    )
    assert owner == module_id(REPO, "api/routes")


def test_packages_hang_under_the_external_module():
    snapshot = snapshot_of()
    packages = {node.id for node in snapshot.nodes if node.label is NodeLabel.PACKAGE}
    contained = {
        edge.target for edge in snapshot.edges
        if edge.type is EdgeType.CONTAINS
        and edge.source == module_id(REPO, EXTERNAL_MODULE)
    }
    assert packages and packages <= contained


def test_the_chain_derived_from_ids_matches_the_edges_the_builder_writes():
    """The view navigates by id arithmetic; the graph navigates by edges.

    If those two ever disagree, expanding a node shows the wrong contents. So
    every containment edge in a real snapshot is checked against the parent
    the view would compute from the child's id alone.
    """
    snapshot = snapshot_of()
    labels = {node.id: node.label for node in snapshot.nodes}

    for edge in snapshot.edges:
        if edge.type not in CONTAINMENT_EDGE_TYPES:
            continue
        if edge.type is EdgeType.HAS_MEMBER:
            continue  # a method's parent is a symbol; the view stops at the file
        assert parent_of(edge.target, REPO, labels[edge.target]) == edge.source, (
            f"{edge.target} is contained by {edge.source} but the view would "
            f"place it under {parent_of(edge.target, REPO, labels[edge.target])}"
        )


def test_a_method_is_placed_in_its_file_not_its_class():
    chain = container_chain(
        symbol_id(REPO, "api/routes/users.py", "Handler.run"), REPO, NodeLabel.SYMBOL,
    )
    assert chain[0] == file_id(REPO, "api/routes/users.py")
    assert chain[-1] == repository_id(REPO)


def test_routes_and_packages_are_placed_under_their_synthetic_modules():
    assert container_chain(f"{REPO}:pkg:pypi/requests", REPO, NodeLabel.PACKAGE)[0] == (
        module_id(REPO, EXTERNAL_MODULE)
    )
    assert container_chain(f"{REPO}:route:GET /users", REPO, NodeLabel.ROUTE)[0] == (
        module_id(REPO, ROUTES_MODULE)
    )


def test_an_id_from_another_repository_is_never_placed():
    assert container_chain("someone/else:api/users.py", REPO, NodeLabel.FILE) == []


# -- Projection ------------------------------------------------------------

def module(name, children=1):
    return ViewNode(
        id=module_id(REPO, name), name=name, label=NodeLabel.MODULE,
        kind="module", path=name, depth=name.count("/") + 1, child_count=children,
    )


def file_node(path):
    return ViewNode(
        id=file_id(REPO, path), name=path.rpartition("/")[2],
        label=NodeLabel.FILE, path=path, child_count=0,
    )


TREE = {
    repository_id(REPO): [module("api", children=2), module("web")],
    module_id(REPO, "api"): [module("api/routes"), module("api/services")],
    module_id(REPO, "api/routes"): [file_node("api/routes/users.py")],
    module_id(REPO, "api/services"): [file_node("api/services/store.py")],
    module_id(REPO, "web"): [file_node("web/page.tsx")],
}

EDGES = (
    RollupEdge(
        source=file_id(REPO, "web/page.tsx"),
        target=file_id(REPO, "api/routes/users.py"),
        type=EdgeType.IMPORTS,
        source_label=NodeLabel.FILE, target_label=NodeLabel.FILE, weight=3,
    ),
    RollupEdge(
        source=file_id(REPO, "api/routes/users.py"),
        target=file_id(REPO, "api/services/store.py"),
        type=EdgeType.IMPORTS,
        source_label=NodeLabel.FILE, target_label=NodeLabel.FILE, weight=5,
    ),
)


def view(expanded=(), filters=None, caps=None):
    return project(
        REPO, TREE, EDGES,
        ViewRequest(
            expanded=expanded,
            filters=filters or ViewFilters(),
            caps=caps or ViewCaps(),
        ),
    )


def test_the_default_view_is_the_outermost_modules():
    assert [node.id for node in view().nodes] == [
        module_id(REPO, "api"), module_id(REPO, "web"),
    ]


def test_an_edge_between_two_files_is_drawn_between_the_modules_holding_them():
    edges = view().edges
    assert len(edges) == 1
    assert (edges[0].source, edges[0].target) == (
        module_id(REPO, "web"), module_id(REPO, "api"),
    )
    assert edges[0].weight == 3
    assert edges[0].collapsed is True


def test_a_relationship_inside_a_collapsed_module_is_not_drawn():
    # api/routes -> api/services lives entirely inside `api`.
    assert all(edge.source != edge.target for edge in view().edges)
    assert len(view().edges) == 1


def test_expanding_a_module_replaces_it_with_its_children():
    ids = [node.id for node in view(expanded=[module_id(REPO, "api")]).nodes]
    assert module_id(REPO, "api") not in ids
    assert module_id(REPO, "api/routes") in ids
    assert module_id(REPO, "api/services") in ids
    assert module_id(REPO, "web") in ids  # untouched


def test_expanding_reveals_the_relationship_that_was_hidden_inside():
    edges = view(expanded=[module_id(REPO, "api")]).edges
    internal = [
        edge for edge in edges
        if edge.source == module_id(REPO, "api/routes")
        and edge.target == module_id(REPO, "api/services")
    ]
    assert internal and internal[0].weight == 5


def test_collapsing_returns_exactly_the_view_you_started_from():
    opened = view(expanded=[module_id(REPO, "api")])
    closed = view()
    assert opened.nodes != closed.nodes
    assert view(expanded=()).nodes == closed.nodes
    assert view(expanded=()).edges == closed.edges


def test_expanding_two_levels_reaches_the_files():
    ids = [
        node.id for node in
        view(expanded=[module_id(REPO, "api"), module_id(REPO, "api/routes")]).nodes
    ]
    assert file_id(REPO, "api/routes/users.py") in ids
    assert module_id(REPO, "api/services") in ids  # sibling stays collapsed


def test_an_expansion_whose_parent_is_shut_is_ignored_and_reported_as_such():
    result = view(expanded=[module_id(REPO, "api/routes")])
    assert result.expanded == ()
    assert [node.id for node in result.nodes] == [
        module_id(REPO, "api"), module_id(REPO, "web"),
    ]


def test_total_weight_is_conserved_when_a_module_is_opened():
    def total(graph):
        return sum(edge.weight for edge in graph.edges)

    # Opening `api` exposes the 5 imports that were previously internal.
    assert total(view()) == 3
    assert total(view(expanded=[module_id(REPO, "api")])) == 3 + 5


def test_filtering_out_a_relationship_type_removes_its_edges():
    filters = ViewFilters(edge_types=VIEWABLE_EDGE_TYPES - {EdgeType.IMPORTS})
    assert view(filters=filters).edges == ()
    assert view(filters=filters).nodes  # the nodes are still there


def test_filtering_out_a_node_label_removes_those_nodes():
    filters = ViewFilters(node_labels=frozenset({NodeLabel.FILE}))
    result = view(expanded=[module_id(REPO, "api"), module_id(REPO, "api/routes")], filters=filters)
    assert [node.id for node in result.nodes] == [file_id(REPO, "api/routes/users.py")]


def test_a_cap_truncates_and_says_so():
    result = view(caps=ViewCaps(max_nodes=1))
    assert len(result.nodes) == 1
    assert result.truncated is True


def test_the_summary_edges_are_never_viewable():
    """They are derived from imports; rolling them up would double-count."""
    assert not (SUMMARY_EDGE_TYPES & VIEWABLE_EDGE_TYPES)
    assert not (CONTAINMENT_EDGE_TYPES & VIEWABLE_EDGE_TYPES)


@pytest.mark.parametrize("unknown", ["", "nope", "octocat/other:api/x.py"])
def test_an_id_outside_this_repository_has_no_place_in_the_view(unknown):
    assert container_chain(unknown, REPO, NodeLabel.FILE) == []


def test_the_repository_itself_is_the_frame_not_a_node_in_it():
    assert container_chain(repository_id(REPO), REPO, NodeLabel.REPOSITORY) == []


# -- Paging a wide container ----------------------------------------------

def wide_file(index: int, degree: int) -> ViewNode:
    path = f"ui/part{index:02d}.tsx"
    return ViewNode(
        id=file_id(REPO, path), name=f"part{index:02d}.tsx",
        label=NodeLabel.FILE, path=path, child_count=0, degree=degree,
    )


#: Twenty files in one folder, each less connected than the last.
WIDE_FILES = [wide_file(index, degree=100 - index) for index in range(20)]

WIDE_TREE = {
    repository_id(REPO): [module("ui", children=20), module("web")],
    module_id(REPO, "ui"): WIDE_FILES,
    module_id(REPO, "web"): [file_node("web/page.tsx")],
}

#: `web/page.tsx` imports one drawn file and one that will not fit.
WIDE_EDGES = (
    RollupEdge(
        source=file_id(REPO, "web/page.tsx"), target=WIDE_FILES[0].id,
        type=EdgeType.IMPORTS,
        source_label=NodeLabel.FILE, target_label=NodeLabel.FILE, weight=2,
    ),
    RollupEdge(
        source=file_id(REPO, "web/page.tsx"), target=WIDE_FILES[19].id,
        type=EdgeType.IMPORTS,
        source_label=NodeLabel.FILE, target_label=NodeLabel.FILE, weight=7,
    ),
)


def wide_view(revealed=(), page_size=12):
    return project(
        REPO, WIDE_TREE, WIDE_EDGES,
        ViewRequest(
            expanded=[module_id(REPO, "ui")],
            revealed=revealed,
            caps=ViewCaps(page_size=page_size),
        ),
    )


def test_a_wide_folder_draws_a_page_and_gathers_the_rest():
    view = wide_view()
    files = [node for node in view.nodes if node.label is NodeLabel.FILE]

    assert len(files) == 12
    assert len(view.overflows) == 1
    assert view.overflows[0].hidden == 8
    assert view.overflows[0].shown == 12
    assert view.overflows[0].container_id == module_id(REPO, "ui")


def test_the_page_keeps_the_most_connected_children():
    drawn = {node.id for node in wide_view().nodes}

    assert WIDE_FILES[0].id in drawn      # most connected
    assert WIDE_FILES[19].id not in drawn  # least


def test_the_marker_carries_the_relationships_of_what_it_hides():
    view = wide_view()
    marker = view.overflows[0]

    to_marker = [edge for edge in view.edges if edge.target == marker.id]
    assert to_marker and to_marker[0].weight == 7
    assert to_marker[0].collapsed is True


def test_paging_never_loses_a_relationship():
    """Whether a file is drawn or gathered, its imports still show up."""
    assert sum(edge.weight for edge in wide_view().edges) == 2 + 7
    assert sum(edge.weight for edge in wide_view(page_size=50).edges) == 2 + 7


def test_revealing_draws_the_rest_and_retires_the_marker():
    view = wide_view(revealed=[module_id(REPO, "ui")])

    assert len([node for node in view.nodes if node.label is NodeLabel.FILE]) == 20
    assert view.overflows == ()
    assert WIDE_FILES[19].id in {node.id for node in view.nodes}


def test_a_marker_id_cannot_collide_with_a_graph_id():
    """Graph ids separate on `/` and `#`; the marker uses neither."""
    marker = wide_view().overflows[0]

    assert "::more" in marker.id
    assert marker.id not in {node.id for node in WIDE_FILES}


def test_paging_is_not_reported_as_truncation():
    """Nothing was lost — the marker is on screen and can be opened."""
    assert wide_view().truncated is False
