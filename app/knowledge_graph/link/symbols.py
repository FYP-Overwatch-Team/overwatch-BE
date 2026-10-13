"""Binding references to the definitions they actually point at.

Each file gets a small table of what its names mean: its own definitions, plus
what its imports bring in. A reference is then resolved by looking the name up
in that table — the same thing a reader does.

**What is deliberately not resolved.** A call on a value whose type we do not
track (`user.save()`), dynamic dispatch, a callback passed elsewhere, a
computed specifier. These are counted as unresolved, never matched by name
alone: an edge that might be wrong is worse than a missing edge in a graph
whose whole promise is that it is derived from the code.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from app.knowledge_graph.facts import KIND_CLASS, FileFacts
from app.knowledge_graph.link.http import RouteMatcher, route_key
from app.knowledge_graph.link.imports import TargetKind, resolve_import
from app.knowledge_graph.link.module_index import ModuleIndex
from app.knowledge_graph.link.results import (
    EdgeKind,
    FileImport,
    PackageUse,
    RequestLink,
    RouteLink,
    RepositoryLinks,
    ResolutionStats,
    SymbolEdge,
    SymbolRef,
)
from app.knowledge_graph.project_config import ProjectConfig

#: Receivers that mean "the class this code is in".
SELF_RECEIVERS = frozenset({"self", "this"})


@dataclass(frozen=True, slots=True)
class _Binding:
    """What a name in a file refers to."""

    file: str
    #: Definition name in the target file, or None for a whole-module binding
    #: (a namespace import, or `import services.users`).
    symbol: str | None = None


class _FileTable:
    """The names one file can resolve, and what they point at."""

    def __init__(self, facts: FileFacts):
        self.path = facts.path
        # Local definitions, by simple name and by qualified name.
        self.by_name: dict[str, str] = {}
        self.by_qualified_name: dict[str, str] = {}
        for definition in facts.definitions:
            self.by_qualified_name[definition.qualified_name] = definition.qualified_name
            # An inner definition never shadows a top-level one of the same name.
            if definition.name not in self.by_name or definition.parent is None:
                self.by_name[definition.name] = definition.qualified_name
        self.exported = [d for d in facts.definitions if d.exported and d.parent is None]
        self.imports: dict[str, _Binding] = {}
        self.namespaces: dict[str, _Binding] = {}
        #: Local name -> package it came from. Not edges between our symbols,
        #: but known destinations, so they are reported separately from
        #: references we genuinely cannot resolve.
        self.packages: dict[str, str] = {}

    def package_for(self, callee: str) -> str | None:
        """The package a reference comes from, if its root name was imported."""
        return self.packages.get(callee) or self.packages.get(callee.partition(".")[0])


def link_repository(
    facts_by_path: Mapping[str, FileFacts], config: ProjectConfig,
) -> RepositoryLinks:
    """Resolve every reference in a repository's facts."""
    index = ModuleIndex(facts_by_path.keys())
    tables = {path: _FileTable(facts) for path, facts in facts_by_path.items()}

    imports: list[FileImport] = []
    package_uses: dict[tuple[str, str], set[str]] = defaultdict(set)
    counters = {
        "imports_total": 0, "imports_to_files": 0,
        "imports_to_packages": 0, "imports_unresolved": 0,
    }

    for path, facts in facts_by_path.items():
        _bind_imports(facts, tables[path], index, config, imports, package_uses, counters)

    edges, reference_counts = _resolve_references(facts_by_path, tables)
    routes, requests, http_counts = _resolve_http(facts_by_path)

    stats = ResolutionStats(**counters, **reference_counts, **http_counts)

    return RepositoryLinks(
        files=tuple(sorted(facts_by_path)),
        imports=tuple(sorted(imports, key=lambda i: (i.source_file, i.target_file, i.line))),
        packages=tuple(
            PackageUse(source_file=source, package=package, names=tuple(sorted(names)))
            for (source, package), names in sorted(package_uses.items())
        ),
        edges=tuple(edges),
        routes=routes,
        requests=requests,
        stats=stats,
    )


