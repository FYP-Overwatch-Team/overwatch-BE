"""Persisting the knowledge graph (the `KnowledgeGraphStore` port).

Neo4j for production, an in-memory implementation for tests; both satisfy the
same suite.

**Injection safety.** Cypher cannot parameterise a label or a relationship
type, so those are concatenated into the query string. Every one comes from
`NodeLabel` / `EdgeType` — closed enums defined in our own code — and is
checked before use. Nothing derived from repository content is ever
concatenated; ids, properties and repository names are always parameters.

**Tenant safety.** Every statement is scoped to one repository, including the
deletes, so a write for one repository can neither read nor remove another's
nodes.

**Write shape.** Rows are chunked and each chunk runs in one transaction. A
first index writes everything; later syncs write only a delta, because
measurement showed the write — not parsing — is the slow stage.
"""

from collections.abc import Sequence
from typing import Any

import structlog

from app.db.neo4j_client import get_graph_store
from app.knowledge_graph.build.ids import EXTERNAL_MODULE
from app.knowledge_graph.build.model import (
    EdgeType,
    GraphDelta,
    GraphEdge,
    GraphNode,
    NodeLabel,
)
from app.knowledge_graph.view.model import (
    CONTAINMENT_EDGE_TYPES,
    RollupEdge,
    ViewNode,
)

logger = structlog.get_logger("app.knowledge_graph.store")

#: Shared label so an edge can find its endpoints through one index, whatever
#: kind of node they are.
BASE_LABEL = "GraphNode"
#: Label written by the first version of the graph. Superseded, and removed
#: once at boot so an upgraded deployment does not keep serving stale nodes.
LEGACY_LABEL = "Service"
#: Rows per statement. Large enough to amortise round trips, small enough that
#: one transaction stays modest.
WRITE_CHUNK = 1_000

#: Read caps. Every query is bounded so one request cannot pull a whole
#: repository into memory, whatever the caller asks for.
CHILDREN_LIMIT = 500
#: Containers one view request may open, and rolled-up rows it may pull back.
VIEW_PARENTS_LIMIT = 64
VIEW_EDGE_LIMIT = 20_000
NEIGHBOUR_LIMIT = 500
SEARCH_LIMIT = 50
#: Traversal depth is concatenated into the query, so it is allow-listed.
ALLOWED_DEPTHS = (1, 2, 3)
ALLOWED_DIRECTIONS = ("in", "out", "both")


def validate_depth(depth: int) -> int:
    if isinstance(depth, bool) or not isinstance(depth, int) or depth not in ALLOWED_DEPTHS:
        raise ValueError(f"depth must be one of {ALLOWED_DEPTHS}, got {depth!r}")
    return depth


def _traversal_pattern(direction: str, depth: int) -> str:
    """The arrow part of a variable-length match, built from checked values."""
    if direction not in ALLOWED_DIRECTIONS:
        raise ValueError(f"direction must be one of {ALLOWED_DIRECTIONS}, got {direction!r}")
    hops = f"[*1..{depth}]"
    if direction == "out":
        return f"-{hops}->"
    if direction == "in":
        return f"<-{hops}-"
    return f"-{hops}-"


def _containment_pattern() -> str:
    """`CONTAINS|DEFINES|HAS_MEMBER`, built from the enum, for a match pattern.

    Relationship types cannot be parameterised, so this is concatenated into
    the query — which is safe precisely because every part of it comes from
    `EdgeType` and is checked by `_safe_type`.
    """
    return "|".join(_safe_type(edge_type) for edge_type in CONTAINMENT_EDGE_TYPES)


def _validated_edge_types(edge_types: Sequence[str] | None) -> list[str] | None:
    """Filter a caller's edge-type list down to types we actually define."""
    if not edge_types:
        return None
    known = {str(edge_type) for edge_type in EdgeType}
    unknown = [value for value in edge_types if value not in known]
    if unknown:
        raise ValueError(f"unknown edge types: {sorted(unknown)}")
    return list(edge_types)

