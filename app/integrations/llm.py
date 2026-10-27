"""The language model behind the AI chat, behind one interface.

Any OpenAI-compatible chat API works (Groq, OpenRouter, DeepSeek, a local
server): `MODEL_BASE_URL`, `MODEL_NAME` and `MODEL_API_KEY` pick it, so
switching provider is configuration, not code.
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
import structlog

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("app.llm")

REQUEST_TIMEOUT_SECONDS = 15
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
#: Pauses before each retry of a transient error: three tries in all.
RETRY_BACKOFF_SECONDS = (1.0, 2.0)
#: Below this much time left, a retry isn't started.
MIN_ATTEMPT_SECONDS = 3.0

# Some models (DeepSeek's DSML, others' `<tool_call>`) sometimes write tool calls into the
# reply as markup instead of sending them as calls; this is how that markup is recognised.
_CALL_MARKUP_RE = re.compile(r"<[^<>\n]{0,12}(?:DSML|tool_call|function_calls)", re.IGNORECASE)
_CALL_TAG_RE = re.compile(r"</?[^<>\n]{0,16}?(?:DSML|tool_call|function_calls|invoke|parameter)\b[^<>]*>", re.IGNORECASE)
_INVOKE_RE = re.compile(r"<[^<>]*?invoke\s+name=\"([^\"]+)\"[^<>]*>(.*?)</[^<>]*?invoke\s*>", re.DOTALL)
_PARAMETER_RE = re.compile(
    r"<[^<>]*?parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?[^<>]*>(.*?)</[^<>]*?parameter\s*>", re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Turn:
    """One earlier message of a conversation, sent before the prompt."""

    role: Literal["user", "model"]
    text: str


@dataclass(frozen=True, slots=True)
class Delta:
    """A piece of a streamed reply: the model's reasoning, or the reply itself."""

    kind: Literal["thought", "text"]
    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool the model asked to run; `arguments` is the JSON it wrote, unchecked."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class CallUsage:
    """What one model call used, as the provider reported it; cost only where it reports one."""

    model: str
    provider: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class ToolRound:
    """One round of looking things up: the calls the model made and what each returned, in order."""

    calls: tuple[ToolCall, ...]
    results: tuple[str, ...]
    #: the reasoning that led to the calls, sent back so the model carries on from it
    thought: str = ""


class LLMClient(Protocol):
    """Injectable interface so services and tests never touch a provider directly."""

    def stream_text(
        self,
        system_instruction: str,
        prompt: str,
        *,
        history: Sequence[Turn] = (),
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        tools: Sequence[dict] = (),
        rounds: Sequence[ToolRound] = (),
        tool_choice: Literal["auto", "none"] = "auto",
        closing: str = "",
        session: str = "",
        on_usage: Callable[[CallUsage], Awaitable[None]] | None = None,
    ) -> AsyncIterator[Delta | ToolCall]:
        """The reply as it's written; with `tools`, the calls the model makes come once it's done.

        `closing` is a last instruction after the rounds, such as "answer now"; `session`
        groups calls that share a prefix (a conversation), for providers that route by it.
        `on_usage` gets what the call used once the provider reports it, to record it.
        """
        ...


