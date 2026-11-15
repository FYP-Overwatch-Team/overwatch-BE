import httpx
import structlog


def log_call(logger: structlog.typing.FilteringBoundLogger, event: str, method: str, path: str, resp: httpx.Response) -> None:
    """One structured line per outbound API call: status, latency and rate-limit headroom."""
    try:
        latency_ms = round(resp.elapsed.total_seconds() * 1000, 1)
    except RuntimeError:  # mocked responses have no elapsed time
        latency_ms = None
    logger.info(
        event,
        method=method,
        path=path,
        status=resp.status_code,
        ratelimit_remaining=resp.headers.get("x-ratelimit-remaining"),
        latency_ms=latency_ms,
    )
