"""Storage adapter for extraction facts (the `FactsStore` port).

Two implementations: Mongo for production, and a dict-backed fake so the
pipeline can be tested without a database. Both are scoped by repository —
every query filters on `repo_full_name`, so one repository's facts can never
be read or overwritten through another's.

What is stored is deliberately small: names, kinds, line numbers, truncated
signatures and docstrings that have already been through secret redaction.
File contents are never stored.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

import structlog

from app.db import mongo
from app.knowledge_graph.facts import PARSER_VERSION, FileFacts, facts_from_dict
from app.knowledge_graph.ports import FactsStore

logger = structlog.get_logger("app.facts")

#: Mongo rejects documents over 16MB. Facts for one file are far smaller, but
#: a generated file can hold tens of thousands of calls, so we shed the
#: unbounded parts rather than failing the write.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
#: Writes are chunked so one batch cannot build an oversized command.
WRITE_CHUNK = 500


def _document(repo_full_name: str, facts: FileFacts) -> dict[str, Any]:
    payload = facts.to_dict()
    document = {
        "repo_full_name": repo_full_name,
        "path": facts.path,
        "language": facts.language,
        "content_hash": facts.content_hash,
        "parser_version": facts.parser_version,
        "facts": payload,
    }
    if _approximate_size(document) > MAX_DOCUMENT_BYTES:
        # Keep the structural facts, drop the unbounded reference lists.
        payload["calls"] = []
        payload["renders"] = []
        payload["truncated"] = True
        logger.warning("facts_document_truncated", repo=repo_full_name, path=facts.path)
    return document


def _approximate_size(document: dict[str, Any]) -> int:
    facts = document["facts"]
    return 512 + sum(
        len(facts.get(key, ())) * 160
        for key in ("definitions", "imports", "calls", "renders", "inheritance")
    )


class MongoFactsStore:
    """FactsStore backed by the `file_facts` collection."""

    async def fingerprints(self, repo_full_name: str) -> dict[str, str]:
        cursor = mongo.file_facts().find(
            {"repo_full_name": repo_full_name, "parser_version": PARSER_VERSION},
            {"path": 1, "content_hash": 1},
        )
        return {doc["path"]: doc["content_hash"] async for doc in cursor}

    async def load_all(self, repo_full_name: str) -> dict[str, FileFacts]:
        cursor = mongo.file_facts().find({"repo_full_name": repo_full_name}, {"facts": 1})
        return {
            doc["facts"]["path"]: facts_from_dict(doc["facts"])
            async for doc in cursor
            if doc.get("facts")
        }

    async def save_many(self, repo_full_name: str, facts: Sequence[FileFacts]) -> None:
        """Upsert per file, a chunk at a time.

        Used by incremental syncs, which touch a handful of files. Writes in a
        chunk are issued concurrently so the round trips overlap.
        """
        for start in range(0, len(facts), WRITE_CHUNK):
            await asyncio.gather(*(
                mongo.file_facts().update_one(
                    {"repo_full_name": repo_full_name, "path": item.path},
                    {"$set": _document(repo_full_name, item)},
                    upsert=True,
                )
                for item in facts[start:start + WRITE_CHUNK]
            ))

    async def delete_paths(self, repo_full_name: str, paths: Sequence[str]) -> None:
        if not paths:
            return
        await mongo.file_facts().delete_many(
            {"repo_full_name": repo_full_name, "path": {"$in": list(paths)}},
        )

    async def replace_all(self, repo_full_name: str, facts: Sequence[FileFacts]) -> None:
        """Swap in a whole repository's facts.

        Used by a full index, where inserting a clean set is much faster than
        thousands of upserts. Only this repository's documents are touched.
        """
        await mongo.file_facts().delete_many({"repo_full_name": repo_full_name})
        for start in range(0, len(facts), WRITE_CHUNK):
            chunk = facts[start:start + WRITE_CHUNK]
            if chunk:
                await mongo.file_facts().insert_many(
                    [_document(repo_full_name, item) for item in chunk],
                    ordered=False,
                )

    async def delete_repo(self, repo_full_name: str) -> None:
        await mongo.file_facts().delete_many({"repo_full_name": repo_full_name})


class InMemoryFactsStore:
    """Dict-backed FactsStore for tests and local runs without Mongo."""

    def __init__(self) -> None:
        self._by_repo: dict[str, dict[str, FileFacts]] = {}

    async def fingerprints(self, repo_full_name: str) -> dict[str, str]:
        return {
            path: facts.content_hash
            for path, facts in self._by_repo.get(repo_full_name, {}).items()
            if facts.parser_version == PARSER_VERSION
        }

    async def load_all(self, repo_full_name: str) -> dict[str, FileFacts]:
        return dict(self._by_repo.get(repo_full_name, {}))

    async def save_many(self, repo_full_name: str, facts: Sequence[FileFacts]) -> None:
        repo = self._by_repo.setdefault(repo_full_name, {})
        repo.update({item.path: item for item in facts})

    async def delete_paths(self, repo_full_name: str, paths: Sequence[str]) -> None:
        repo = self._by_repo.get(repo_full_name, {})
        for path in paths:
            repo.pop(path, None)

    async def replace_all(self, repo_full_name: str, facts: Sequence[FileFacts]) -> None:
        self._by_repo[repo_full_name] = {item.path: item for item in facts}

    async def delete_repo(self, repo_full_name: str) -> None:
        self._by_repo.pop(repo_full_name, None)


_store: FactsStore | None = None


def get_facts_store() -> FactsStore:
    global _store
    if _store is None:
        _store = MongoFactsStore()
    return _store


def use_facts_store(store: FactsStore | None) -> None:
    """Inject a store; tests use the in-memory implementation here."""
    global _store
    _store = store
