import asyncio
import time
from typing import Protocol

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


class GeminiClient(Protocol):
    """Injectable interface so services/tests never touch the SDK directly."""

    async def generate_text(self, system_instruction: str, prompt: str) -> str: ...

    async def generate_json(self, system_instruction: str, prompt: str, response_schema: dict) -> str: ...


class GoogleGeminiClient:
    """Thin wrapper: single retry on transient errors, hard timeout, request logging."""

    def __init__(self, api_key: str | None = None):
        self._client = genai.Client(api_key=api_key or get_settings().gemini_api_key)

    async def generate_text(self, system_instruction: str, prompt: str) -> str:
        return await self._call(system_instruction, prompt, response_schema=None)

    async def generate_json(self, system_instruction: str, prompt: str, response_schema: dict) -> str:
        return await self._call(system_instruction, prompt, response_schema=response_schema)

    async def _call(self, system_instruction: str, prompt: str, response_schema: dict | None) -> str:
        config = genai_types.GenerateContentConfig(system_instruction=system_instruction)
        if response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_schema = response_schema

        last_error: Exception | None = None
        for attempt in range(2):  # one retry on transient errors only
            start = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=MODEL_NAME, contents=prompt, config=config,
                    ),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                logger.info(
                    "gemini_call", attempt=attempt, status="ok",
                    latency_ms=round((time.perf_counter() - start) * 1000, 1),
                )
                return response.text
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning("gemini_call_timeout", attempt=attempt)
                break  # a hung request is not worth retrying inside the same call
            except genai_errors.APIError as exc:
                last_error = exc
                status = getattr(exc, "code", None)
                logger.warning("gemini_call_error", attempt=attempt, status=status)
                if status not in TRANSIENT_STATUS_CODES or attempt == 1:
                    break

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