#: Edge types the dashboard's module view is built from, mapped to the wire
#: names the frontend already understands.
_MODULE_EDGE_WIRE_NAMES = {
    EdgeType.DEPENDS_ON: "DEPENDS_ON",
    EdgeType.USES_EXTERNAL: "EXTERNAL_DEPENDENCY",
}

SCHEMA_STATEMENTS = (
    f"CREATE CONSTRAINT graph_node_id IF NOT EXISTS "
    f"FOR (n:{BASE_LABEL}) REQUIRE n.id IS UNIQUE",
    f"CREATE INDEX graph_node_repo IF NOT EXISTS FOR (n:{BASE_LABEL}) ON (n.repo)",
    f"CREATE INDEX graph_symbol_name IF NOT EXISTS "
    f"FOR (n:{NodeLabel.SYMBOL}) ON (n.name)",
)


def _safe_label(label: NodeLabel) -> str:
    if not isinstance(label, NodeLabel):
        raise ValueError(f"unknown node label: {label!r}")
    return label.value


def _safe_type(edge_type: EdgeType) -> str:
    if not isinstance(edge_type, EdgeType):
        raise ValueError(f"unknown edge type: {edge_type!r}")
    return edge_type.value


def _chunks(rows: Sequence[Any]) -> list[Sequence[Any]]:
    return [rows[start:start + WRITE_CHUNK] for start in range(0, len(rows), WRITE_CHUNK)]


#: Folds a Symbol endpoint into its File unless that file is open. Written
#: once and formatted per endpoint so the two ends cannot drift apart; every
#: value inside it is a query parameter.
_FOLD_TO_FILE = (
    "CASE WHEN $symbol IN labels({node}) AND {node}.file_path IS NOT NULL "
    "AND NOT {node}.file_path IN $expanded_files "
    "THEN {folded} ELSE {plain} END"
)


def _fold_fragment(node: str, field: str) -> str:
    folded = f"$prefix + {node}.file_path" if field == "id" else "$file"
    plain = f"{node}.id" if field == "id" else f"[label IN labels({node}) WHERE label <> $base][0]"
    return _FOLD_TO_FILE.format(node=node, folded=folded, plain=plain)


def _view_node(row: dict) -> ViewNode | None:
    """One row of a children query as a `ViewNode`, or None if unrecognised."""
    try:
        label = NodeLabel(row["label"])
    except ValueError:
        return None  # a label this version does not know: leave it out
    return ViewNode(
        id=row["id"],
        name=row.get("name") or row.get("path") or row["id"],
        label=label,
        kind=row.get("kind"),
        path=row.get("path"),
        depth=row.get("depth") or 0,
        file_count=row.get("file_count") or 0,
        definition_count=row.get("definition_count") or 0,
        child_count=row.get("child_count") or 0,
        language=row.get("language"),
    )


def _group_children(rows: Sequence[dict]) -> dict[str, list[ViewNode]]:
    grouped: dict[str, list[ViewNode]] = {}
    for row in rows:
        node = _view_node(row)
        if node is not None:
            grouped.setdefault(row["parent"], []).append(node)
    return grouped


def _rollup_edges_from_rows(rows: Sequence[dict]) -> list[RollupEdge]:
    edges: list[RollupEdge] = []
    for row in rows:
        try:
            edges.append(
                RollupEdge(
                    source=row["source"],
                    target=row["target"],
                    type=EdgeType(row["type"]),
                    source_label=NodeLabel(row["source_label"]),
                    target_label=NodeLabel(row["target_label"]),
                    weight=int(row.get("weight") or 1),
                )
            )
        except (ValueError, TypeError):
            continue  # a type or label this version does not know
    return edges


