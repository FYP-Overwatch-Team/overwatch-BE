"""Ports: what the pipeline needs from the outside world.

Declared here, in the domain, and implemented in app/services — so the
dependency points inward. The pipeline can be exercised end to end against
in-memory fakes, and swapping Mongo for something else touches one adapter.
"""

from collections.abc import Sequence
from typing import Protocol

from app.knowledge_graph.build.model import GraphDelta
from app.knowledge_graph.facts import FileFacts


class FactsStore(Protocol):
    """Per-file extraction results, keyed by repository and path.

    This is a cache with a purpose: it is what makes an incremental sync cheap
    (only changed files are re-extracted) and what lets the linker re-run over
    a whole repository without touching the filesystem.

    Every method is scoped by `repo_full_name`. Nothing here may read or write
    across repositories.
    """

    async def fingerprints(self, repo_full_name: str) -> dict[str, str]:
        """Map of path -> content hash for facts that are still usable.

        Entries written by an older parser version are omitted, so a parser
        change re-extracts rather than mixing old and new facts.
        """

    async def load_all(self, repo_full_name: str) -> dict[str, FileFacts]:
        """Every stored fact for a repository, keyed by path."""

    async def save_many(self, repo_full_name: str, facts: Sequence[FileFacts]) -> None:
        """Insert or replace facts for the given files."""

    async def delete_paths(self, repo_full_name: str, paths: Sequence[str]) -> None:
        """Forget files that no longer exist in the repository."""

    async def replace_all(self, repo_full_name: str, facts: Sequence[FileFacts]) -> None:
        """Swap in a complete set of facts, dropping anything not included."""

    async def delete_repo(self, repo_full_name: str) -> None:
        """Remove everything for a repository, e.g. when it is disconnected."""


class KnowledgeGraphStore(Protocol):
    """The graph itself: nodes, edges, and the queries the API serves from.

    Implementations make the stored graph match a `GraphDelta`. They own the
    query language; the pipeline only hands them data. As with facts, every
    operation is scoped to one repository.
    """

    async def ensure_schema(self) -> None:
        """Create constraints and indexes. Idempotent; safe on every boot."""

    async def apply(self, delta: GraphDelta) -> None:
        """Write upserts and deletions for one repository."""

    async def delete_repository(self, repo_full_name: str) -> None:
        """Remove a repository's graph entirely."""

    async def module_view(self, repo_full_name: str) -> dict:
        """The module-level nodes and edges the dashboard renders."""

    async def node_ids(self, repo_full_name: str) -> set[str]:
        """Every node id stored for a repository."""

    async def node_detail(self, repo_full_name: str, node_id: str) -> dict | None:
        """One node and what it contains, or None if it is not in this repository."""

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
        """What a node references and what references it."""

    async def search(
        self, repo_full_name: str, term: str, *, limit: int = 20,
    ) -> list[dict]:
        """Files and symbols whose name matches, for jump-to and grounding."""

    async def counts(self, repo_full_name: str) -> dict:
        """Live node and edge counts, by label and type."""

    async def purge_legacy_nodes(self, batch: int = 5_000) -> int:
        """Remove nodes written by a superseded version of the graph."""