class OpenAICompatibleClient:
    """Chat completions over HTTP: retries transient errors inside a hard timeout, request logging."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        reasoning_effort: str = "",
        provider: dict | None = None,
        session_routing: bool = False,
    ):
        self._model = model
        self._reasoning_effort = reasoning_effort
        #: OpenRouter's `provider` routing preferences; None sends nothing, for any other API
        self._provider = provider
        self._session_routing = session_routing
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"},
        )

    async def stream_text(
        self,
        system_instruction: str,
        prompt: str,
        *,
        history: Sequence[Turn] = (),
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        tools: Sequence[dict] = (),
        rounds: Sequence[ToolRound] = (),
        tool_choice: Literal["auto", "none"] = "auto",
        closing: str = "",
        session: str = "",
        on_usage: Callable[[CallUsage], Awaitable[None]] | None = None,
    ) -> AsyncIterator[Delta | ToolCall]:
        body = self._body(system_instruction, prompt, history, rounds, closing) | {
            "stream": True, "stream_options": {"include_usage": True},
        }
        if tools:
            body |= {"tools": list(tools), "tool_choice": tool_choice}
        if self._session_routing and session:
            body["session_id"] = session
        deadline = time.monotonic() + timeout
        # only the request is retried; once text has been handed on, a failure ends the reply
        response = await self._post(body, deadline)
        # a call's id and name come first and its arguments in pieces after, keyed by position
        calls: dict[int, list[str]] = {}
        try:
            async with asyncio.timeout_at(_loop_time(deadline)):
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    usage = self._usage(chunk)
                    if usage and on_usage:
                        await on_usage(usage)
                    for delta in _deltas(chunk):
                        yield delta
                    _collect_calls(chunk, calls)
        except TimeoutError as exc:
            logger.warning("llm_stream_timeout", model=self._model)
            raise _unavailable() from exc
        except httpx.HTTPError as exc:
            logger.warning("llm_stream_error", model=self._model, error=type(exc).__name__)
            raise _unavailable() from exc
        finally:
            await response.aclose()
        for index in sorted(calls):
            call_id, name, arguments = calls[index]
            if name:
                yield ToolCall(call_id or f"call_{index}", name, arguments)

    def _usage(self, chunk: dict) -> CallUsage | None:
        """Tokens and, where the provider reports it, cost of the call: logged, and returned to record."""
        usage = chunk.get("usage") if isinstance(chunk, dict) else None
        if not isinstance(usage, dict):
            return None
        cache = usage.get("prompt_tokens_details") or {}
        provider = chunk.get("provider") if isinstance(chunk.get("provider"), str) else None
        logger.info(
            "llm_usage", model=self._model, prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"), cost=usage.get("cost"),
            cached_tokens=cache.get("cached_tokens"), cache_write_tokens=cache.get("cache_write_tokens"),
            provider=provider,
        )
        cost = usage.get("cost")
        return CallUsage(
            model=self._model, provider=provider,
            prompt_tokens=usage.get("prompt_tokens") or 0, completion_tokens=usage.get("completion_tokens") or 0,
            cached_tokens=cache.get("cached_tokens") or 0,
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )

    def _body(
        self,
        system_instruction: str,
        prompt: str,
        history: Sequence[Turn],
        rounds: Sequence[ToolRound] = (),
        closing: str = "",
    ) -> dict:
        messages = [{"role": "system", "content": system_instruction}]
        messages += [{"role": "assistant" if t.role == "model" else "user", "content": t.text} for t in history]
        messages.append({"role": "user", "content": prompt})
        for round_ in rounds:
            asked = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                    for call in round_.calls
                ],
            }
            if round_.thought:
                asked["reasoning"] = round_.thought
            messages.append(asked)
            messages += [
                {"role": "tool", "tool_call_id": call.id, "content": result}
                for call, result in zip(round_.calls, round_.results, strict=True)
            ]
        if closing:
            messages.append({"role": "user", "content": closing})
        body = {"model": self._model, "messages": messages}
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        if self._provider:
            body["provider"] = self._provider
        return body

    async def _post(self, body: dict, deadline: float) -> httpx.Response:
        """Send the request, retrying transient failures while time allows; the response is left open to stream."""
        last_status: int | None = None
        last_error: Exception | None = None
        for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
            start = time.perf_counter()
            try:
                request = self._http.build_request(
                    "POST", "/chat/completions", json=body, timeout=max(deadline - time.monotonic(), 0.1),
                )
                response = await self._http.send(request, stream=True)
            except httpx.TimeoutException as exc:
                logger.warning("llm_call_timeout", attempt=attempt, model=self._model)
                raise _unavailable() from exc  # the time is spent; another try can't finish either
            except httpx.HTTPError as exc:
                last_status, last_error = None, exc
                logger.warning("llm_call_error", attempt=attempt, model=self._model, error=type(exc).__name__)
            else:
                if response.status_code < 400:
                    logger.info(
                        "llm_call", attempt=attempt, status="ok", model=self._model,
                        latency_ms=round((time.perf_counter() - start) * 1000, 1),
                    )
                    return response
                last_status = response.status_code
                await response.aclose()
                logger.warning("llm_call_error", attempt=attempt, model=self._model, status=last_status)
                if last_status not in TRANSIENT_STATUS_CODES:
                    break
            # only worth retrying if a try still fits after the pause
            if backoff is None or deadline - time.monotonic() < backoff + MIN_ATTEMPT_SECONDS:
                break
            await asyncio.sleep(backoff)

        if last_status == 429:
            raise ExternalServiceError(
                "The AI quota is used up for now", error_code="llm_quota_exhausted", status_code=503,
            )
        if last_status in TRANSIENT_STATUS_CODES or last_error is not None:
            raise _unavailable() from last_error
        raise ExternalServiceError("The AI request failed", error_code="llm_failed")


def _deltas(chunk: dict) -> list[Delta]:
    """The reasoning and reply text in one streamed chunk. Providers name the reasoning differently."""
    delta = _delta_of(chunk)
    found = []
    thought = delta.get("reasoning") or delta.get("reasoning_content")
    if isinstance(thought, str) and thought:
        found.append(Delta("thought", thought))
    if isinstance(delta.get("content"), str) and delta["content"]:
        found.append(Delta("text", delta["content"]))
    return found


def _delta_of(chunk: dict) -> dict:
    try:
        return chunk["choices"][0].get("delta") or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        return {}


def _collect_calls(chunk: dict, calls: dict[int, list[str]]) -> None:
    """Add a chunk's pieces of tool calls to `calls`: index -> [id, name, arguments so far]."""
    pieces = _delta_of(chunk).get("tool_calls")
    if not isinstance(pieces, list):
        return
    for piece in pieces:
        if not isinstance(piece, dict):
            continue
        index = piece.get("index")
        call = calls.setdefault(index if isinstance(index, int) else len(calls), ["", "", ""])
        function = piece.get("function") or {}
        if isinstance(piece.get("id"), str):
            call[0] = piece["id"]
        if isinstance(function.get("name"), str):
            call[1] += function["name"]
        if isinstance(function.get("arguments"), str):
            call[2] += function["arguments"]


