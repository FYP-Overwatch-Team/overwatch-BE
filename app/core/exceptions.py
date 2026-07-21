from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import structlog

logger = structlog.get_logger("app.errors")


class AppError(Exception):
    """Base for domain errors that map to a clean {error_code, message} response."""

    status_code = 400
    error_code = "bad_request"

    def __init__(self, message: str = "", *, error_code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message or self.error_code
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


def _error_response(status_code: int, error_code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error_code": error_code, "message": message})


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return _error_response(exc.status_code, exc.error_code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return _error_response(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return _error_response(422, "validation_error", "Request validation failed")

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        # Log the traceback server-side; the client only ever sees the generic shape.
        logger.exception("unhandled_error", path=request.url.path)
        return _error_response(500, "internal_error", "An internal error occurred")
