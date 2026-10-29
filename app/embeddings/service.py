"""Vectors for work items, pages and decisions, kept current, and the ones nearest in meaning.

Each record keeps the fingerprint of the text it was made from and the model that made it, so
only changed texts are embedded again, and a new model's vectors are never compared with an old
one's. Nearest ones come from Atlas Vector Search in prod and are compared here in dev and tests.
"""

import hashlib
import math
from collections.abc import Mapping

from pymongo import UpdateOne

from app.core.config import get_settings
from app.core.constants import EmbeddingKind, EmbedPurpose, UsageFeature, VectorSearch
from app.core.time import utcnow
from app.db import mongo
from app.integrations.embeddings import get_embedder
from app.usage import service as usage

#: how much of a text is embedded; the start of a long page says what it's about
MAX_CHARS = 8000
#: Atlas looks at this many candidates per result it returns, the accuracy it recommends
CANDIDATES_PER_RESULT = 20


def fingerprint(text: str) -> str:
    """Changes whenever the text does, so only changed texts are embedded again."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def enabled() -> bool:
    return get_embedder() is not None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norms = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norms if norms else 0.0


async def refresh(
    workspace_id: str, user_id: str, kind: EmbeddingKind, texts: Mapping[str, str],
) -> dict[str, list[float]]:
    """Vectors for `texts` (by ref id), embedding only the new and changed ones; empty when embeddings are off."""
    embedder = get_embedder()
    if embedder is None or not texts:
        return {}
    query = {"workspace_id": workspace_id, "kind": kind, "ref_id": {"$in": list(texts)}, "model": embedder.model}
    stored = {doc["ref_id"]: doc async for doc in mongo.embeddings().find(query)}
    current = {ref_id: fingerprint(text) for ref_id, text in texts.items()}
    stale = [ref_id for ref_id in texts if stored.get(ref_id, {}).get("fingerprint") != current[ref_id]]
    if stale:
        vectors = await embedder.embed(
            [texts[ref_id][:MAX_CHARS] for ref_id in stale], EmbedPurpose.DOCUMENT,
            on_usage=usage.recorder(workspace_id, user_id, UsageFeature.EMBEDDINGS),
        )
        now = utcnow()
        await mongo.embeddings().bulk_write([
            UpdateOne(
                {"workspace_id": workspace_id, "kind": kind, "ref_id": ref_id},
                {"$set": {"model": embedder.model, "fingerprint": current[ref_id], "vector": vector, "updated_at": now}},
                upsert=True,
            )
            for ref_id, vector in zip(stale, vectors, strict=True)
        ], ordered=False)
        stored |= {ref_id: {"vector": vector} for ref_id, vector in zip(stale, vectors, strict=True)}
    return {ref_id: stored[ref_id]["vector"] for ref_id in texts}


async def fingerprints(workspace_id: str, kind: EmbeddingKind, ref_ids: list[str]) -> dict[str, str]:
    """The text fingerprint each of `ref_ids` was last embedded from, by the current model."""
    embedder = get_embedder()
    if embedder is None:
        return {}
    query = {"workspace_id": workspace_id, "kind": kind, "ref_id": {"$in": ref_ids}, "model": embedder.model}
    return {doc["ref_id"]: doc["fingerprint"] async for doc in mongo.embeddings().find(query, {"ref_id": 1, "fingerprint": 1})}


async def embed_query(workspace_id: str, user_id: str, text: str) -> list[float] | None:
    """`text` embedded to search with, or None when embeddings are off."""
    embedder = get_embedder()
    if embedder is None:
        return None
    [vector] = await embedder.embed(
        [text[:MAX_CHARS]], EmbedPurpose.QUERY, on_usage=usage.recorder(workspace_id, user_id, UsageFeature.EMBEDDINGS),
    )
    return vector


async def nearest(
    workspace_id: str, vector: list[float], kinds: list[EmbeddingKind], limit: int,
) -> list[tuple[EmbeddingKind, str, float]]:
    """The `(kind, ref_id, score)` closest in meaning to `vector`, closest first; score is cosine similarity."""
    embedder = get_embedder()
    if embedder is None:
        return []
    scope = {"workspace_id": workspace_id, "kind": {"$in": list(kinds)}, "model": embedder.model}
    if get_settings().vector_search == VectorSearch.ATLAS:
        pipeline = [
            {"$vectorSearch": {
                "index": mongo.VECTOR_INDEX, "path": "vector", "queryVector": vector, "filter": scope,
                "numCandidates": limit * CANDIDATES_PER_RESULT, "limit": limit,
            }},
            {"$project": {"kind": 1, "ref_id": 1, "score": {"$meta": "vectorSearchScore"}}},
        ]
        # Atlas scores cosine as (1 + cosine) / 2, so 0..1; turned back so thresholds mean the same everywhere
        return [(doc["kind"], doc["ref_id"], doc["score"] * 2 - 1) async for doc in mongo.embeddings().aggregate(pipeline)]
    scored = [
        (doc["kind"], doc["ref_id"], cosine(vector, doc["vector"]))
        async for doc in mongo.embeddings().find(scope, {"kind": 1, "ref_id": 1, "vector": 1})
    ]
    return sorted(scored, key=lambda row: -row[2])[:limit]
