import logging
import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Keys whose values must never appear in logs, and patterns that look like secrets.
SENSITIVE_KEYS = {
    "access_token", "refresh_token", "token", "authorization", "client_secret",
    "webhook_secret", "password", "api_key", "secret", "cookie", "set-cookie",
}
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")


def redact(value):
    """Recursively redact sensitive keys in dicts/lists and bearer tokens in strings."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower() in SENSITIVE_KEYS else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _BEARER_RE.sub(r"\1[REDACTED]", value)
    return value


def _redact_event(logger, method_name, event_dict):
    return redact(event_dict)


def configure_logging(json_logs: bool = True) -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_event,
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id to the structlog context and log request timing."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id", uuid.uuid4().hex)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["x-request-id"] = request_id
        structlog.get_logger("app.request").info(
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response
