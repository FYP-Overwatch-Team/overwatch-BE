"""The indexing pipeline: checkout in, graph out.

    discover → hash → extract (only what changed) → link → build → diff → write

Every stage is a pure function or a port. This class owns the order and the
error handling, and nothing else: no database driver, no HTTP client, no git.
That is what lets the whole pipeline run in a test against in-memory stores
and a temporary directory.

Two behaviours worth knowing:

* **Content hashes decide what is re-read**, not the webhook payload. A
  force-push, a rewritten history or a payload that lies about its file list
  all produce the same correct result.
* **A sync re-links the whole repository** from cached facts (milliseconds)
  and writes only the difference. Renaming a function correctly removes the
  edge from a caller whose own file never changed.

CPU-bound stages run in a worker thread, so indexing never blocks the API.
"""

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app.knowledge_graph import project_config
from app.knowledge_graph.build import build_snapshot, diff_snapshots, full_delta
from app.knowledge_graph.build.model import GraphDelta, GraphSnapshot
from app.knowledge_graph.discovery import DiscoveredFile, discover
from app.knowledge_graph.extract.pool import (
    IN_PROCESS_THRESHOLD,
    ExtractionPool,
    extract_files,
)
from app.knowledge_graph.facts import FileFacts
from app.knowledge_graph.limits import DEFAULT_LIMITS, Limits
from app.knowledge_graph.link import link_repository
from app.knowledge_graph.ports import FactsStore, KnowledgeGraphStore
from app.knowledge_graph.sync_plan import SyncPlan, merge_facts, plan_sync

logger = structlog.get_logger("app.knowledge_graph.pipeline")

#: Stand-in commit for the rebuilt previous snapshot. The stored facts do not
#: record which commit they came from, and the repository node carries the
#: commit, so this guarantees that node is re-stamped on every sync.
_UNKNOWN_VERSION = "unknown"


@dataclass(frozen=True, slots=True)
class IndexResult:
    """What one indexing run did. Stored on the repository for the UI."""

    repo_full_name: str
    version: str
    files_indexed: int
    plan: Mapping[str, int] = field(default_factory=dict)
    delta: Mapping[str, int] = field(default_factory=dict)
    stats: Mapping[str, Any] = field(default_factory=dict)

    def as_stats_document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "files": self.files_indexed,
            "plan": dict(self.plan),
            "delta": dict(self.delta),
            **dict(self.stats),
        }


class IndexingPipeline:
    """Runs the stages. Construct once per request or job; it holds no state."""

    def __init__(
        self,
        facts_store: FactsStore,
        graph_store: KnowledgeGraphStore,
        limits: Limits = DEFAULT_LIMITS,
    ):
        self._facts = facts_store
        self._graph = graph_store
        self._limits = limits

    async def index(
        self,
        repo_full_name: str,
        checkout: Path,
        version: str,
        *,
        full: bool = False,
    ) -> IndexResult:
        """Index a checkout, writing only what changed unless `full` is set."""
        discovered, skipped = await asyncio.to_thread(self._discover, checkout)
        config = await asyncio.to_thread(project_config.load, checkout)
        hashes = await asyncio.to_thread(_hash_files, discovered)

        stored_fingerprints = {} if full else await self._facts.fingerprints(repo_full_name)
        plan = plan_sync(discovered, stored_fingerprints, known_hashes=hashes)

        outcomes = await asyncio.to_thread(self._extract, plan.to_extract)
        extracted = [outcome.facts for outcome in outcomes if outcome.facts is not None]
        for outcome in outcomes:
            if outcome.skip_reason is not None:
                skipped[str(outcome.skip_reason)] = skipped.get(str(outcome.skip_reason), 0) + 1

        stored_facts = {} if full else await self._facts.load_all(repo_full_name)
        current_facts = merge_facts(stored_facts, extracted, plan.deleted)

        delta, snapshot = await asyncio.to_thread(
            self._build_delta,
            repo_full_name, version, config, stored_facts, current_facts, skipped, full,
        )

        if full:
            # A full index starts from a clean slate, so nothing an earlier
            # run left behind can survive as an orphan.
            await self._graph.delete_repository(repo_full_name)

        # Graph first, facts second. If the graph write fails, the facts cache
        # still describes the previous commit, so the next sync re-does the
        # work rather than believing it is already applied.
        await self._graph.apply(delta)
        await self._persist_facts(repo_full_name, extracted, plan, full)

        result = IndexResult(
            repo_full_name=repo_full_name,
            version=version,
            files_indexed=len(current_facts),
            plan=plan.summary(),
            delta=delta.summary(),
            stats={**snapshot.stats, "skipped": skipped},
        )
        logger.info(
            "index_complete",
            repo=repo_full_name,
            version=version,
            full=full,
            **plan.summary(),
            **delta.summary(),
        )
        return result

    # -- stages ------------------------------------------------------------

    def _discover(self, checkout: Path) -> tuple[list[DiscoveredFile], dict[str, int]]:
        result = discover(checkout, self._limits)
        skipped = {str(reason): count for reason, count in result.skipped.items()}
        if result.capped:
            logger.warning("repository_cap_reached", files=len(result.files))
        return result.files, skipped

    def _extract(self, files: Sequence[DiscoveredFile]):
        """Blocking. Small batches stay in-process; a full index uses workers."""
        if len(files) <= IN_PROCESS_THRESHOLD:
            return extract_files(files, self._limits)
        with ExtractionPool(self._limits) as pool:
            return pool.extract_all(files)

    def _build_delta(
        self,
        repo_full_name: str,
        version: str,
        config: project_config.ProjectConfig,
        stored_facts: Mapping[str, FileFacts],
        current_facts: Mapping[str, FileFacts],
        skipped: Mapping[str, int],
        full: bool,
    ) -> tuple[GraphDelta, GraphSnapshot]:
        """Blocking. Link and build the new graph, then diff it against the old.

        The previous graph is rebuilt from the stored facts rather than read
        back from the database: linking is measured in milliseconds, and it
        keeps the diff a pure function of data we already hold.
        """
        links = link_repository(current_facts, config)
        snapshot = build_snapshot(
            repo_full_name, version, current_facts, links,
            limits=self._limits, extra_stats={"skipped": dict(skipped)},
        )

        if full or not stored_facts:
            return full_delta(snapshot), snapshot

        previous_links = link_repository(stored_facts, config)
        previous = build_snapshot(
            repo_full_name, _UNKNOWN_VERSION, stored_facts, previous_links, limits=self._limits,
        )
        return diff_snapshots(previous, snapshot), snapshot

    async def _persist_facts(
        self,
        repo_full_name: str,
        extracted: Sequence[FileFacts],
        plan: SyncPlan,
        full: bool,
    ) -> None:
        if full:
            await self._facts.replace_all(repo_full_name, extracted)
            return
        await self._facts.save_many(repo_full_name, extracted)
        await self._facts.delete_paths(repo_full_name, plan.deleted)


def _hash_files(files: Sequence[DiscoveredFile]) -> dict[str, str]:
    """Content hash per file. Blocking.

    Deliberately a second read of each file: hashing decides what to extract,
    and extraction happens in worker processes that read the file themselves.
    Measured at 0.42s for a 13k-file repository, against 12s to parse it.
    """
    hashes: dict[str, str] = {}
    for file in files:
        try:
            hashes[file.relative_path] = hashlib.sha256(file.path.read_bytes()).hexdigest()
        except OSError:
            continue  # vanished or unreadable; treated as changed
    return hashes
