"""How a connector's requests get authenticated.

`NativeOAuth` signs requests with our own tokens from `oauth_tokens`. A platform backend
(Composio, for providers whose OAuth app isn't verified yet) would implement the same
`request` by proxying through the platform; connections record which one they use.
"""

import asyncio
from typing import Any

import httpx
import structlog

from app.connectors.oauth import OAuth2Flow, http
from app.core.constants import ConnectionStatus
from app.core.exceptions import ExternalServiceError, ForbiddenError, UnauthorizedError
from app.db import oauth_tokens
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors")

#: Longest `Retry-After` honoured before giving up on a rate-limited call.
MAX_RETRY_AFTER_SECONDS = 10


class NativeOAuth:
    def __init__(self, flow: OAuth2Flow):
        self.flow = flow
        # the token's key: one per OAuth app, shared by the products on it (Jira and Confluence)
        self.credential = flow.config.provider

    async def request(self, conn: dict, method: str, url: str, **kwargs: Any) -> httpx.Response:
        provider = conn["provider"]
        token = await oauth_tokens.get_fresh_access_token(conn["workspace_id"], self.credential, self.flow.refresh)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **kwargs.pop("headers", {})}
        for attempt in range(2):
            resp = await http.request(method, url, headers=headers, **kwargs)
            log_call(logger, f"{provider}_api_call", method, httpx.URL(url).path, resp)
            if resp.status_code != 429 or attempt:
                break
            # one retry when the provider says how long to wait and it's short enough
            wait = _retry_after(resp)
            if wait is None:
                break
            await asyncio.sleep(wait)
        if resp.status_code == 401:
            if _scope_mismatch(resp):
                # the token is fine; this call needs a scope the grant lacks, so only this call fails
                raise ForbiddenError(
                    f"reconnect {provider} to allow this", error_code=f"{provider}_scope_missing",
                )
            await oauth_tokens.set_status(conn["workspace_id"], self.credential, ConnectionStatus.NEEDS_REAUTH)
            raise UnauthorizedError(f"{provider} token is no longer valid", error_code=f"{provider}_needs_reauth")
        if resp.status_code == 429:
            raise ExternalServiceError(f"{provider} rate limit hit", error_code=f"{provider}_rate_limited")
        return resp


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        seconds = float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= MAX_RETRY_AFTER_SECONDS else None


def _scope_mismatch(resp: httpx.Response) -> bool:
    """Atlassian answers a call outside the grant's scopes with 401 "scope does not match"."""
    try:
        return "scope does not match" in str(resp.json().get("message", "")).lower()
    except ValueError:
        return False
