"""One model call that answers in JSON, as the decision features use the AI: no tools, usage recorded."""

import json

import structlog
from pydantic import BaseModel, ValidationError

from app.core.constants import UsageFeature
from app.core.exceptions import ExternalServiceError
from app.integrations.llm import Delta, get_llm_client
from app.usage import service as usage

logger = structlog.get_logger("app.decisions")

TIMEOUT_SECONDS = 60
#: how much of a source the model reads; longer ones are cut here
MAX_SOURCE_CHARS = 12_000


def fenced(text: str, *, source_id: str = "", limit: int = MAX_SOURCE_CHARS) -> str:
    """A source's text marked off as data for the model to read, never instructions; `source_id` names it."""
    opening = f'<source id="{source_id}">' if source_id else "<source>"
    return f"{opening}\n{text[:limit]}\n</source>"


async def ask_json[T: BaseModel](
    reply_model: type[T], system: str, prompt: str, *, workspace_id: str, user_id: str, feature: UsageFeature,
    timeout: float = TIMEOUT_SECONDS,
) -> T:
    """The model's reply read as `reply_model`; it may come wrapped in a code fence."""
    parts = []
    stream = get_llm_client().stream_text(
        system, prompt, timeout=timeout, on_usage=usage.recorder(workspace_id, user_id, feature),
    )
    async for item in stream:
        if isinstance(item, Delta) and item.kind == "text":
            parts.append(item.text)
    text = "".join(parts)
    start, end = text.find("{"), text.rfind("}")
    try:
        return reply_model.model_validate(json.loads(text[start:end + 1]))
    except (ValueError, ValidationError) as exc:
        logger.warning("decision_ai_unreadable", feature=feature, error=type(exc).__name__, reply=text[:200])
        raise ExternalServiceError("The AI's reply couldn't be read; try again", error_code="llm_bad_reply") from exc
