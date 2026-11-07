"""A repository's code embedded by meaning, so the chat finds code whose names don't match the question.

Each file gets one vector (its path, what it defines, its opening lines) and each substantial
definition one more (its name, signature, docstring and first lines). Tests, constants and tiny
functions are left out: name search and grep find those. Vectors belong to the repository, like
its graph, and are synced after every index: only text that changed is embedded again, and
what's gone is deleted. Only vectors and fingerprints are stored, never the code.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

import structlog
from pymongo import DeleteMany, UpdateOne

from app.core.config import get_settings
from app.core.constants import SYSTEM_USER, EmbeddingKind, EmbedPurpose, UsageFeature
from app.core.time import utcnow
from app.db import mongo
from app.embeddings import service as embeddings
from app.integrations.embeddings import BATCH_SIZE, get_embedder
from app.knowledge_graph.build.ids import definition_ids, file_id
from app.knowledge_graph.facts import (
    KIND_CLASS,
    KIND_COMPONENT,
    KIND_FUNCTION,
    KIND_INTERFACE,
    KIND_METHOD,
    KIND_TYPE,
    Definition,
    FileFacts,
)
from app.knowledge_graph.redaction import redact
from app.query.source import read_file
from app.usage import service as usage

logger = structlog.get_logger("app.embeddings")

#: folders whose files are tests, and file names that are
TEST_DIRECTORIES = frozenset({"tests", "test", "__tests__", "spec", "specs", "__mocks__", "fixtures"})
_TEST_FILE_RE = re.compile(r"^test_.*\.py$|_test\.py$|^conftest\.py$|\.(test|spec|stories)\.[^.]+$")
#: worth a vector whatever their size; functions and methods need `MIN_SYMBOL_LINES`
_SHAPE_KINDS = frozenset({KIND_CLASS, KIND_COMPONENT, KIND_INTERFACE, KIND_TYPE})
_SIZED_KINDS = frozenset({KIND_FUNCTION, KIND_METHOD})
MIN_SYMBOL_LINES = 4
#: what a definition's text holds: its first lines, at most this many characters in all
MAX_BODY_LINES = 40
MAX_CODE_CHARS = 2000
#: what a file's text holds: the names it defines, then its opening, at most this many characters
MAX_FILE_NAMES = 60
MAX_FILE_CHARS = 3000
#: kept first when a repository has more than its limit: files, routes, shapes, exported, then the rest
_FILE, _ROUTE, _SHAPE, _EXPORTED, _OTHER = range(5)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Something to embed: a file (`definition` None) or one of its definitions, by graph node id."""

    ref_id: str
    path: str
    definition: Definition | None = None


def enabled() -> bool:
    return embeddings.enabled() and get_settings().code_embedding_repo_limit > 0


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in TEST_DIRECTORIES for part in parts[:-1]) or bool(_TEST_FILE_RE.search(parts[-1]))


def candidates(repo_full_name: str, facts_by_path: Mapping[str, FileFacts], include_symbols: bool) -> list[Candidate]:
    """What's worth a vector, most worth it first; definitions only when the graph has them, so every id is a node."""
    ranked: list[tuple[tuple, Candidate]] = []
    for path in sorted(facts_by_path):
        if is_test_path(path):
            continue
        facts = facts_by_path[path]
        ranked.append(((_FILE, path), Candidate(file_id(repo_full_name, path), path)))
        if not include_symbols:
            continue
        handlers = {route.handler for route in facts.routes if route.handler}
        for definition, ref_id in definition_ids(repo_full_name, path, facts.definitions):
            lines = definition.end_line - definition.start_line + 1
            if definition.qualified_name in handlers:
                rank = (_ROUTE, path)
            elif definition.kind in _SHAPE_KINDS:
                rank = (_SHAPE, path)
            elif definition.kind in _SIZED_KINDS and lines >= MIN_SYMBOL_LINES:
                rank = (_EXPORTED, path) if definition.exported else (_OTHER, -lines, path)
            else:
                continue
            ranked.append((rank, Candidate(ref_id, path, definition)))
    return [candidate for _, candidate in sorted(ranked, key=lambda row: row[0])]


def file_text(path: str, facts: FileFacts, source: str) -> str:
    names = [
        f"{definition.kind} {definition.qualified_name}" + (f": {definition.signature}" if definition.signature else "")
        for definition in facts.definitions if definition.parent is None
    ][:MAX_FILE_NAMES]
    return redact("\n".join([path, facts.language, *names, "", source]))[:MAX_FILE_CHARS]


def symbol_text(path: str, definition: Definition, lines: list[str]) -> str:
    end = min(definition.end_line, definition.start_line + MAX_BODY_LINES - 1)
    body = "\n".join(lines[definition.start_line - 1:end])
    parts = [path, f"{definition.kind} {definition.qualified_name}", definition.signature, definition.docstring or "", body]
    return redact("\n".join(part for part in parts if part))[:MAX_CODE_CHARS]