def _resolve_http(
    facts_by_path: Mapping[str, FileFacts],
) -> tuple[tuple[RouteLink, ...], tuple[RequestLink, ...], dict[str, int]]:
    """Collect routes, then match outbound requests against them.

    Matching is exact on method and normalised path. A request that matches
    nothing is counted, not attached to the closest-looking route: a wrong
    edge between a frontend and a backend would be worse than no edge.
    """
    routes = tuple(
        RouteLink(
            file=path,
            method=route.method.upper(),
            path=route.path,
            framework=route.framework,
            line=route.line,
            handler=route.handler,
        )
        for path in sorted(facts_by_path)
        for route in facts_by_path[path].routes
    )
    matcher = RouteMatcher([(route.method, route.path) for route in routes])

    requests: list[RequestLink] = []
    total = 0
    for path in sorted(facts_by_path):
        for call in facts_by_path[path].http_calls:
            total += 1
            matched = matcher.match(call.method, call.path)
            if matched is None:
                continue
            requests.append(
                RequestLink(
                    source_file=path,
                    source_symbol=call.caller,
                    # The route's own shape, so the edge points at one node
                    # however the call spelled its parameters.
                    method=matched.method,
                    path=matched.path,
                    line=call.line,
                )
            )

    counts = {
        "routes_total": len(routes),
        "http_calls_total": total,
        "http_calls_matched": len(requests),
    }
    return routes, tuple(requests), counts


def _bind_imports(
    facts: FileFacts,
    table: _FileTable,
    index: ModuleIndex,
    config: ProjectConfig,
    imports: list[FileImport],
    package_uses: dict[tuple[str, str], set[str]],
    counters: dict[str, int],
) -> None:
    for reference in facts.imports:
        counters["imports_total"] += 1
        target = resolve_import(facts.path, reference.specifier, index, config)

        if target.kind is TargetKind.PACKAGE:
            counters["imports_to_packages"] += 1
            package_uses[(facts.path, target.value)].update(reference.names)
            _bind_package_names(table, reference, target.value)
            continue

        if target.kind is TargetKind.UNRESOLVED:
            counters["imports_unresolved"] += 1
            continue

        counters["imports_to_files"] += 1
        if target.value != facts.path:  # a file importing itself adds nothing
            imports.append(
                FileImport(
                    source_file=facts.path,
                    target_file=target.value,
                    names=reference.names,
                    line=reference.line,
                )
            )
        _bind_file_names(table, reference, target.value)


def _bind_file_names(table: _FileTable, reference, target_file: str) -> None:
    """Record what each imported name means inside this file."""
    if reference.is_wildcard and reference.alias:
        table.namespaces[reference.alias] = _Binding(target_file)
        return

    for name in reference.names:
        if name == "default":
            # `import express from "express"`: the local alias stands for the
            # module, not for a definition we can name.
            if reference.alias:
                table.namespaces[reference.alias] = _Binding(target_file)
        else:
            table.imports[name] = _Binding(target_file, name)

    if not reference.names:
        # Python's `import services.users` binds the dotted path itself.
        table.namespaces[reference.alias or reference.specifier] = _Binding(target_file)


def _bind_package_names(table: _FileTable, reference, package: str) -> None:
    """Names from a package point outside the repository, not at a local symbol."""
    for name in reference.names:
        table.imports.pop(name, None)
        if name != "default":
            table.packages[name] = package
    if reference.alias:
        table.namespaces.pop(reference.alias, None)
        table.packages[reference.alias] = package
    if not reference.names and not reference.alias:
        table.packages[reference.specifier] = package


