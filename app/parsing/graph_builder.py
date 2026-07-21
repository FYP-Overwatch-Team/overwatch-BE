"""Turn per-file AST extractions into a service-level node/edge graph.

Known eval-1 simplification (stated openly in the demo): one node per
top-level directory under the repo root. Root-level files group into a
"root" node. External package imports collapse into a single external
node per repo rather than one node per package.
"""

from collections import defaultdict
from dataclasses import asdict

from app.parsing.ast_service import FileParse, LANGUAGE_BY_EXT

EXTERNAL_NODE = "__external__"


def node_id(repo_full_name: str, group: str) -> str:
    # Stable, path-derived ids so Neo4j MERGE updates instead of duplicating.
    return f"{repo_full_name}:{group}"


def _group_of(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "root"


def _strip_ext(path: str) -> str:
    for ext in LANGUAGE_BY_EXT:
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def _resolve_import(importer_path: str, target: str, known_modules: set[str]) -> str | None:
    """Resolve an import string to a repo-relative file path (sans extension), or None if external."""
    if target.startswith("."):
        base = importer_path.split("/")[:-1]
        for part in target.split("/"):
            if part in (".", ""):
                continue
            elif part == "..":
                if base:
                    base.pop()
            else:
                base.append(part)
        candidate = "/".join(base)
        for suffix in ("", "/index"):
            if candidate + suffix in known_modules:
                return candidate + suffix
        return candidate if candidate in known_modules else None

    # python absolute module path (a.b.c) or bare name that matches an internal module
    dotted = target.replace(".", "/")
    for candidate in (dotted, f"{dotted}/__init__", target):
        if candidate in known_modules:
            return candidate
    return None


def build_graph(repo_full_name: str, parses: list[FileParse]) -> dict:
    """Returns {"nodes": [...], "edges": [...]} with deterministic ordering."""
    known_modules = {_strip_ext(p.path) for p in parses}

    groups: dict[str, dict] = {}
    for p in parses:
        group = _group_of(p.path)
        entry = groups.setdefault(group, {"files": [], "definitions": 0})
        entry["files"].append(p.path)
        entry["definitions"] += len(p.definitions)

    edge_weights: dict[tuple[str, str, str], int] = defaultdict(int)
    has_external = False
    for p in parses:
        source_group = _group_of(p.path)
        for imp in p.imports:
            resolved = _resolve_import(_strip_ext(p.path), imp, known_modules)
            if resolved is None:
                has_external = True
                edge_weights[(source_group, EXTERNAL_NODE, "EXTERNAL_DEPENDENCY")] += 1
                continue
            target_group = _group_of(resolved)
            if target_group != source_group:
                edge_weights[(source_group, target_group, "DEPENDS_ON")] += 1

    nodes = [
        {
            "id": node_id(repo_full_name, group),
            "name": group,
            "kind": "module",
            "file_count": len(info["files"]),
            "definition_count": info["definitions"],
        }
        for group, info in sorted(groups.items())
    ]
    if has_external:
        nodes.append({
            "id": node_id(repo_full_name, EXTERNAL_NODE),
            "name": "external dependencies",
            "kind": "external",
            "file_count": 0,
            "definition_count": 0,
        })

    edges = [
        {
            "source": node_id(repo_full_name, src),
            "target": node_id(repo_full_name, dst),
            "type": edge_type,
            "weight": weight,
        }
        for (src, dst, edge_type), weight in sorted(edge_weights.items())
    ]
    return {"nodes": nodes, "edges": edges}


def graph_snapshot(repo_full_name: str, parses: list[FileParse]) -> dict:
    """Full deterministic dump (graph + per-file extraction) for regression snapshots."""
    return {
        "graph": build_graph(repo_full_name, parses),
        "files": [asdict(p) for p in sorted(parses, key=lambda p: p.path)],
    }