def calls_written_as_text(text: str) -> tuple[list[ToolCall], str]:
    """Tool calls a model wrote into its reply as markup, and the reply without that markup.

    Each `invoke` becomes a call; a parameter marked `string="false"` is JSON, any
    other is text. No markup gives no calls and the text unchanged.
    """
    if not _CALL_MARKUP_RE.search(text):
        return [], text
    calls = []
    for index, (name, body) in enumerate(_INVOKE_RE.findall(text)):
        arguments = {}
        for key, is_string, value in _PARAMETER_RE.findall(body):
            value = value.strip()
            if is_string == "false":
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            arguments[key] = value
        calls.append(ToolCall(f"written-{index}", name.strip(), json.dumps(arguments)))
    # the markup runs from its first tag to the end of its last one
    tags = list(_CALL_TAG_RE.finditer(text)) or list(_CALL_MARKUP_RE.finditer(text))
    return calls, (text[:tags[0].start()] + text[tags[-1].end():]).strip()


def _unavailable() -> ExternalServiceError:
    return ExternalServiceError(
        "The AI service is busy right now, try again in a moment", error_code="llm_unavailable", status_code=503,
    )


def _loop_time(deadline: float) -> float:
    """A `time.monotonic()` deadline on the event loop's clock."""
    loop = asyncio.get_running_loop()
    return loop.time() + (deadline - time.monotonic())


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = OpenAICompatibleClient(
            base_url=settings.model_base_url, model=settings.model_name, api_key=settings.model_api_key,
            reasoning_effort=settings.model_reasoning_effort,
            provider=provider_routing(settings.model_provider_sort, settings.model_provider_order),
            session_routing=settings.model_session_routing,
        )
    return _client


def provider_routing(sort: str, order: str) -> dict | None:
    """OpenRouter's `provider` preferences from settings; fallbacks stay on, so a provider being down isn't an outage."""
    routing: dict = {}
    if sort.strip():
        routing["sort"] = sort.strip()
    if slugs := [slug.strip() for slug in order.split(",") if slug.strip()]:
        routing["order"] = slugs
    return routing | {"allow_fallbacks": True} if routing else None


def use_llm_client(client: LLMClient | None) -> None:
    global _client
    _client = client
