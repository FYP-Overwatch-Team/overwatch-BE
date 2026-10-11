"""How a connector's requests get authenticated.

`NativeOAuth` signs requests with our own tokens from `oauth_tokens`. A platform backend
(Composio, for providers whose OAuth app isn't verified yet) would implement the same
`request` by proxying through the platform; connections record which one they use.
"""

from typing import Any

import httpx
import structlog

from app.connectors.oauth import OAuth2Flow, http
from app.core.constants import ConnectionStatus
from app.core.exceptions import ExternalServiceError, UnauthorizedError
from app.db import oauth_tokens
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors")


class NativeOAuth:
    def __init__(self, flow: OAuth2Flow):
        self.flow = flow
        self.provider = flow.config.provider

    async def request(self, conn: dict, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await oauth_tokens.get_fresh_access_token(conn["workspace_id"], self.provider, self.flow.refresh)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **kwargs.pop("headers", {})}
        resp = await http.request(method, url, headers=headers, **kwargs)
        log_call(logger, f"{self.provider}_api_call", method, httpx.URL(url).path, resp)
        if resp.status_code == 401:
            await oauth_tokens.set_status(conn["workspace_id"], self.provider, ConnectionStatus.NEEDS_REAUTH)
            raise UnauthorizedError(f"{self.provider} token is no longer valid", error_code=f"{self.provider}_needs_reauth")
        if resp.status_code == 429:
            raise ExternalServiceError(f"{self.provider} rate limit hit", error_code=f"{self.provider}_rate_limited")
        return resp
