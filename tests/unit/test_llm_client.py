"""The OpenAI-compatible model client, against a mocked provider."""

import json

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from app.core.exceptions import ExternalServiceError
from app.integrations import llm
from app.integrations.llm import (
    CallUsage,
    Delta,
    OpenAICompatibleClient,
    ToolCall,
    ToolRound,
    Turn,
    calls_written_as_text,
    provider_routing,
)

BASE = "https://models.example/v1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(llm, "MIN_ATTEMPT_SECONDS", 0.0)
    return OpenAICompatibleClient(base_url=BASE, model="some/model", api_key="key-123", reasoning_effort="low")


def sse(*chunks: dict) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def delta(**fields) -> dict:
    return {"choices": [{"delta": fields}]}


@respx.mock
async def test_a_stream_yields_reasoning_then_text_whatever_the_provider_calls_it(client):
    route = respx.post(f"{BASE}/chat/completions").respond(
        200, text=sse(delta(reasoning="Look"), delta(reasoning_content="ing."), delta(content="Hi"), delta(content=" there")),
        headers={"content-type": "text/event-stream"},
    )

    got = [d async for d in client.stream_text("sys", "q", history=[Turn("user", "a"), Turn("model", "b")])]

    assert got == [Delta("thought", "Look"), Delta("thought", "ing."), Delta("text", "Hi"), Delta("text", " there")]
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True
    assert sent["reasoning_effort"] == "low"
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "user"]
    assert route.calls[0].request.headers["authorization"] == "Bearer key-123"


@respx.mock
async def test_a_transient_error_is_retried(client):
    respx.post(f"{BASE}/chat/completions").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, text=sse(delta(content="ok"))),
    ])

    assert [d.text async for d in client.stream_text("sys", "q")] == ["ok"]


@respx.mock
async def test_a_spent_quota_is_its_own_error(client):
    respx.post(f"{BASE}/chat/completions").respond(429)

    with pytest.raises(ExternalServiceError) as caught:
        [d async for d in client.stream_text("sys", "q")]

    assert caught.value.error_code == "llm_quota_exhausted"


@respx.mock
async def test_a_rejected_request_isnt_retried(client):
    route = respx.post(f"{BASE}/chat/completions").respond(400)

    with pytest.raises(ExternalServiceError) as caught:
        [d async for d in client.stream_text("sys", "q")]

    assert caught.value.error_code == "llm_failed"
    assert route.call_count == 1


@respx.mock
async def test_tool_calls_written_in_pieces_come_out_whole_after_the_text(client):
    def call(index, **fields):
        return {"choices": [{"delta": {"tool_calls": [{"index": index, **fields}]}}]}

    route = respx.post(f"{BASE}/chat/completions").respond(
        200,
        text=sse(
            delta(content="Let me look."),
            call(0, id="c1", function={"name": "read_code", "arguments": '{"ref":'}),
            call(1, id="c2", function={"name": "search_graph", "arguments": '{"query": "signup"}'}),
            call(0, function={"arguments": ' "n4"}'}),
            {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40, "cost": 0.0002}},
        ),
        headers={"content-type": "text/event-stream"},
    )

    got = [d async for d in client.stream_text("sys", "q", tools=[{"type": "function"}], tool_choice="auto")]

    assert got == [
        Delta("text", "Let me look."),
        ToolCall("c1", "read_code", '{"ref": "n4"}'),
        ToolCall("c2", "search_graph", '{"query": "signup"}'),
    ]
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"] == [{"type": "function"}] and sent["tool_choice"] == "auto"
    assert sent["stream_options"] == {"include_usage": True}


@respx.mock
async def test_earlier_rounds_are_sent_as_calls_and_their_results(client):
    route = respx.post(f"{BASE}/chat/completions").respond(200, text=sse(delta(content="ok")))
    asked = ToolCall("c1", "read_code", '{"ref": "n4"}')

    [d async for d in client.stream_text("sys", "q", rounds=[ToolRound((asked,), ("Source s9 …",), "Need n4.")])]

    messages = json.loads(route.calls[0].request.content)["messages"]
    assert messages[-2] == {
        "role": "assistant", "content": None, "reasoning": "Need n4.",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_code", "arguments": '{"ref": "n4"}'}}],
    }
    assert messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": "Source s9 …"}
    assert "tools" not in json.loads(route.calls[0].request.content)


#: As a model wrote it into a reply, instead of making the calls
WRITTEN_CALLS = (
    '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="grep_code">\n'
    '<｜｜DSML｜｜ parameter name="query" string="true">socket.emit</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n'
    '<｜｜DSML｜｜ invoke name="read_code">\n<｜｜DSML｜｜ parameter name="start_line" string="false">4</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'
)


def test_calls_written_into_the_reply_are_read_as_calls_and_taken_out_of_it():
    calls, rest = calls_written_as_text(f"Let me check.\n{WRITTEN_CALLS}\n")

    assert calls == [
        ToolCall("written-0", "grep_code", '{"query": "socket.emit"}'),
        ToolCall("written-1", "read_code", '{"start_line": 4}'),
    ]
    assert rest == "Let me check."


def test_a_reply_without_call_markup_is_left_alone():
    assert calls_written_as_text("ANSWERABLE: yes\n---\nIt renders <Button /> twice.") == (
        [], "ANSWERABLE: yes\n---\nIt renders <Button /> twice.",
    )