async def sync(
    repo_full_name: str, version: str, facts_by_path: Mapping[str, FileFacts], *, include_symbols: bool,
    workspace_id: str,
) -> dict[str, int]:
    """Bring the repository's vectors in line with its code at `version`; what it did, by count.

    Embedding is charged to `workspace_id` as background work. Vectors are written a request at a
    time, so a failure part way (raised as `ExternalServiceError`) keeps what was done, and the
    next index carries on from there.
    """
    embedder = get_embedder()
    settings = get_settings()
    if embedder is None or settings.code_embedding_repo_limit <= 0:
        return {}
    found = candidates(repo_full_name, facts_by_path, include_symbols)
    wanted = found[:settings.code_embedding_repo_limit]
    if len(found) > len(wanted):
        logger.info("code_embeddings_capped", repo=repo_full_name, wanted=len(found), kept=len(wanted))

    scope = {"repo": repo_full_name, "kind": EmbeddingKind.CODE}
    stored = {
        doc["ref_id"]: doc
        async for doc in mongo.embeddings().find({**scope, "model": embedder.model}, {"ref_id": 1, "fingerprint": 1, "file_hash": 1})
    }
    wanted_ids = {candidate.ref_id for candidate in wanted}
    gone = [ref_id for ref_id in stored if ref_id not in wanted_ids]
    # another model's vectors are never compared with these, and what's no longer wanted goes
    cleared = await mongo.embeddings().bulk_write([
        DeleteMany({**scope, "model": {"$ne": embedder.model}}),
        DeleteMany({**scope, "ref_id": {"$in": gone}}),
    ], ordered=False)

    # only files that changed since their vectors were made are read again
    outdated = [
        candidate for candidate in wanted
        if stored.get(candidate.ref_id, {}).get("file_hash") != facts_by_path[candidate.path].content_hash
    ]
    sources: dict[str, str | None] = {}
    for path in dict.fromkeys(candidate.path for candidate in outdated):
        sources[path] = await read_file(repo_full_name, version, path)

    room = max(0, settings.code_embedding_total_limit - await mongo.embeddings().count_documents({"kind": EmbeddingKind.CODE}))
    now = utcnow()
    same_text: list[UpdateOne] = []
    to_embed: list[tuple[Candidate, str, str]] = []
    no_room = 0
    for candidate in outdated:
        source = sources[candidate.path]
        if source is None:
            continue  # unreadable at this commit; its vector, if any, stays until it can be read
        facts = facts_by_path[candidate.path]
        text = (
            symbol_text(candidate.path, candidate.definition, source.splitlines())
            if candidate.definition else file_text(candidate.path, facts, source)
        )
        current = embeddings.fingerprint(text)
        if stored.get(candidate.ref_id, {}).get("fingerprint") == current:
            # moved, not changed: lines shifted by an edit elsewhere in the file
            same_text.append(UpdateOne(
                {**scope, "ref_id": candidate.ref_id}, {"$set": {"file_hash": facts.content_hash, "path": candidate.path}},
            ))
        elif candidate.ref_id in stored or room > 0:
            room -= candidate.ref_id not in stored
            to_embed.append((candidate, text, current))
        else:
            no_room += 1  # every repository's vectors together are at `code_embedding_total_limit`
    deferred = max(0, len(to_embed) - settings.code_embedding_run_limit)
    to_embed = to_embed[:settings.code_embedding_run_limit]
    if same_text:
        await mongo.embeddings().bulk_write(same_text, ordered=False)

    on_usage = usage.recorder(workspace_id, SYSTEM_USER, UsageFeature.EMBEDDINGS)
    for start in range(0, len(to_embed), BATCH_SIZE):
        chunk = to_embed[start:start + BATCH_SIZE]
        vectors = await embedder.embed([text for _, text, _ in chunk], EmbedPurpose.DOCUMENT, on_usage=on_usage)
        await mongo.embeddings().bulk_write([
            UpdateOne(
                {**scope, "ref_id": candidate.ref_id},
                {"$set": {
                    "model": embedder.model, "fingerprint": current, "vector": embeddings.encode(vector),
                    "file_hash": facts_by_path[candidate.path].content_hash, "path": candidate.path, "updated_at": now,
                }},
                upsert=True,
            )
            for (candidate, _, current), vector in zip(chunk, vectors, strict=True)
        ], ordered=False)

    if deferred or no_room:
        logger.info("code_embeddings_deferred", repo=repo_full_name, deferred=deferred, no_room=no_room)
    return {
        "embedded": len(to_embed),
        "deleted": cleared.deleted_count,
        "capped": len(found) - len(wanted),
        "deferred": deferred,
        "no_room": no_room,
    }


async def delete_repo(repo_full_name: str) -> None:
    await mongo.embeddings().delete_many({"repo": repo_full_name, "kind": EmbeddingKind.CODE})


async def similar(workspace_id: str, user_id: str, repo_full_name: str, text: str, limit: int) -> list[tuple[str, float]]:
    """The graph node ids of the repository's code closest in meaning to `text`, closest first, with their scores."""
    if not enabled():
        return []
    vector = await embeddings.embed_query(workspace_id, user_id, text, EmbedPurpose.CODE_QUERY)
    if vector is None:
        return []
    threshold = get_settings().code_similarity_threshold
    return [(ref_id, score) for ref_id, score in await embeddings.nearest_code(repo_full_name, vector, limit) if score >= threshold]
