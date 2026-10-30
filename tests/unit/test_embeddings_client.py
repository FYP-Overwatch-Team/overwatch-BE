"""The OpenAI-compatible embedding adapter, against a mocked provider."""

import httpx
import pytest
import respx

from app.core.constants import EmbedPurpose
from app.core.exceptions import ExternalServiceError
from app.embeddings.service import cosine
from app.integrations import embeddings
from app.integrations.embeddings import OpenAICompatibleEmbedder

BASE = "https://models.example/v1"


@pytest.fixture
def embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    return OpenAICompatibleEmbedder(
        base_url=BASE, api_key="key-123", model="some/embedder", dimensions=3, query_instruction="Find: ",
        provider={"order": ["nebius"], "allow_fallbacks": True},
    )


def answer(*vectors: list[float], cost: float = 0.000002) -> dict:
    # rows may come back out of order; `index` says which text each is for
    rows = [{"index": n, "embedding": v} for n, v in reversed(list(enumerate(vectors)))]
    return {"data": rows, "provider": "Nebius", "usage": {"prompt_tokens": 12, "cost": cost}}


@respx.mock
async def test_texts_are_embedded_in_order_and_usage_is_reported(embedder):
    route = respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json=answer([1, 0, 0], [0, 1, 0])))
    used = []

    async def on_usage(usage):
        used.append(usage)

    vectors = await embedder.embed(["a", "b"], EmbedPurpose.DOCUMENT, on_usage=on_usage)

    assert vectors == [[1, 0, 0], [0, 1, 0]]
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer key-123"
    body = __import__("json").loads(sent.content)
    assert body == {"model": "some/embedder", "input": ["a", "b"], "dimensions": 3, "provider": {"order": ["nebius"], "allow_fallbacks": True}}
    [usage] = used
    assert (usage.model, usage.provider, usage.prompt_tokens, usage.completion_tokens, usage.cost_usd) == ("some/embedder", "Nebius", 12, 0, 0.000002)


@respx.mock
async def test_a_text_to_search_with_gets_the_models_instruction(embedder):
    route = respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(200, json=answer([1, 0, 0])))

    await embedder.embed(["guest export"], EmbedPurpose.QUERY)

    assert __import__("json").loads(route.calls[0].request.content)["input"] == ["Find: guest export"]


@respx.mock
async def test_busy_providers_are_retried_and_errors_raise(embedder):
    route = respx.post(f"{BASE}/embeddings").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json={"error": {"message": "overloaded"}}), httpx.Response(200, json=answer([1, 0, 0])),
    ])

    assert await embedder.embed(["a"], EmbedPurpose.DOCUMENT) == [[1, 0, 0]]
    assert route.call_count == 3

    respx.post(f"{BASE}/embeddings").mock(return_value=httpx.Response(401))
    with pytest.raises(ExternalServiceError):
        await embedder.embed(["a"], EmbedPurpose.DOCUMENT)


def test_cosine():
    assert cosine([1, 0], [1, 0]) == 1
    assert cosine([1, 0], [0, 1]) == 0
    assert cosine([0, 0], [1, 0]) == 0
