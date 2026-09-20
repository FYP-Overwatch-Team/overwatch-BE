"""Small builder for seeding a knowledge graph in tests.

Keeps tests about what they assert rather than about constructing deltas.
"""

from app.knowledge_graph.build.ids import module_id
from app.knowledge_graph.build.model import (
    EdgeType,
    GraphDelta,
    GraphEdge,
    GraphNode,
    NodeLabel,
)


class GraphSeed:
    """Fluent builder for a module-level graph."""

    def __init__(self, repo_full_name: str, version: str = "sha-1"):
        self.repo = repo_full_name
        self.version = version
        self._nodes: list[GraphNode] = []
        self._edges: list[GraphEdge] = []

    def modules(self, *names: str, files: int = 1, definitions: int = 1) -> "GraphSeed":
        self._nodes.extend(
            GraphNode(
                id=module_id(self.repo, name),
                label=NodeLabel.MODULE,
                properties={
                    "name": name, "kind": "module",
                    "file_count": files, "definition_count": definitions,
                },
            )
            for name in names
        )
        return self

    def depends(self, source: str, target: str, weight: int = 1) -> "GraphSeed":
        self._edges.append(
            GraphEdge(
                module_id(self.repo, source),
                module_id(self.repo, target),
                EdgeType.DEPENDS_ON,
                {"weight": weight},
            )
        )
        return self

    def delta(self) -> GraphDelta:
        return GraphDelta(
            repo_full_name=self.repo,
            version=self.version,
            upserted_nodes=tuple(self._nodes),
            upserted_edges=tuple(self._edges),
        )

    async def apply(self, store) -> None:
        await store.apply(self.delta())