class Neo4jKnowledgeGraphStore:
    """The knowledge graph as stored in Neo4j."""

    def __init__(self, store=None):
        self._store = store or get_graph_store()

    async def ensure_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            await self._store.run(statement)

    async def apply(self, delta: GraphDelta) -> None:
        """Write a delta: upserts first, then deletions."""
        if delta.is_empty:
            return

        repo, version = delta.repo_full_name, delta.version
        await self._upsert_nodes(repo, version, delta.upserted_nodes)
        await self._upsert_edges(repo, version, delta.upserted_edges)
        await self._delete_edges(repo, delta.deleted_edge_keys)
        await self._delete_nodes(repo, delta.deleted_node_ids)

        logger.info("graph_delta_applied", repo=repo, version=version, **delta.summary())

    async def delete_repository(self, repo_full_name: str) -> None:
        """Remove a repository's graph, e.g. when it is disconnected."""
        await self._store.run(
            f"MATCH (n:{BASE_LABEL} {{repo: $repo}}) DETACH DELETE n", repo=repo_full_name,
        )

    async def module_view(self, repo_full_name: str) -> dict:
        """The module-level payload the dashboard renders, in its existing shape."""
        nodes = await self._store.run(
            f"""
            MATCH (n:{NodeLabel.MODULE} {{repo: $repo}})
            WHERE coalesce(n.depth, 1) = 1
            RETURN n.id AS id, n.name AS name, n.kind AS kind,
                   n.file_count AS file_count, n.definition_count AS definition_count
            ORDER BY id
            """,
            repo=repo_full_name,
        )
        edges = await self._store.run(
            f"""
            MATCH (a:{NodeLabel.MODULE} {{repo: $repo}})-[e]->(b:{NodeLabel.MODULE})
            WHERE type(e) IN $types
            RETURN a.id AS source, b.id AS target, type(e) AS type, e.weight AS weight
            ORDER BY source, target
            """,
            repo=repo_full_name,
            types=[str(edge_type) for edge_type in _MODULE_EDGE_WIRE_NAMES],
        )
        return {
            "nodes": nodes,
            "edges": [
                {**edge, "type": _MODULE_EDGE_WIRE_NAMES.get(EdgeType(edge["type"]), edge["type"])}
                for edge in edges
            ],
        }

    async def node_ids(self, repo_full_name: str) -> set[str]:
        rows = await self._store.run(
            f"MATCH (n:{BASE_LABEL} {{repo: $repo}}) RETURN n.id AS id", repo=repo_full_name,
        )
        return {row["id"] for row in rows}

    async def purge_legacy_nodes(self, batch: int = 5_000) -> int:
        """Delete nodes left by the previous graph, in bounded batches.

        Runs once at boot. Batched so an upgrade on a large database cannot
        build one enormous transaction.
        """
        total = 0
        while True:
            rows = await self._store.run(
                f"MATCH (n:{LEGACY_LABEL}) WITH n LIMIT $batch "
                f"DETACH DELETE n RETURN count(*) AS deleted",
                batch=batch,
            )
            deleted = rows[0]["deleted"] if rows else 0
            total += deleted
            if deleted < batch:
                return total

    async def node_detail(self, repo_full_name: str, node_id: str) -> dict | None:
        """A node plus the nodes it contains (module → files → symbols).

        Both the id and the repository are matched, so an id belonging to
        another repository simply finds nothing.
        """
        rows = await self._store.run(
            f"""
            MATCH (n:{BASE_LABEL} {{id: $id, repo: $repo}})
            OPTIONAL MATCH (n)-[r]->(child:{BASE_LABEL})
            WHERE type(r) IN $containment
            RETURN n AS node, labels(n) AS labels,
                   collect(DISTINCT {{id: child.id, name: child.name,
                                      kind: child.kind, path: child.path}})[..$limit] AS children
            """,
            id=node_id,
            repo=repo_full_name,
            containment=[str(EdgeType.CONTAINS), str(EdgeType.DEFINES), str(EdgeType.HAS_MEMBER)],
            limit=CHILDREN_LIMIT,
        )
        if not rows:
            return None
        row = rows[0]
        node = dict(row["node"])
        return {
            "id": node.pop("id"),
            "labels": [label for label in row["labels"] if label != BASE_LABEL],
            "properties": node,
            "children": [child for child in row["children"] if child.get("id")],
        }

    async def neighbours(
        self,
        repo_full_name: str,
        node_id: str,
        *,
        direction: str = "both",
        edge_types: Sequence[str] | None = None,
        depth: int = 1,
        limit: int = 200,
    ) -> dict:
        """The subgraph around a node, bounded by depth and result count."""
        pattern = _traversal_pattern(direction, validate_depth(depth))
        types = _validated_edge_types(edge_types)

        rows = await self._store.run(
            f"""
            MATCH path = (n:{BASE_LABEL} {{id: $id, repo: $repo}}){pattern}(m:{BASE_LABEL})
            WHERE m.repo = $repo
              AND ($types IS NULL OR all(r IN relationships(path) WHERE type(r) IN $types))
            WITH DISTINCT m, relationships(path) AS rels
            LIMIT $limit
            RETURN m.id AS id, m.name AS name, m.kind AS kind, labels(m) AS labels,
                   [r IN rels | {{source: startNode(r).id, target: endNode(r).id,
                                  type: type(r), count: r.count, weight: r.weight}}] AS edges
            """,
            id=node_id, repo=repo_full_name, types=types, limit=min(limit, NEIGHBOUR_LIMIT),
        )

        nodes: dict[str, dict] = {}
        edges: dict[tuple, dict] = {}
        for row in rows:
            nodes[row["id"]] = {
                "id": row["id"],
                "name": row["name"],
                "kind": row["kind"],
                "labels": [label for label in row["labels"] if label != BASE_LABEL],
            }
            for edge in row["edges"]:
                edges[(edge["source"], edge["target"], edge["type"])] = edge
        return {
            "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "edges": sorted(edges.values(), key=lambda e: (e["source"], e["target"], e["type"])),
        }

    async def search(self, repo_full_name: str, term: str, *, limit: int = 20) -> list[dict]:
        rows = await self._store.run(
            f"""
            MATCH (n:{BASE_LABEL} {{repo: $repo}})
            WHERE (n:{NodeLabel.SYMBOL} OR n:{NodeLabel.FILE})
              AND toLower(coalesce(n.name, n.path, '')) CONTAINS toLower($term)
            RETURN n.id AS id, n.name AS name, n.kind AS kind, n.path AS path,
                   n.file_path AS file_path, n.signature AS signature, labels(n) AS labels
            ORDER BY size(coalesce(n.name, n.path, '')), id
            LIMIT $limit
            """,
            repo=repo_full_name, term=term, limit=min(limit, SEARCH_LIMIT),
        )
        return [
            {**row, "labels": [label for label in row["labels"] if label != BASE_LABEL]}
            for row in rows
        ]

    async def counts(self, repo_full_name: str) -> dict:
        node_rows = await self._store.run(
            f"""
            MATCH (n:{BASE_LABEL} {{repo: $repo}})
            UNWIND labels(n) AS label
            WITH label, count(*) AS total WHERE label <> $base
            RETURN label, total ORDER BY label
            """,
            repo=repo_full_name, base=BASE_LABEL,
        )
        edge_rows = await self._store.run(
            f"""
            MATCH (:{BASE_LABEL} {{repo: $repo}})-[r]->(:{BASE_LABEL})
            RETURN type(r) AS type, count(*) AS total ORDER BY type
            """,
            repo=repo_full_name,
        )
        return {
            "nodes_by_label": {row["label"]: row["total"] for row in node_rows},
            "edges_by_type": {row["type"]: row["total"] for row in edge_rows},
        }


    # -- interactive view --------------------------------------------------

    async def view_children(
        self, repo_full_name: str, parent_ids: Sequence[str],
    ) -> dict[str, list[ViewNode]]:
        """What each of these nodes contains, one level down.

        The result is capped *per parent*, so one enormous directory cannot
        starve the others of the request's budget.
        """
        parents = list(dict.fromkeys(parent_ids))[:VIEW_PARENTS_LIMIT]
        if not parents:
            return {}

        containment = _containment_pattern()
        rows = await self._store.run(
            f"""
            MATCH (p:{BASE_LABEL} {{repo: $repo}})-[:{containment}]->(c:{BASE_LABEL})
            WHERE p.id IN $parents AND c.repo = $repo
            WITH p.id AS parent, c ORDER BY c.id
            WITH parent, collect(c)[..$per_parent] AS children
            UNWIND children AS c
            RETURN parent,
                   c.id AS id, c.name AS name, c.kind AS kind, c.path AS path,
                   [label IN labels(c) WHERE label <> $base][0] AS label,
                   coalesce(c.depth, 0) AS depth,
                   coalesce(c.file_count, 0) AS file_count,
                   coalesce(c.definition_count, 0) AS definition_count,
                   c.language AS language,
                   size([(c)-[:{containment}]->(x:{BASE_LABEL}) | x]) AS child_count
            ORDER BY parent, id
            """,
            repo=repo_full_name,
            parents=parents,
            per_parent=CHILDREN_LIMIT,
            base=BASE_LABEL,
        )
        return _group_children(rows)

    async def rollup_edges(
        self,
        repo_full_name: str,
        *,
        edge_types: Sequence[str],
        expanded_files: Sequence[str] = (),
        limit: int = VIEW_EDGE_LIMIT,
    ) -> list[RollupEdge]:
        """Every relationship of these types, pre-aggregated for the view.

        Symbols are folded into their file *in the database* unless that file
        is open, which is what keeps a repository with a hundred thousand
        definitions from shipping a hundred thousand rows to render a picture
        of a dozen boxes. The remaining rollup — file into folder into folder —
        is arithmetic on ids and happens in the projection.
        """
        types = _validated_edge_types(edge_types)
        if not types:
            return []

        rows = await self._store.run(
            f"""
            MATCH (a:{BASE_LABEL} {{repo: $repo}})-[e]->(b:{BASE_LABEL} {{repo: $repo}})
            WHERE type(e) IN $types
            WITH e,
                 {_fold_fragment("a", "id")} AS source,
                 {_fold_fragment("a", "label")} AS source_label,
                 {_fold_fragment("b", "id")} AS target,
                 {_fold_fragment("b", "label")} AS target_label
            RETURN source, source_label, target, target_label, type(e) AS type,
                   sum(coalesce(e.count, e.weight, 1)) AS weight
            ORDER BY source, target, type
            LIMIT $limit
            """,
            repo=repo_full_name,
            types=types,
            expanded_files=list(expanded_files),
            prefix=f"{repo_full_name}:",
            symbol=str(NodeLabel.SYMBOL),
            file=str(NodeLabel.FILE),
            base=BASE_LABEL,
            limit=min(limit, VIEW_EDGE_LIMIT),
        )
        return _rollup_edges_from_rows(rows)


    # -- writes ------------------------------------------------------------

    async def _upsert_nodes(self, repo: str, version: str, nodes: Sequence[GraphNode]) -> None:
        by_label: dict[NodeLabel, list[dict]] = {}
        for node in nodes:
            by_label.setdefault(node.label, []).append(
                {"id": node.id, "properties": dict(node.properties)}
            )

        for label, rows in by_label.items():
            query = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{BASE_LABEL} {{id: row.id}}) "
                f"SET n:{_safe_label(label)}, n += row.properties, "
                f"    n.repo = $repo, n.version = $version"
            )
            for chunk in _chunks(rows):
                await self._store.run_write([(query, {"rows": list(chunk), "repo": repo, "version": version})])

    async def _upsert_edges(self, repo: str, version: str, edges: Sequence[GraphEdge]) -> None:
        by_type: dict[EdgeType, list[dict]] = {}
        for edge in edges:
            by_type.setdefault(edge.type, []).append({
                "source": edge.source,
                "target": edge.target,
                "properties": dict(edge.properties),
                "origin_file": edge.origin_file,
            })

        for edge_type, rows in by_type.items():
            query = (
                f"UNWIND $rows AS row "
                f"MATCH (a:{BASE_LABEL} {{id: row.source}}), (b:{BASE_LABEL} {{id: row.target}}) "
                f"MERGE (a)-[e:{_safe_type(edge_type)}]->(b) "
                f"SET e += row.properties, e.repo = $repo, e.version = $version, "
                f"    e.origin_file = row.origin_file"
            )
            for chunk in _chunks(rows):
                await self._store.run_write([(query, {"rows": list(chunk), "repo": repo, "version": version})])

    async def _delete_edges(self, repo: str, keys: Sequence[tuple[str, str, str]]) -> None:
        rows = [{"source": source, "target": target, "type": edge_type} for source, target, edge_type in keys]
        query = (
            f"UNWIND $rows AS row "
            f"MATCH (a:{BASE_LABEL} {{id: row.source}})-[e]->(b:{BASE_LABEL} {{id: row.target}}) "
            f"WHERE type(e) = row.type AND e.repo = $repo "
            f"DELETE e"
        )
        for chunk in _chunks(rows):
            await self._store.run_write([(query, {"rows": list(chunk), "repo": repo})])

    async def _delete_nodes(self, repo: str, node_ids: Sequence[str]) -> None:
        query = (
            f"UNWIND $ids AS id "
            f"MATCH (n:{BASE_LABEL} {{id: id, repo: $repo}}) "
            f"DETACH DELETE n"
        )
        for chunk in _chunks(node_ids):
            await self._store.run_write([(query, {"ids": list(chunk), "repo": repo})])


