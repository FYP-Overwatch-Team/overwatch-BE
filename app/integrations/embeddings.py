"""Embedding models behind one interface, so another model or API is an adapter, not a rewrite.

An adapter turns texts into vectors for one API shape. `EMBEDDING_ADAPTER` picks it and
`EMBEDDING_MODEL` the model; vectors are stored with the model's name, so changing it means
embedding again, never comparing vectors from two models. Today's adapters take text; one for a
model that embeds images or audio would add those inputs to its own `embed`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import httpx
import structlog

from app.core.config import get_settings
from app.core.constants import EmbedPurpose
from app.core.exceptions import ExternalServiceError
from app.integrations.llm import RETRY_BACKOFF_SECONDS, TRANSIENT_STATUS_CODES, CallUsage, provider_routing

logger = structlog.get_logger("app.embeddings")

TIMEOUT_SECONDS = 60
#: texts sent in one request
BATCH_SIZE = 64


class Embedder(Protocol):
    """Injectable interface so services and tests never touch a provider directly."""

    #: stored with each vector; vectors are only compared with ones from the same model
    model: str

    async def embed(
        self, texts: Sequence[str], purpose: EmbedPurpose, *, on_usage: Callable[[CallUsage], Awaitable[None]] | None = None,
    ) -> list[list[float]]:
        """One vector per text, in order. `on_usage` gets what each request used, to record it."""
        ...


class OpenAICompatibleEmbedder:
    """`POST /embeddings` as OpenAI defines it, which OpenRouter and most providers serve."""

    def __init__(
        self, *, base_url: str, api_key: str, model: str, dimensions: int, query_instruction: str = "",
        provider: dict | None = None,
    ):
        self.model = model
        self._dimensions = dimensions
        #: some models (Qwen3) are trained to read what a search text is for before it
        self._query_instruction = query_instruction
        self._provider = provider
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=TIMEOUT_SECONDS,
        )

    async def embed(
        self, texts: Sequence[str], purpose: EmbedPurpose, *, on_usage: Callable[[CallUsage], Awaitable[None]] | None = None,
    ) -> list[list[float]]:
        prefix = self._query_instruction if purpose == EmbedPurpose.QUERY else ""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            body = {"model": self.model, "input": [prefix + text for text in texts[start:start + BATCH_SIZE]]}
            if self._dimensions:
                body["dimensions"] = self._dimensions
            if self._provider:
                body["provider"] = self._provider
            data = await self._post(body)
            vectors += [row["embedding"] for row in sorted(data["data"], key=lambda row: row["index"])]
            usage = data.get("usage") or {}
            cost = usage.get("cost")
            if on_usage:
                await on_usage(CallUsage(
                    model=self.model, provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
                    prompt_tokens=usage.get("prompt_tokens") or 0, completion_tokens=0, cached_tokens=0,
                    cost_usd=float(cost) if isinstance(cost, int | float) else None,
                ))
        return vectors

    async def _post(self, body: dict) -> dict:
        """The response, retrying transient failures."""
        status: int | None = None
        for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
            try:
                response = await self._http.post("/embeddings", json=body)
            except httpx.HTTPError as exc:
                status = None
                logger.warning("embedding_call_error", attempt=attempt, model=self.model, error=type(exc).__name__)
            else:
                data = response.json() if response.status_code < 400 else None
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    return data
                status = response.status_code
                logger.warning("embedding_call_error", attempt=attempt, model=self.model, status=status)
                # a 200 with an error body instead of vectors is a provider failing upstream: worth another try
                if status >= 400 and status not in TRANSIENT_STATUS_CODES:
                    break
            if backoff is None:
                break
            await asyncio.sleep(backoff)
        raise ExternalServiceError(
            "The embedding service is unavailable right now", error_code="embedding_unavailable", status_code=503,
        )


#: adapters by `EMBEDDING_ADAPTER`; a new API shape is one more entry
ADAPTERS = {"openai_compatible": OpenAICompatibleEmbedder}

_embedder: Embedder | None = None


def get_embedder() -> Embedder | None:
    """The configured embedder, or None when embeddings are off (no model set)."""
    global _embedder
    settings = get_settings()
    if _embedder is None and settings.embedding_model:
        _embedder = ADAPTERS[settings.embedding_adapter](
            base_url=settings.model_base_url, api_key=settings.model_api_key, model=settings.embedding_model,
            dimensions=settings.embedding_dimensions, query_instruction=settings.embedding_query_instruction,
            provider=provider_routing("", settings.embedding_provider_order),
        )
    return _embedder


def use_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder
