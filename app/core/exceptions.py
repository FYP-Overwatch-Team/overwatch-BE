from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import structlog

from app.core.responses import error

logger = structlog.get_logger("app.errors")

VALIDATION_MESSAGE = "Request validation failed"


class AppError(Exception):
    """Base for domain errors that map to a clean {error_code, message} response."""

    status_code = 400
    error_code = "bad_request"

    def __init__(
        self, message: str = "", *, error_code: str | None = None, status_code: int | None = None,
        meta: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message or self.error_code
        # extra `meta` for the client: field errors, a challenge id, a retry delay
        self.meta = meta or {}
        if error_code:
            self.error_code = error_code
        if status_code:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    error_code = "forbidden"


class ExternalServiceError(AppError):
    status_code = 502
    error_code = "external_service_error"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"


class FieldValidationError(AppError):
    """Input that passed the schema but not a rule: messages keyed by the form field they belong to."""

    status_code = 422
    error_code = "validation_error"

    def __init__(self, fields: dict[str, str], message: str = VALIDATION_MESSAGE):
        super().__init__(message, meta={"fields": fields})


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    """The first message per field, keyed by the last part of its location ("body.password" -> "password")."""
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = [part for part in err.get("loc", ()) if isinstance(part, str)]
        if len(loc) > 1:
            fields.setdefault(loc[-1], str(err.get("msg", "")).removeprefix("Value error, "))
    return fields


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return error(exc.status_code, exc.error_code, exc.message, exc.meta)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        fields = _field_errors(exc)
        return error(422, "validation_error", VALIDATION_MESSAGE, {"fields": fields} if fields else None)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        # Log the traceback server-side; the client only ever sees the generic shape.
        logger.exception("unhandled_error", path=request.url.path)
        return error(500, "internal_error", "An internal error occurred")
