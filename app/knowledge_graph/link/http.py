"""Matching outbound requests to the routes that serve them.

This is what connects a frontend to its backend: `fetch("/api/users")` in one
file and `@router.get("/api/users")` in another become an edge.

Matching is **exact** on method and normalised path. Parameter syntax differs
per framework — `{id}`, `:id`, `[id]` — so both sides are normalised to the
same shape first. What is deliberately *not* done is guessing at mounted
prefixes: if a router is included under `/api` in code we do not read, the
call will not match, and it is reported as unmatched rather than attached to
a route it might not belong to.
"""

import re
from dataclasses import dataclass

#: `{id}` (FastAPI), `:id` (Express), `[id]` / `[...slug]` (Next.js).
_PARAMETER_RE = re.compile(r"\{[^/}]*\}|:[^/]+|\[[^/\]]*\]")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*")


@dataclass(frozen=True, slots=True)
class RouteKey:
    """Method plus normalised path: what both sides of a match agree on."""

    method: str
    path: str

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


def normalise_path(path: str) -> str:
    """`https://host/api/users/{id}/?x=1` -> `/api/users/*`."""
    without_host = _SCHEME_RE.sub("", path.strip())
    without_query = without_host.split("?", 1)[0].split("#", 1)[0]
    parameterised = _PARAMETER_RE.sub("*", without_query)

    normalised = re.sub(r"/{2,}", "/", parameterised).rstrip("/")
    if not normalised:
        return "/"
    return normalised if normalised.startswith("/") else f"/{normalised}"


def route_key(method: str, path: str) -> RouteKey:
    return RouteKey(method=method.upper(), path=normalise_path(path))


class RouteMatcher:
    """Finds the route that serves a request path.

    A route's parameters (`/users/{id}`) become `*`, which matches exactly one
    segment of a concrete call path (`/users/123`). An exact route always wins
    over a parameterised one, as every router does it.
    """

    def __init__(self, routes: list[tuple[str, str]]):
        """`routes` is (method, path as written)."""
        self._exact: dict[RouteKey, RouteKey] = {}
        self._patterns: list[tuple[RouteKey, tuple[str, ...]]] = []

        for method, path in routes:
            key = route_key(method, path)
            segments = tuple(key.path.split("/"))
            if "*" in segments:
                self._patterns.append((key, segments))
            else:
                self._exact[key] = key

    def match(self, method: str, path: str) -> RouteKey | None:
        key = route_key(method, path)
        if key in self._exact:
            return key

        segments = key.path.split("/")
        for route, pattern in self._patterns:
            if route.method != key.method or len(pattern) != len(segments):
                continue
            if all(expected in ("*", actual) for expected, actual in zip(pattern, segments)):
                return route
        return None
