"""Facts plus links become a graph.

The last pure stage: given what was extracted and what it resolved to, produce
the complete set of nodes and edges for one commit. No database, no clock, no
randomness — the same inputs always produce the same snapshot, which is what
makes snapshot tests and delta writes possible.

Layers, from coarse to fine:

    Repository → Module → Module… → File → Symbol
                    ↘ Package, Route

Modules nest: every directory is one, so the containment chain runs all the
way from the repository down to a single definition. Packages and routes have
no directory, so they hang under the two synthetic modules. That unbroken
chain is what the interactive view walks when a node is expanded or collapsed.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from app.knowledge_graph.build.ids import (
    EXTERNAL_MODULE,
    ROUTES_MODULE,
    ecosystem_for_language,
    route_id,
    file_id,
    module_id,
    owning_module,
    package_id,
    repository_id,
    symbol_id,
)
from app.knowledge_graph.build.model import (
    EdgeType,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    NodeLabel,
)
from app.knowledge_graph.build.projection import build_module_layer
from app.knowledge_graph.facts import FileFacts
from app.knowledge_graph.limits import DEFAULT_LIMITS, Limits
from app.knowledge_graph.link import EdgeKind, RepositoryLinks, SymbolRef
from app.knowledge_graph.link.http import route_key

_EDGE_TYPE_BY_KIND = {
    EdgeKind.CALLS: EdgeType.CALLS,
    EdgeKind.RENDERS: EdgeType.RENDERS,
    EdgeKind.EXTENDS: EdgeType.EXTENDS,
    EdgeKind.IMPLEMENTS: EdgeType.IMPLEMENTS,
}


def build_snapshot(
    repo_full_name: str,
    version: str,
    facts_by_path: Mapping[str, FileFacts],
    links: RepositoryLinks,
    *,
    limits: Limits = DEFAULT_LIMITS,
    extra_stats: Mapping[str, Any] | None = None,
) -> GraphSnapshot:
    """Build the complete graph for one commit of one repository."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    total_symbols = sum(len(facts.definitions) for facts in facts_by_path.values())
    # Above the cap we keep the file and module layers and drop the symbol
    # layer, rather than failing or silently truncating. The reason is
    # recorded in stats and surfaces in the UI.
    include_symbols = total_symbols <= limits.max_symbols_per_repo

    nodes.append(
        GraphNode(
            id=repository_id(repo_full_name),
            label=NodeLabel.REPOSITORY,
            properties={"name": repo_full_name, "commit_sha": version},
        )
    )

    module_nodes, module_edges = build_module_layer(repo_full_name, facts_by_path, links)
    nodes.extend(module_nodes)
    edges.extend(module_edges)
    # Only the outermost modules hang off the repository; the rest are
    # reached through their parent module, which the projection wired up.
    edges.extend(
        GraphEdge(
            source=repository_id(repo_full_name),
            target=module.id,
            type=EdgeType.CONTAINS,
        )
        for module in module_nodes
        if module.properties.get("depth") == 1
    )

    symbol_ids = _add_files_and_symbols(
        repo_full_name, facts_by_path, nodes, edges, include_symbols=include_symbols,
    )
    _add_packages(repo_full_name, facts_by_path, links, nodes, edges)
    _add_imports(repo_full_name, links, edges)
    _add_http_layer(repo_full_name, links, symbol_ids, nodes, edges)
    if include_symbols:
        _add_symbol_edges(repo_full_name, links, symbol_ids, edges)

    return GraphSnapshot(
        repo_full_name=repo_full_name,
        version=version,
        nodes=tuple(nodes),
        edges=tuple(edges),
        stats={
            "files": len(facts_by_path),
            "symbols": total_symbols,
            "symbol_layer": "included" if include_symbols else "omitted_over_cap",
            "nodes_by_label": _counted(str(node.label) for node in nodes),
            "edges_by_type": _counted(str(edge.type) for edge in edges),
            "resolution": links.stats.as_dict(),
            **(extra_stats or {}),
        },
    )


