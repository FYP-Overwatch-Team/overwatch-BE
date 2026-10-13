import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("app.gemini")

MODEL_NAME = "gemini-3.5-flash"
REQUEST_TIMEOUT_SECONDS = 15
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
#: Pauses before each retry of a transient error: three tries in all.
RETRY_BACKOFF_SECONDS = (1.0, 2.0)
#: Below this much time left, a retry isn't started.
MIN_ATTEMPT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class Turn:
    """One earlier message of a conversation, sent before the prompt."""

    role: Literal["user", "model"]
    text: str


class GeminiClient(Protocol):
    """Injectable interface so services/tests never touch the SDK directly."""

    async def generate_text(self, system_instruction: str, prompt: str) -> str: ...

    async def generate_json(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: dict,
        *,
        history: Sequence[Turn] = (),
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        think_briefly: bool = False,
    ) -> str: ...


class GoogleGeminiClient:
    """Thin wrapper: retries transient errors with backoff inside a hard timeout, request logging."""

    def __init__(self, api_key: str | None = None):
        self._client = genai.Client(api_key=api_key or get_settings().gemini_api_key)

    async def generate_text(self, system_instruction: str, prompt: str) -> str:
        return await self._call(system_instruction, prompt)

    async def generate_json(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: dict,
        *,
        history: Sequence[Turn] = (),
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        think_briefly: bool = False,
    ) -> str:
        return await self._call(
            system_instruction, prompt, response_schema=response_schema,
            history=history, timeout=timeout, think_briefly=think_briefly,
        )

    async def _call(
        self,
        system_instruction: str,
        prompt: str,
        *,
        response_schema: dict | None = None,
        history: Sequence[Turn] = (),
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        think_briefly: bool = False,
    ) -> str:
        config = genai_types.GenerateContentConfig(system_instruction=system_instruction)
        if response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema
        if think_briefly:
            config.thinking_config = genai_types.ThinkingConfig(thinking_level=genai_types.ThinkingLevel.LOW)
        contents = [
            genai_types.Content(role=turn.role, parts=[genai_types.Part(text=turn.text)])
            for turn in history
        ] + [genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])]

        # `timeout` bounds the whole call, retries included
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
            start = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=MODEL_NAME, contents=contents, config=config,
                    ),
                    timeout=deadline - time.monotonic(),
                )
                logger.info(
                    "gemini_call", attempt=attempt, status="ok",
                    latency_ms=round((time.perf_counter() - start) * 1000, 1),
                )
                return response.text
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning("gemini_call_timeout", attempt=attempt)
                break  # the time is spent; another try can't finish either
            except genai_errors.APIError as exc:
                last_error = exc
                status = getattr(exc, "code", None)
                logger.warning("gemini_call_error", attempt=attempt, status=status)
                # only worth retrying if it's transient and a try still fits after the pause
                if status not in TRANSIENT_STATUS_CODES or backoff is None:
                    break
                if deadline - time.monotonic() < backoff + MIN_ATTEMPT_SECONDS:
                    break
                await asyncio.sleep(backoff)

        status = getattr(last_error, "code", None)
        if status == 429:
            raise ExternalServiceError(
                "The AI quota is used up for now", error_code="llm_quota_exhausted", status_code=503,
            ) from last_error
        if status in TRANSIENT_STATUS_CODES or isinstance(last_error, asyncio.TimeoutError):
            raise ExternalServiceError(
                "The AI service is busy right now, try again in a moment",
                error_code="llm_unavailable", status_code=503,
            ) from last_error
        raise ExternalServiceError(
            "Gemini request failed", error_code="gemini_unavailable",
        ) from last_error


_client: GeminiClient | None = None


def get_gemini_client() -> GeminiClient:
    global _client
    if _client is None:
        _client = GoogleGeminiClient()
    return _client


def use_gemini_client(client: GeminiClient) -> None:
    global _client
    _client = client
