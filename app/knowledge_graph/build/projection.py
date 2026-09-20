"""The module layer: one node per directory, at every depth.

A *module* is a directory. `api` and `api/routes` are both modules, wired
parent-to-child, so the graph can be read at whatever granularity a question
needs — the whole repository as a handful of boxes, or one folder opened down
to its files.

Two modules stand for something that is not a directory: `__external__` owns
the third-party packages, and `__routes__` owns the HTTP routes. Neither has a
path, so neither is ever split like one.

Counts are **subtree totals**: `api`'s file count includes everything under
`api/routes`. A collapsed node therefore reports what it is hiding.

The weighted `DEPENDS_ON` / `USES_EXTERNAL` edges are materialised for the
*top* level only. They are a cheap summary for the LLM prompt; the interactive
view derives its edges from the real import and call edges instead, which is
why the view must never also read these (it would count the same import
twice).
"""

from collections import Counter, defaultdict
from collections.abc import Mapping

from app.knowledge_graph.build.ids import (
    EXTERNAL_MODULE,
    ROUTES_MODULE,
    module_ancestry,
    module_depth,
    module_display_name,
    module_id,
    module_of,
    owning_module,
    parent_module,
)
from app.knowledge_graph.build.model import EdgeType, GraphEdge, GraphNode, NodeLabel
from app.knowledge_graph.facts import FileFacts
from app.knowledge_graph.link import RepositoryLinks


def build_module_layer(
    repo_full_name: str,
    facts_by_path: Mapping[str, FileFacts],
    links: RepositoryLinks,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Module nodes, the containment between them, and the top-level summary."""
    file_counts, symbol_counts, languages = _subtree_totals(facts_by_path)

    nodes = [
        _module_node(repo_full_name, module, file_counts, symbol_counts, languages)
        for module in sorted(file_counts)
    ]
    nodes.extend(
        _synthetic_module_node(repo_full_name, module, kind)
        for module, kind, present in (
            (EXTERNAL_MODULE, "external", bool(links.packages)),
            (ROUTES_MODULE, "routes", bool(links.routes)),
        )
        if present
    )

    known = {node.id for node in nodes}
    edges = [
        GraphEdge(
            source=module_id(repo_full_name, parent),
            target=module_id(repo_full_name, module),
            type=EdgeType.CONTAINS,
        )
        for module in sorted(file_counts)
        if (parent := parent_module(module)) is not None
        and module_id(repo_full_name, parent) in known
    ]
    edges.extend(_top_level_summary(repo_full_name, links, known))

    return nodes, edges


def _subtree_totals(
    facts_by_path: Mapping[str, FileFacts],
) -> tuple[Counter[str], Counter[str], dict[str, Counter[str]]]:
    """Files, definitions and languages per module, counted over the subtree."""
    file_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    languages: dict[str, Counter[str]] = defaultdict(Counter)

    for path, facts in facts_by_path.items():
        # A file is counted against its own directory and every directory above
        # it, so a collapsed module reports the whole subtree it stands for.
        for module in module_ancestry(owning_module(path)):
            file_counts[module] += 1
            symbol_counts[module] += len(facts.definitions)
            languages[module][facts.language] += 1

    return file_counts, symbol_counts, languages


def _module_node(
    repo_full_name: str,
    module: str,
    file_counts: Counter[str],
    symbol_counts: Counter[str],
    languages: Mapping[str, Counter[str]],
) -> GraphNode:
    return GraphNode(
        id=module_id(repo_full_name, module),
        label=NodeLabel.MODULE,
        properties={
            "name": module.rpartition("/")[2],
            "kind": "module",
            "path": module,
            "depth": module_depth(module),
            "file_count": file_counts[module],
            "definition_count": symbol_counts[module],
            "language": languages[module].most_common(1)[0][0],
        },
    )


def _synthetic_module_node(repo_full_name: str, module: str, kind: str) -> GraphNode:
    return GraphNode(
        id=module_id(repo_full_name, module),
        label=NodeLabel.MODULE,
        properties={
            "name": module_display_name(module),
            "kind": kind,
            "path": module,
            "depth": 1,
            "file_count": 0,
            "definition_count": 0,
        },
    )


def _top_level_summary(
    repo_full_name: str, links: RepositoryLinks, known: set[str],
) -> list[GraphEdge]:
    """Weighted dependencies between the outermost modules.

    Weight is the number of resolved imports collapsed into the edge.
    """
    internal: Counter[tuple[str, str]] = Counter()
    for file_import in links.imports:
        source, target = module_of(file_import.source_file), module_of(file_import.target_file)
        if source != target:  # a module depending on itself says nothing
            internal[(source, target)] += 1

    external: Counter[str] = Counter()
    for package_use in links.packages:
        external[module_of(package_use.source_file)] += 1

    edges = [
        GraphEdge(
            source=module_id(repo_full_name, source),
            target=module_id(repo_full_name, target),
            type=EdgeType.DEPENDS_ON,
            properties={"weight": weight},
        )
        for (source, target), weight in sorted(internal.items())
    ]
    edges.extend(
        GraphEdge(
            source=module_id(repo_full_name, module),
            target=module_id(repo_full_name, EXTERNAL_MODULE),
            type=EdgeType.USES_EXTERNAL,
            properties={"weight": weight},
        )
        for module, weight in sorted(external.items())
    )
    return [
        edge for edge in edges if edge.source in known and edge.target in known
    ]