def _counted(values) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _add_files_and_symbols(
    repo_full_name: str,
    facts_by_path: Mapping[str, FileFacts],
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    include_symbols: bool,
) -> dict[tuple[str, str], str]:
    """File nodes, their symbols, and the containment edges between them.

    Returns a lookup from (file, qualified name) to symbol id, so edges
    resolved by the linker can address the same nodes.
    """
    symbol_ids: dict[tuple[str, str], str] = {}

    for path in sorted(facts_by_path):
        facts = facts_by_path[path]
        this_file = file_id(repo_full_name, path)
        nodes.append(
            GraphNode(
                id=this_file,
                label=NodeLabel.FILE,
                properties={
                    "path": path,
                    "name": path.rpartition("/")[2],
                    "language": facts.language,
                    "loc": facts.loc,
                    "content_hash": facts.content_hash,
                    "definition_count": len(facts.definitions),
                    "has_syntax_errors": facts.has_syntax_errors,
                },
            )
        )
        # A file with no directory above it is held by the repository itself.
        directory = owning_module(path)
        edges.append(
            GraphEdge(
                source=(
                    module_id(repo_full_name, directory)
                    if directory is not None
                    else repository_id(repo_full_name)
                ),
                target=this_file,
                type=EdgeType.CONTAINS,
                origin_file=path,
            )
        )

        if not include_symbols:
            continue

        seen: Counter[str] = Counter()
        for definition in facts.definitions:
            seen[definition.qualified_name] += 1
            ordinal = seen[definition.qualified_name]
            identifier = symbol_id(repo_full_name, path, definition.qualified_name, ordinal)
            # Only the first definition of a name owns the plain id; later
            # ones are addressable but the linker cannot tell them apart.
            symbol_ids.setdefault((path, definition.qualified_name), identifier)

            nodes.append(
                GraphNode(
                    id=identifier,
                    label=NodeLabel.SYMBOL,
                    properties={
                        "name": definition.name,
                        "qualified_name": definition.qualified_name,
                        "kind": definition.kind,
                        "file_path": path,
                        "start_line": definition.start_line,
                        "end_line": definition.end_line,
                        "signature": definition.signature,
                        "docstring": definition.docstring,
                        "exported": definition.exported,
                    },
                )
            )

            parent_id = symbol_ids.get((path, definition.parent)) if definition.parent else None
            edges.append(
                GraphEdge(
                    source=parent_id or this_file,
                    target=identifier,
                    type=EdgeType.HAS_MEMBER if parent_id else EdgeType.DEFINES,
                    origin_file=path,
                )
            )

    return symbol_ids


