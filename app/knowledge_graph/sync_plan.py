"""Deciding what actually needs re-reading.

Extraction is the expensive stage, so the pipeline compares what is on disk
against what is already stored and re-extracts only the difference. That
comparison is pure arithmetic over hashes, which keeps it trivially testable
and independent of both the filesystem and the database.

Content hashes — not webhook payloads, timestamps or commit ranges — are what
decide. A push that rewrites history, a force-push, or a payload that lies
about which files changed all produce the same correct answer.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.knowledge_graph.discovery import DiscoveredFile
from app.knowledge_graph.facts import FileFacts


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """What to extract, what to keep, and what to forget."""

    #: Files whose contents differ from what is stored (or are new).
    to_extract: tuple[DiscoveredFile, ...] = ()
    #: Paths whose stored facts are still valid.
    unchanged: tuple[str, ...] = ()
    #: Paths that are stored but no longer present in the checkout.
    deleted: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.to_extract and not self.deleted

    def summary(self) -> dict[str, int]:
        return {
            "extract": len(self.to_extract),
            "unchanged": len(self.unchanged),
            "deleted": len(self.deleted),
        }


def plan_sync(
    discovered: Sequence[DiscoveredFile],
    stored_fingerprints: Mapping[str, str],
    *,
    known_hashes: Mapping[str, str] | None = None,
) -> SyncPlan:
    """Compare a checkout against stored facts.

    `stored_fingerprints` maps path -> content hash, and already excludes
    facts written by an older parser version, so those are re-extracted.

    `known_hashes` lets a caller that has already hashed files (for example
    after reading them) avoid a second read. Anything missing from it is
    treated as changed, which is the safe direction: at worst we re-extract.
    """
    hashes = known_hashes or {}
    to_extract: list[DiscoveredFile] = []
    unchanged: list[str] = []

    for file in discovered:
        stored_hash = stored_fingerprints.get(file.relative_path)
        current_hash = hashes.get(file.relative_path)
        if stored_hash is not None and current_hash is not None and stored_hash == current_hash:
            unchanged.append(file.relative_path)
        else:
            to_extract.append(file)

    present = {file.relative_path for file in discovered}
    deleted = sorted(path for path in stored_fingerprints if path not in present)

    return SyncPlan(
        to_extract=tuple(to_extract),
        unchanged=tuple(unchanged),
        deleted=tuple(deleted),
    )


def merge_facts(
    stored: Mapping[str, FileFacts],
    extracted: Sequence[FileFacts],
    deleted: Sequence[str] = (),
) -> dict[str, FileFacts]:
    """Freshly extracted facts laid over the stored ones.

    The linker needs the whole repository, but a sync only re-extracts part of
    it; this is what reassembles the complete picture without re-reading files.
    """
    dropped = set(deleted)
    merged = {path: facts for path, facts in stored.items() if path not in dropped}
    merged.update({facts.path: facts for facts in extracted})
    return merged
