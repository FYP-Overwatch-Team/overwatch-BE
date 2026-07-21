from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError, UnauthorizedError

logger = structlog.get_logger("app.jira")

ATLASSIAN_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_BASE = "https://api.atlassian.com/ex/jira"
# Verified against Atlassian 3LO docs (July 2026): classic read scopes + offline_access
# (required for a refresh token; Atlassian rotates the refresh token on every use).
OAUTH_SCOPES = "read:jira-work read:jira-user offline_access"


class JiraAuthExpired(UnauthorizedError):
    error_code = "jira_needs_reauth"


class JiraClient:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=15)

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams(
            audience="api.atlassian.com",
            client_id=settings.jira_client_id,
            scope=OAUTH_SCOPES,
            redirect_uri=f"{settings.app_base_url}/jira/callback",
            state=state,
            response_type="code",
            prompt="consent",
        )
        return f"{ATLASSIAN_AUTHORIZE_URL}?{params}"

    async def exchange_code(self, code: str) -> dict:
        settings = get_settings()
        return await self._token_request({
            "grant_type": "authorization_code",
            "client_id": settings.jira_client_id,
            "client_secret": settings.jira_client_secret,
            "code": code,
            "redirect_uri": f"{settings.app_base_url}/jira/callback",
        })

    async def refresh_tokens(self, refresh_token: str) -> dict:
        settings = get_settings()
        return await self._token_request({
            "grant_type": "refresh_token",
            "client_id": settings.jira_client_id,
            "client_secret": settings.jira_client_secret,
            "refresh_token": refresh_token,
        })

    async def _token_request(self, payload: dict) -> dict:
        resp = await self._http.post(ATLASSIAN_TOKEN_URL, json=payload)
        self._log_call("POST", "/oauth/token", resp)
        if resp.status_code != 200:
            if payload["grant_type"] == "refresh_token":
                raise JiraAuthExpired("Jira refresh token rejected; re-authorization required")
            raise ExternalServiceError("Atlassian token exchange failed")
        body = resp.json()
        if "access_token" not in body:
            raise ExternalServiceError("Atlassian token response missing access_token")
        return body

    async def get_accessible_resources(self, access_token: str) -> list[dict]:
        resp = await self._http.get(
            ACCESSIBLE_RESOURCES_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        self._log_call("GET", "/oauth/token/accessible-resources", resp)
        if resp.status_code != 200:
            raise ExternalServiceError("failed to fetch accessible Atlassian resources")
        return resp.json()

    async def api_get(self, access_token: str, cloud_id: str, path: str, **params: Any) -> httpx.Response:
        resp = await self._http.get(
            f"{API_BASE}/{cloud_id}{path}",
            params=params or None,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        self._log_call("GET", path, resp)
        if resp.status_code == 401:
            raise JiraAuthExpired("Jira token is no longer valid")
        if resp.status_code == 429:
            raise ExternalServiceError("Jira rate limit hit", error_code="jira_rate_limited")
        return resp

    @staticmethod
    def _log_call(method: str, path: str, resp: httpx.Response) -> None:
        try:
            latency_ms = round(resp.elapsed.total_seconds() * 1000, 1)
        except RuntimeError:
            latency_ms = None
        logger.info(
            "jira_api_call",
            method=method,
            path=path,
            status=resp.status_code,
            ratelimit_remaining=resp.headers.get("x-ratelimit-remaining"),
            latency_ms=latency_ms,
        )


_client: JiraClient | None = None


def get_jira_client() -> JiraClient:
    global _client
    if _client is None:
        _client = JiraClient()
    return _client


def use_jira_client(client: JiraClient) -> None:
    global _client
    _client = client