def _add_packages(
    repo_full_name: str,
    facts_by_path: Mapping[str, FileFacts],
    links: RepositoryLinks,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> None:
    """One node per third-party package, with the files that import it."""
    package_names: dict[str, str] = {}  # id -> name
    ecosystems: dict[str, str] = {}

    for use in links.packages:
        language = facts_by_path[use.source_file].language if use.source_file in facts_by_path else ""
        ecosystem = ecosystem_for_language(language)
        identifier = package_id(repo_full_name, ecosystem, use.package)
        package_names[identifier] = use.package
        ecosystems[identifier] = ecosystem
        edges.append(
            GraphEdge(
                source=file_id(repo_full_name, use.source_file),
                target=identifier,
                type=EdgeType.USES_PACKAGE,
                properties={"names": list(use.names)},
                origin_file=use.source_file,
            )
        )

    nodes.extend(
        GraphNode(
            id=identifier,
            label=NodeLabel.PACKAGE,
            properties={
                "name": package_names[identifier],
                "ecosystem": ecosystems[identifier],
            },
        )
        for identifier in sorted(package_names)
    )
    edges.extend(
        GraphEdge(
            source=module_id(repo_full_name, EXTERNAL_MODULE),
            target=identifier,
            type=EdgeType.CONTAINS,
        )
        for identifier in sorted(package_names)
    )


def _add_imports(repo_full_name: str, links: RepositoryLinks, edges: list[GraphEdge]) -> None:
    """File-to-file imports, merged per pair with the names they carry."""
    merged: dict[tuple[str, str], tuple[set[str], list[int]]] = defaultdict(lambda: (set(), []))
    for file_import in links.imports:
        names, lines = merged[(file_import.source_file, file_import.target_file)]
        names.update(file_import.names)
        lines.append(file_import.line)

    edges.extend(
        GraphEdge(
            source=file_id(repo_full_name, source),
            target=file_id(repo_full_name, target),
            type=EdgeType.IMPORTS,
            properties={
                "names": sorted(names),
                "count": len(lines),
                "lines": sorted(lines),
            },
            origin_file=source,
        )
        for (source, target), (names, lines) in sorted(merged.items())
    )


def _add_http_layer(
    repo_full_name: str,
    links: RepositoryLinks,
    symbol_ids: Mapping[tuple[str, str], str],
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> None:
    """Route nodes, their handlers, and the code that calls them.

    This is what connects a frontend to its backend: the caller and the
    handler may be in different languages and different directories, but they
    meet at the route.
    """
    routes: dict[str, GraphNode] = {}

    for route in links.routes:
        key = route_key(route.method, route.path)
        identifier = route_id(repo_full_name, key.method, key.path)
        routes.setdefault(
            identifier,
            GraphNode(
                id=identifier,
                label=NodeLabel.ROUTE,
                properties={
                    "name": str(key),
                    "method": key.method,
                    "path": key.path,
                    "declared_path": route.path,
                    "framework": route.framework,
                    "file_path": route.file,
                    "line": route.line,
                },
            ),
        )

        handler_id = symbol_ids.get((route.file, route.handler)) if route.handler else None
        edges.append(
            GraphEdge(
                source=handler_id or file_id(repo_full_name, route.file),
                target=identifier,
                type=EdgeType.HANDLES,
                properties={"method": key.method, "path": key.path},
                origin_file=route.file,
            )
        )

    for request in links.requests:
        identifier = route_id(repo_full_name, request.method, request.path)
        if identifier not in routes:
            continue  # a route we did not index; nothing to point at
        source_id = (
            symbol_ids.get((request.source_file, request.source_symbol))
            if request.source_symbol
            else None
        ) or file_id(repo_full_name, request.source_file)
        edges.append(
            GraphEdge(
                source=source_id,
                target=identifier,
                type=EdgeType.REQUESTS,
                properties={"method": request.method, "line": request.line},
                origin_file=request.source_file,
            )
        )

    nodes.extend(routes[identifier] for identifier in sorted(routes))
    edges.extend(
        GraphEdge(
            source=module_id(repo_full_name, ROUTES_MODULE),
            target=identifier,
            type=EdgeType.CONTAINS,
        )
        for identifier in sorted(routes)
    )


def _add_symbol_edges(
    repo_full_name: str,
    links: RepositoryLinks,
    symbol_ids: Mapping[tuple[str, str], str],
    edges: list[GraphEdge],
) -> None:
    """Calls, renders and inheritance between symbols."""
    for edge in links.edges:
        target_id = _symbol_id_for(repo_full_name, edge.target, symbol_ids)
        if target_id is None:
            continue  # the definition vanished between linking and building

        source_id = (
            symbol_ids.get((edge.source_file, edge.source_symbol))
            if edge.source_symbol
            else file_id(repo_full_name, edge.source_file)
        )
        if source_id is None:
            continue

        edges.append(
            GraphEdge(
                source=source_id,
                target=target_id,
                type=_EDGE_TYPE_BY_KIND[edge.kind],
                properties={"count": edge.count, "lines": list(edge.lines)},
                origin_file=edge.source_file,
            )
        )


def _symbol_id_for(
    repo_full_name: str, reference: SymbolRef, symbol_ids: Mapping[tuple[str, str], str],
) -> str | None:
    return symbol_ids.get((reference.file, reference.qualified_name))
