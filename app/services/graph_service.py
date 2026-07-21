from typing import Protocol

from app.db.neo4j_client import get_graph_store


class GraphRepository(Protocol):
    """Persistence interface for the architecture graph; in-memory fake in tests."""

    async def upsert_graph(self, repo_full_name: str, graph: dict) -> None: ...

    async def fetch_graph(self, repo_full_name: str) -> dict: ...

    async def delete_nodes(self, node_ids: list[str]) -> None: ...

    async def existing_node_ids(self, node_ids: list[str]) -> set[str]: ...

    async def neighborhood(self, node_id: str, hops: int = 2) -> dict: ...


class Neo4jGraphRepository:
    def __init__(self, store=None):
        self._store = store or get_graph_store()

    async def upsert_graph(self, repo_full_name: str, graph: dict) -> None:
        await self._store.run(
            """
            UNWIND $nodes AS n
            MERGE (s:Service {id: n.id})
            SET s.name = n.name, s.kind = n.kind, s.repo = $repo,
                s.file_count = n.file_count, s.definition_count = n.definition_count
            """,
            nodes=graph["nodes"], repo=repo_full_name,
        )
        # replace this repo's edges wholesale: stale edges must not survive a re-parse
        await self._store.run(
            "MATCH (a:Service {repo: $repo})-[r]->(:Service) DELETE r",
            repo=repo_full_name,
        )
        await self._store.run(
            """
            UNWIND $edges AS e
            MATCH (a:Service {id: e.source}), (b:Service {id: e.target})
            MERGE (a)-[r:DEPENDS_ON {type: e.type}]->(b)
            SET r.weight = e.weight
            """,
            edges=graph["edges"],
        )
        # drop nodes that disappeared from the latest parse
        keep = [n["id"] for n in graph["nodes"]]
        await self._store.run(
            "MATCH (s:Service {repo: $repo}) WHERE NOT s.id IN $keep DETACH DELETE s",
            repo=repo_full_name, keep=keep,
        )

    async def fetch_graph(self, repo_full_name: str) -> dict:
        nodes = await self._store.run(
            """
            MATCH (s:Service {repo: $repo})
            RETURN s.id AS id, s.name AS name, s.kind AS kind,
                   s.file_count AS file_count, s.definition_count AS definition_count
            ORDER BY id
            """,
            repo=repo_full_name,
        )
        edges = await self._store.run(
            """
            MATCH (a:Service {repo: $repo})-[r]->(b:Service)
            RETURN a.id AS source, b.id AS target, r.type AS type, r.weight AS weight
            ORDER BY source, target
            """,
            repo=repo_full_name,
        )
        return {"nodes": nodes, "edges": edges}

    async def delete_nodes(self, node_ids: list[str]) -> None:
        await self._store.run(
            "MATCH (s:Service) WHERE s.id IN $ids DETACH DELETE s", ids=node_ids,
        )

    async def existing_node_ids(self, node_ids: list[str]) -> set[str]:
        rows = await self._store.run(
            "MATCH (s:Service) WHERE s.id IN $ids RETURN s.id AS id", ids=node_ids,
        )
        return {row["id"] for row in rows}

    async def neighborhood(self, node_id: str, hops: int = 2) -> dict:
        rows = await self._store.run(
            """
            MATCH path = (s:Service {id: $id})-[*0..%d]-(m:Service)
            UNWIND nodes(path) AS n
            WITH collect(DISTINCT n) AS ns
            UNWIND ns AS n
            OPTIONAL MATCH (n)-[r]->(t:Service) WHERE t IN ns
            RETURN DISTINCT n.id AS id, n.name AS name, n.kind AS kind,
                   t.id AS target, r.type AS edge_type, r.weight AS weight
            """ % hops,
            id=node_id,
        )
        nodes: dict[str, dict] = {}
        edges = []
        for row in rows:
            nodes.setdefault(row["id"], {"id": row["id"], "name": row["name"], "kind": row["kind"]})
            if row.get("target"):
                edges.append({
                    "source": row["id"], "target": row["target"],
                    "type": row["edge_type"], "weight": row["weight"],
                })
        return {"nodes": sorted(nodes.values(), key=lambda n: n["id"]),
                "edges": sorted(edges, key=lambda e: (e["source"], e["target"]))}


class InMemoryGraphRepository:
    """Dict-backed GraphRepository used by tests (and available for local dev without Neo4j)."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []

    async def upsert_graph(self, repo_full_name: str, graph: dict) -> None:
        incoming_ids = {n["id"] for n in graph["nodes"]}
        for node in graph["nodes"]:
            self.nodes[node["id"]] = {**node, "repo": repo_full_name}
        # drop this repo's stale nodes/edges, keep other repos untouched
        stale = [nid for nid, n in self.nodes.items()
                 if n["repo"] == repo_full_name and nid not in incoming_ids]
        for nid in stale:
            del self.nodes[nid]
        self.edges = [e for e in self.edges if e["repo"] != repo_full_name]
        self.edges.extend({**e, "repo": repo_full_name} for e in graph["edges"])

    async def fetch_graph(self, repo_full_name: str) -> dict:
        nodes = sorted(
            ({k: v for k, v in n.items() if k != "repo"}
             for n in self.nodes.values() if n["repo"] == repo_full_name),
            key=lambda n: n["id"],
        )
        edges = sorted(
            ({k: v for k, v in e.items() if k != "repo"}
             for e in self.edges if e["repo"] == repo_full_name),
            key=lambda e: (e["source"], e["target"]),
        )
        return {"nodes": nodes, "edges": edges}

    async def delete_nodes(self, node_ids: list[str]) -> None:
        for nid in node_ids:
            self.nodes.pop(nid, None)
        self.edges = [e for e in self.edges if e["source"] not in node_ids and e["target"] not in node_ids]

    async def existing_node_ids(self, node_ids: list[str]) -> set[str]:
        return {nid for nid in node_ids if nid in self.nodes}

    async def neighborhood(self, node_id: str, hops: int = 2) -> dict:
        if node_id not in self.nodes:
            return {"nodes": [], "edges": []}
        reachable = {node_id}
        for _ in range(hops):
            for e in self.edges:
                if e["source"] in reachable:
                    reachable.add(e["target"])
                if e["target"] in reachable:
                    reachable.add(e["source"])
        nodes = sorted(
            ({"id": n["id"], "name": n["name"], "kind": n["kind"]}
             for nid, n in self.nodes.items() if nid in reachable),
            key=lambda n: n["id"],
        )
        edges = sorted(
            ({"source": e["source"], "target": e["target"], "type": e["type"], "weight": e["weight"]}
             for e in self.edges if e["source"] in reachable and e["target"] in reachable),
            key=lambda e: (e["source"], e["target"]),
        )
        return {"nodes": nodes, "edges": edges}


_repository: GraphRepository | None = None


def get_graph_repository() -> GraphRepository:
    global _repository
    if _repository is None:
        _repository = Neo4jGraphRepository()
    return _repository


def use_graph_repository(repo: GraphRepository | None) -> None:
    global _repository
    _repository = repo
