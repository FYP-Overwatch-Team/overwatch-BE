"""The module-level view the dashboard renders.

One node per top-level directory, plus a single node standing for everything
outside the repository — the eval-1 simplification, stated openly in the UI.

It is **materialised** rather than computed per request: the dashboard's read
stays a single cheap query, and the richer file and symbol layers sit beside
it for drill-down. Both are derived from the same facts, so they cannot
disagree.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping

from app.knowledge_graph.build.ids import (
    EXTERNAL_MODULE,
    module_display_name,
    module_id,
    module_of,
)
from app.knowledge_graph.build.model import EdgeType, GraphEdge, GraphNode, NodeLabel
from app.knowledge_graph.facts import FileFacts
from app.knowledge_graph.link import RepositoryLinks


def build_module_layer(
    repo_full_name: str,
    facts_by_path: Mapping[str, FileFacts],
    links: RepositoryLinks,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Module nodes and the weighted dependencies between them."""
    file_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    languages: dict[str, Counter[str]] = defaultdict(Counter)

    for path, facts in facts_by_path.items():
        module = module_of(path)
        file_counts[module] += 1
        symbol_counts[module] += len(facts.definitions)
        languages[module][facts.language] += 1

    nodes = [
        GraphNode(
            id=module_id(repo_full_name, module),
            label=NodeLabel.MODULE,
            properties={
                "name": module,
                "kind": "module",
                "path": module,
                "file_count": file_counts[module],
                "definition_count": symbol_counts[module],
                "language": languages[module].most_common(1)[0][0],
            },
        )
        for module in sorted(file_counts)
    ]

    # Weight is the number of resolved imports collapsed into the edge, which
    # is what the dashboard renders as "×N".
    internal_weights: Counter[tuple[str, str]] = Counter()
    for file_import in links.imports:
        source, target = module_of(file_import.source_file), module_of(file_import.target_file)
        if source != target:  # a module depending on itself says nothing
            internal_weights[(source, target)] += 1

    external_weights: Counter[str] = Counter()
    for package_use in links.packages:
        external_weights[module_of(package_use.source_file)] += 1

    edges = [
        GraphEdge(
            source=module_id(repo_full_name, source),
            target=module_id(repo_full_name, target),
            type=EdgeType.DEPENDS_ON,
            properties={"weight": weight},
        )
        for (source, target), weight in sorted(internal_weights.items())
    ]

    if external_weights:
        nodes.append(
            GraphNode(
                id=module_id(repo_full_name, EXTERNAL_MODULE),
                label=NodeLabel.MODULE,
                properties={
                    "name": module_display_name(EXTERNAL_MODULE),
                    "kind": "external",
                    "path": EXTERNAL_MODULE,
                    "file_count": 0,
                    "definition_count": 0,
                },
            )
        )
        edges.extend(
            GraphEdge(
                source=module_id(repo_full_name, module),
                target=module_id(repo_full_name, EXTERNAL_MODULE),
                type=EdgeType.USES_EXTERNAL,
                properties={"weight": weight},
            )
            for module, weight in sorted(external_weights.items())
        )

    return nodes, edges
