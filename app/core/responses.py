"""The one response shape every endpoint returns:

    {"status": "success" | "error", "code": <http status>, "data": ..., "meta": {...}}

Routes just return their data and declare its type (`-> RepoSummary`);
`EnvelopeRoute` wraps it on the way out, like a NestJS interceptor, and
documents the endpoint as `ApiResponse[RepoSummary]`. Errors get the same
shape from the exception handlers, with `error_code` and `message` in `meta`.
A route that returns a `Response` itself (an OAuth redirect) is left alone.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, Generic, Literal, TypeVar, get_args, get_origin

from fastapi import APIRouter, Response
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.utils import get_typed_return_annotation
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    status: Literal["success", "error"]
    code: int
    data: T | None = None
    meta: dict[str, Any] = {}


@dataclass(frozen=True)
class WithMeta(Generic[T]):
    """Return this from a route to add `meta` (pagination, counts) next to the data."""

    data: T
    meta: dict[str, Any]


class EnvelopeRoute(APIRoute):
    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        code = kwargs.get("status_code") or 200

        @wraps(endpoint)
        async def enveloped(*args: Any, **values: Any) -> Any:
            result = endpoint(*args, **values)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, Response):
                return result
            meta: dict[str, Any] = {}
            if isinstance(result, WithMeta):
                result, meta = result.data, result.meta
            return {"status": "success", "code": code, "data": result, "meta": meta}

        if isinstance(kwargs.get("response_model", DefaultPlaceholder(None)), DefaultPlaceholder):
            kwargs["response_model"] = _envelope_model(get_typed_return_annotation(endpoint))
        super().__init__(path, enveloped, **kwargs)


def api_router(**kwargs: Any) -> APIRouter:
    """An APIRouter whose routes return the envelope."""
    return APIRouter(route_class=EnvelopeRoute, **kwargs)


def error(code: int, error_code: str, message: str) -> JSONResponse:
    body = ApiResponse[None](status="error", code=code, meta={"error_code": error_code, "message": message})
    return JSONResponse(status_code=code, content=jsonable_encoder(body))


def _envelope_model(return_type: Any) -> Any:
    if inspect.isclass(return_type) and issubclass(return_type, Response):
        return None  # redirects and other raw responses aren't wrapped
    if get_origin(return_type) is WithMeta:
        return_type = get_args(return_type)[0]
    return ApiResponse[Any if return_type is inspect.Signature.empty else return_type]