class InMemoryKnowledgeGraphStore:
    """Dict-backed store: the same behaviour, without a database."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple[str, str, str], dict] = {}
        self.schema_ensured = False

    async def ensure_schema(self) -> None:
        self.schema_ensured = True

    async def apply(self, delta: GraphDelta) -> None:
        for node in delta.upserted_nodes:
            self.nodes[node.id] = {
                "id": node.id,
                "label": str(node.label),
                "repo": delta.repo_full_name,
                "version": delta.version,
                **dict(node.properties),
            }
        for edge in delta.upserted_edges:
            if edge.source in self.nodes and edge.target in self.nodes:
                self.edges[edge.key] = {
                    "source": edge.source,
                    "target": edge.target,
                    "type": str(edge.type),
                    "repo": delta.repo_full_name,
                    "version": delta.version,
                    "origin_file": edge.origin_file,
                    **dict(edge.properties),
                }
        for key in delta.deleted_edge_keys:
            stored = self.edges.get(key)
            if stored and stored["repo"] == delta.repo_full_name:
                del self.edges[key]
        for node_id in delta.deleted_node_ids:
            stored = self.nodes.get(node_id)
            if stored and stored["repo"] == delta.repo_full_name:
                del self.nodes[node_id]
                self.edges = {
                    key: edge for key, edge in self.edges.items()
                    if node_id not in (edge["source"], edge["target"])
                }

    async def delete_repository(self, repo_full_name: str) -> None:
        removed = {node_id for node_id, node in self.nodes.items() if node["repo"] == repo_full_name}
        self.nodes = {node_id: node for node_id, node in self.nodes.items() if node_id not in removed}
        self.edges = {
            key: edge for key, edge in self.edges.items()
            if edge["source"] not in removed and edge["target"] not in removed
        }

    async def module_view(self, repo_full_name: str) -> dict:
        modules = {
            node_id: node for node_id, node in self.nodes.items()
            if node["repo"] == repo_full_name
            and node["label"] == str(NodeLabel.MODULE)
            and node.get("depth", 1) == 1
        }
        nodes = [
            {
                "id": node["id"], "name": node["name"], "kind": node["kind"],
                "file_count": node.get("file_count", 0),
                "definition_count": node.get("definition_count", 0),
            }
            for node in sorted(modules.values(), key=lambda n: n["id"])
        ]
        edges = [
            {
                "source": edge["source"], "target": edge["target"],
                "type": _MODULE_EDGE_WIRE_NAMES[EdgeType(edge["type"])],
                "weight": edge.get("weight"),
            }
            for edge in sorted(self.edges.values(), key=lambda e: (e["source"], e["target"]))
            if EdgeType(edge["type"]) in _MODULE_EDGE_WIRE_NAMES
            and edge["source"] in modules and edge["target"] in modules
        ]
        return {"nodes": nodes, "edges": edges}

    async def node_ids(self, repo_full_name: str) -> set[str]:
        return {node_id for node_id, node in self.nodes.items() if node["repo"] == repo_full_name}

    async def purge_legacy_nodes(self, batch: int = 5_000) -> int:
        return 0  # an in-memory store never held the previous version

    async def node_detail(self, repo_full_name: str, node_id: str) -> dict | None:
        node = self.nodes.get(node_id)
        if node is None or node["repo"] != repo_full_name:
            return None

        containment = {str(EdgeType.CONTAINS), str(EdgeType.DEFINES), str(EdgeType.HAS_MEMBER)}
        children = [
            {
                "id": child["id"], "name": child.get("name"),
                "kind": child.get("kind"), "path": child.get("path"),
            }
            for edge in self.edges.values()
            if edge["source"] == node_id and edge["type"] in containment
            for child in [self.nodes.get(edge["target"], {})]
            if child.get("id")
        ][:CHILDREN_LIMIT]

        properties = {
            key: value for key, value in node.items()
            if key not in ("id", "label", "repo", "version")
        }
        return {
            "id": node_id,
            "labels": [node["label"]],
            "properties": properties,
            "children": children,
        }

    async def neighbours(
        self,
        repo_full_name: str,
        node_id: str,
        *,
        direction: str = "both",
        edge_types: Sequence[str] | None = None,
        depth: int = 1,
        limit: int = 200,
    ) -> dict:
        validate_depth(depth)
        if direction not in ALLOWED_DIRECTIONS:
            raise ValueError(f"direction must be one of {ALLOWED_DIRECTIONS}")
        wanted = set(_validated_edge_types(edge_types) or ())

        start = self.nodes.get(node_id)
        if start is None or start["repo"] != repo_full_name:
            return {"nodes": [], "edges": []}

        reached = {node_id}
        collected: dict[tuple, dict] = {}
        frontier = {node_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for edge in self.edges.values():
                if edge["repo"] != repo_full_name or (wanted and edge["type"] not in wanted):
                    continue
                for origin, other in (("source", "target"), ("target", "source")):
                    if edge[origin] not in frontier:
                        continue
                    if direction == "out" and origin != "source":
                        continue
                    if direction == "in" and origin != "target":
                        continue
                    collected[(edge["source"], edge["target"], edge["type"])] = edge
                    next_frontier.add(edge[other])
            frontier = next_frontier - reached
            reached |= next_frontier

        nodes = [
            {
                "id": self.nodes[found]["id"], "name": self.nodes[found].get("name"),
                "kind": self.nodes[found].get("kind"), "labels": [self.nodes[found]["label"]],
            }
            for found in sorted(reached)
            if found in self.nodes and found != node_id
        ][:min(limit, NEIGHBOUR_LIMIT)]
        kept = {node["id"] for node in nodes} | {node_id}
        edges = [
            {
                "source": edge["source"], "target": edge["target"], "type": edge["type"],
                "count": edge.get("count"), "weight": edge.get("weight"),
            }
            for key, edge in sorted(collected.items())
            if edge["source"] in kept and edge["target"] in kept
        ]
        return {"nodes": nodes, "edges": edges}

    async def search(self, repo_full_name: str, term: str, *, limit: int = 20) -> list[dict]:
        needle = term.lower()
        matches = [
            {
                "id": node["id"], "name": node.get("name"), "kind": node.get("kind"),
                "path": node.get("path"), "file_path": node.get("file_path"),
                "signature": node.get("signature"), "labels": [node["label"]],
            }
            for node in self.nodes.values()
            if node["repo"] == repo_full_name
            and node["label"] in (str(NodeLabel.SYMBOL), str(NodeLabel.FILE))
            and needle in str(node.get("name") or node.get("path") or "").lower()
        ]
        matches.sort(key=lambda row: (len(str(row["name"] or row["path"] or "")), row["id"]))
        return matches[:min(limit, SEARCH_LIMIT)]

    async def view_children(
        self, repo_full_name: str, parent_ids: Sequence[str],
    ) -> dict[str, list[ViewNode]]:
        parents = list(dict.fromkeys(parent_ids))[:VIEW_PARENTS_LIMIT]
        wanted = {str(edge_type) for edge_type in CONTAINMENT_EDGE_TYPES}
        grouped: dict[str, list[ViewNode]] = {}

        for edge in sorted(self.edges.values(), key=lambda e: (e["source"], e["target"])):
            if edge["repo"] != repo_full_name or edge["type"] not in wanted:
                continue
            if edge["source"] not in parents:
                continue
            child = self.nodes.get(edge["target"])
            if child is None or child["repo"] != repo_full_name:
                continue
            siblings = grouped.setdefault(edge["source"], [])
            if len(siblings) >= CHILDREN_LIMIT:
                continue
            node = _view_node({
                **child,
                "label": child["label"],
                "child_count": self._child_count(repo_full_name, child["id"]),
            })
            if node is not None:
                siblings.append(node)

        return grouped

    async def rollup_edges(
        self,
        repo_full_name: str,
        *,
        edge_types: Sequence[str],
        expanded_files: Sequence[str] = (),
        limit: int = VIEW_EDGE_LIMIT,
    ) -> list[RollupEdge]:
        types = set(_validated_edge_types(edge_types) or ())
        if not types:
            return []
        open_files = set(expanded_files)
        weights: dict[tuple[str, str, str, str, str], int] = {}

        for edge in self.edges.values():
            if edge["repo"] != repo_full_name or edge["type"] not in types:
                continue
            source = self.nodes.get(edge["source"])
            target = self.nodes.get(edge["target"])
            if source is None or target is None:
                continue
            source_id, source_label = self._fold(repo_full_name, source, open_files)
            target_id, target_label = self._fold(repo_full_name, target, open_files)
            key = (source_id, source_label, target_id, target_label, edge["type"])
            weight = edge.get("count") or edge.get("weight") or 1
            weights[key] = weights.get(key, 0) + int(weight)

        rows = [
            {
                "source": source, "source_label": source_label,
                "target": target, "target_label": target_label,
                "type": edge_type, "weight": weight,
            }
            for (source, source_label, target, target_label, edge_type), weight
            in sorted(weights.items())
        ][:min(limit, VIEW_EDGE_LIMIT)]
        return _rollup_edges_from_rows(rows)

    def _child_count(self, repo_full_name: str, node_id: str) -> int:
        wanted = {str(edge_type) for edge_type in CONTAINMENT_EDGE_TYPES}
        return sum(
            1 for edge in self.edges.values()
            if edge["repo"] == repo_full_name
            and edge["source"] == node_id
            and edge["type"] in wanted
        )

    @staticmethod
    def _fold(
        repo_full_name: str, node: dict, open_files: set[str],
    ) -> tuple[str, str]:
        """Mirror of the Neo4j CASE: a symbol becomes its file unless it is open."""
        file_path = node.get("file_path")
        if (
            node["label"] == str(NodeLabel.SYMBOL)
            and file_path
            and file_path not in open_files
        ):
            return f"{repo_full_name}:{file_path}", str(NodeLabel.FILE)
        return node["id"], node["label"]

    async def counts(self, repo_full_name: str) -> dict:
        nodes_by_label: dict[str, int] = {}
        for node in self.nodes.values():
            if node["repo"] == repo_full_name:
                nodes_by_label[node["label"]] = nodes_by_label.get(node["label"], 0) + 1
        edges_by_type: dict[str, int] = {}
        for edge in self.edges.values():
            if edge["repo"] == repo_full_name:
                edges_by_type[edge["type"]] = edges_by_type.get(edge["type"], 0) + 1
        return {
            "nodes_by_label": dict(sorted(nodes_by_label.items())),
            "edges_by_type": dict(sorted(edges_by_type.items())),
        }


_store: Any | None = None


def get_knowledge_graph_store():
    global _store
    if _store is None:
        _store = Neo4jKnowledgeGraphStore()
    return _store


def use_knowledge_graph_store(store) -> None:
    """Inject a store; tests use the in-memory implementation here."""
    global _store
    _store = store


__all__ = [
    "BASE_LABEL",
    "EXTERNAL_MODULE",
    "InMemoryKnowledgeGraphStore",
    "Neo4jKnowledgeGraphStore",
    "get_knowledge_graph_store",
    "use_knowledge_graph_store",
]