@respx.mock
async def test_routing_fields_are_sent_only_when_set():
    route = respx.post(f"{BASE}/chat/completions").respond(200, text=sse(delta(content="ok")))
    plain = OpenAICompatibleClient(base_url=BASE, model="m", api_key="k")
    routed = OpenAICompatibleClient(
        base_url=BASE, model="m", api_key="k", provider=provider_routing("latency", ""), session_routing=True,
    )

    [d async for d in plain.stream_text("sys", "q", session="conv-1")]
    [d async for d in routed.stream_text("sys", "q", session="conv-1")]

    first, second = (json.loads(call.request.content) for call in route.calls)
    assert "provider" not in first and "session_id" not in first
    assert second["provider"] == {"sort": "latency", "allow_fallbacks": True}
    assert second["session_id"] == "conv-1"


def test_provider_routing_reads_the_settings():
    assert provider_routing("", "") is None
    assert provider_routing("", " deepinfra, novita ") == {"order": ["deepinfra", "novita"], "allow_fallbacks": True}


@respx.mock
async def test_each_round_only_adds_to_the_messages_so_the_start_stays_cached(client):
    route = respx.post(f"{BASE}/chat/completions").respond(200, text=sse(delta(content="ok")))
    asked = ToolCall("c1", "read_code", "{}")

    [d async for d in client.stream_text("sys", "q", history=[Turn("user", "a")], tools=[{"type": "function"}])]
    [d async for d in client.stream_text(
        "sys", "q", history=[Turn("user", "a")], tools=[{"type": "function"}],
        rounds=[ToolRound((asked,), ("r",))], tool_choice="none", closing="answer now",
    )]

    first, second = (json.loads(call.request.content)["messages"] for call in route.calls)
    assert second[:len(first)] == first


@respx.mock
async def test_cache_use_is_logged_with_the_cost(client):
    respx.post(f"{BASE}/chat/completions").respond(200, text=sse(
        delta(content="ok"),
        {"choices": [], "provider": "DeepInfra", "usage": {
            "prompt_tokens": 1000, "completion_tokens": 10, "cost": 0.0001,
            "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
        }},
    ))

    with capture_logs() as logs:
        [d async for d in client.stream_text("sys", "q")]

    usage = next(log for log in logs if log["event"] == "llm_usage")
    assert (usage["cached_tokens"], usage["cache_write_tokens"], usage["provider"]) == (800, 0, "DeepInfra")


@respx.mock
async def test_what_a_call_used_is_handed_to_on_usage(client):
    respx.post(f"{BASE}/chat/completions").respond(200, text=sse(
        delta(content="ok"),
        {"choices": [], "provider": "DeepInfra", "usage": {
            "prompt_tokens": 1000, "completion_tokens": 10, "cost": 0.0001,
            "prompt_tokens_details": {"cached_tokens": 800},
        }},
    ))
    used: list[CallUsage] = []

    async def record(usage: CallUsage) -> None:
        used.append(usage)

    [d async for d in client.stream_text("sys", "q", on_usage=record)]

    assert used == [CallUsage("some/model", "DeepInfra", 1000, 10, 800, 0.0001)]


OPENROUTER = "https://openrouter.ai/api/v1"


@pytest.fixture
def openrouter(monkeypatch):
    monkeypatch.setattr(llm, "USAGE_LOOKUP_DELAYS_SECONDS", (0.0, 0.0, 0.0))
    return OpenAICompatibleClient(base_url=OPENROUTER, model="some/model", api_key="key-123")


def generation(**fields) -> dict:
    return {"id": "gen-abc123", "choices": [{"delta": fields}]}


@respx.mock
async def test_a_call_stopped_early_is_recorded_as_openrouter_billed_it(openrouter):
    respx.post(f"{OPENROUTER}/chat/completions").respond(200, text=sse(
        generation(content="The answer"), generation(content=" goes on"),
        {"id": "gen-abc123", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    ))
    billed = respx.get(f"{OPENROUTER}/generation", params={"id": "gen-abc123"}).mock(side_effect=[
        httpx.Response(404),
        httpx.Response(200, json={"data": {
            "native_tokens_prompt": 1200, "native_tokens_completion": 7, "native_tokens_cached": 1000,
            "total_cost": 0.00042, "provider_name": "DeepInfra",
        }}),
    ])
    used: list[CallUsage] = []

    async def record(usage: CallUsage) -> None:
        used.append(usage)

    stream = openrouter.stream_text("sys", "q", on_usage=record)
    await anext(stream)
    await stream.aclose()  # refused or cancelled after the first piece, as the chat closes it
    await llm.settle_usage()

    assert billed.call_count == 2
    assert used == [CallUsage("some/model", "DeepInfra", 1200, 7, 1000, 0.00042)]


@respx.mock
async def test_a_call_that_reports_its_usage_isnt_looked_up(openrouter):
    respx.post(f"{OPENROUTER}/chat/completions").respond(200, text=sse(
        generation(content="ok"), {"id": "gen-abc123", "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001}},
    ))
    lookup = respx.get(f"{OPENROUTER}/generation")
    used: list[CallUsage] = []

    async def record(usage: CallUsage) -> None:
        used.append(usage)

    [d async for d in openrouter.stream_text("sys", "q", on_usage=record)]
    await llm.settle_usage()

    assert not lookup.called
    assert [(u.prompt_tokens, u.cost_usd) for u in used] == [(10, 0.001)]


@respx.mock
async def test_a_stopped_call_that_cant_be_looked_up_is_logged_not_guessed(client):
    respx.post(f"{BASE}/chat/completions").respond(200, text=sse(generation(content="a"), generation(content="b")))
    used: list[CallUsage] = []

    async def record(usage: CallUsage) -> None:
        used.append(usage)

    with capture_logs() as logs:
        stream = client.stream_text("sys", "q", on_usage=record)
        await anext(stream)
        await stream.aclose()
        await llm.settle_usage()

    assert used == []
    assert any(log["event"] == "llm_usage_unrecorded" and log["reason"] == "no_lookup" for log in logs)