def _resolve_references(
    facts_by_path: Mapping[str, FileFacts], tables: Mapping[str, _FileTable],
) -> tuple[list[SymbolEdge], dict[str, int]]:
    """Resolve calls, renders and inheritance into edges, merging duplicates."""
    counts = dict.fromkeys(
        ["calls_total", "calls_resolved", "calls_to_packages",
         "renders_total", "renders_resolved", "renders_to_packages",
         "inheritance_total", "inheritance_resolved", "inheritance_to_packages"], 0,
    )
    merged: dict[tuple[str, str | None, str, str, EdgeKind], list[int]] = defaultdict(list)

    for path, facts in facts_by_path.items():
        table = tables[path]

        for call in facts.calls:
            counts["calls_total"] += 1
            target = _resolve_name(call.callee, call.receiver, call.enclosing, table, tables)
            if target is not None:
                counts["calls_resolved"] += 1
                merged[(path, call.enclosing, target.file, target.qualified_name, EdgeKind.CALLS)].append(call.line)
            elif table.package_for(call.callee):
                counts["calls_to_packages"] += 1

        for render in facts.renders:
            counts["renders_total"] += 1
            target = _resolve_name(render.component, None, render.enclosing, table, tables)
            if target is not None:
                counts["renders_resolved"] += 1
                merged[(path, render.enclosing, target.file, target.qualified_name, EdgeKind.RENDERS)].append(render.line)
            elif table.package_for(render.component):
                counts["renders_to_packages"] += 1

        for inheritance in facts.inheritance:
            counts["inheritance_total"] += 1
            target = _resolve_name(inheritance.parent_name, None, inheritance.child, table, tables)
            if target is not None:
                counts["inheritance_resolved"] += 1
                kind = EdgeKind.IMPLEMENTS if inheritance.kind == "implements" else EdgeKind.EXTENDS
                merged[(path, inheritance.child, target.file, target.qualified_name, kind)].append(inheritance.line)
            elif table.package_for(inheritance.parent_name):
                counts["inheritance_to_packages"] += 1

    edges = [
        SymbolEdge(
            source_file=source_file,
            source_symbol=source_symbol,
            target=SymbolRef(target_file, target_symbol),
            kind=kind,
            count=len(lines),
            lines=tuple(sorted(lines)),
        )
        for (source_file, source_symbol, target_file, target_symbol, kind), lines in merged.items()
    ]
    edges.sort(key=lambda e: (e.source_file, e.source_symbol or "", e.kind, e.target.file, e.target.qualified_name))
    return edges, counts


def _resolve_name(
    callee: str,
    receiver: str | None,
    enclosing: str | None,
    table: _FileTable,
    tables: Mapping[str, _FileTable],
) -> SymbolRef | None:
    """Resolve one reference, or None when it cannot be known from the code."""
    if "." not in callee:
        return _resolve_simple(callee, table, tables)

    head, _, tail = callee.partition(".")
    member = callee.rsplit(".", 1)[1]

    if head in SELF_RECEIVERS:
        return _resolve_member_of_enclosing_class(member, enclosing, table)

    namespace = table.namespaces.get(head)
    if namespace is not None:
        return _resolve_in_file(member, namespace.file, tables)

    # `services.users.get_user()` — a dotted module binding.
    module_path = callee.rsplit(".", 1)[0]
    namespace = table.namespaces.get(module_path)
    if namespace is not None:
        return _resolve_in_file(member, namespace.file, tables)

    # Anything else is a call on a value we do not have a type for.
    return None


def _resolve_simple(
    name: str, table: _FileTable, tables: Mapping[str, _FileTable],
) -> SymbolRef | None:
    local = table.by_name.get(name)
    if local is not None:
        return SymbolRef(table.path, local)

    binding = table.imports.get(name)
    if binding is None or binding.symbol is None:
        return None
    return _resolve_in_file(binding.symbol, binding.file, tables)


def _resolve_in_file(
    name: str, file: str, tables: Mapping[str, _FileTable],
) -> SymbolRef | None:
    target_table = tables.get(file)
    if target_table is None:
        return None
    qualified = target_table.by_name.get(name)
    if qualified is None:
        return None
    return SymbolRef(file, qualified)


def _resolve_member_of_enclosing_class(
    member: str, enclosing: str | None, table: _FileTable,
) -> SymbolRef | None:
    """`self.helper()` means the method `helper` on the class we are inside."""
    if not enclosing:
        return None
    owner = enclosing.rpartition(".")[0] or enclosing
    qualified = f"{owner}.{member}"
    if qualified in table.by_qualified_name:
        return SymbolRef(table.path, qualified)
    return None


__all__ = ["link_repository"]
