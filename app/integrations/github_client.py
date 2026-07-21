from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError, UnauthorizedError

logger = structlog.get_logger("app.github")

GITHUB_OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_BASE = "https://api.github.com"
OAUTH_SCOPES = "read:user repo admin:repo_hook"


class GitHubAuthExpired(UnauthorizedError):
    """GitHub rejected the stored token — the connection must be re-authorized."""

    error_code = "github_needs_reauth"


class GitHubClient:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=15)

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams(
            client_id=settings.github_client_id,
            redirect_uri=f"{settings.app_base_url}/auth/github/callback",
            scope=OAUTH_SCOPES,
            state=state,
        )
        return f"{GITHUB_OAUTH_AUTHORIZE_URL}?{params}"

    async def exchange_code(self, code: str) -> str:
        settings = get_settings()
        resp = await self._http.post(
            GITHUB_OAUTH_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        self._log_call("POST", GITHUB_OAUTH_TOKEN_URL, resp)
        if resp.status_code != 200:
            raise ExternalServiceError("GitHub token exchange failed")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise ExternalServiceError(f"GitHub token exchange rejected: {body.get('error', 'unknown')}")
        return token

    async def api_get(self, token: str, path: str, **params: Any) -> httpx.Response:
        resp = await self._http.get(
            f"{GITHUB_API_BASE}{path}",
            params=params or None,
            headers=self._auth_headers(token),
        )
        self._log_call("GET", path, resp)
        return self._checked(resp)

    async def api_post(self, token: str, path: str, json: dict) -> httpx.Response:
        resp = await self._http.post(
            f"{GITHUB_API_BASE}{path}",
            json=json,
            headers=self._auth_headers(token),
        )
        self._log_call("POST", path, resp)
        return self._checked(resp)

    async def get_profile(self, token: str) -> dict:
        resp = await self.api_get(token, "/user")
        return resp.json()

    @staticmethod
    def _auth_headers(token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _checked(resp: httpx.Response) -> httpx.Response:
        if resp.status_code == 401:
            raise GitHubAuthExpired("GitHub token is no longer valid")
        remaining = resp.headers.get("x-ratelimit-remaining")
        if resp.status_code == 403 and remaining == "0":
            raise ExternalServiceError("GitHub rate limit exhausted", error_code="github_rate_limited")
        return resp

    @staticmethod
    def _log_call(method: str, path: str, resp: httpx.Response) -> None:
        try:
            latency_ms = round(resp.elapsed.total_seconds() * 1000, 1)
        except RuntimeError:
            latency_ms = None
        logger.info(
            "github_api_call",
            method=method,
            path=path,
            status=resp.status_code,
            ratelimit_remaining=resp.headers.get("x-ratelimit-remaining"),
            latency_ms=latency_ms,
        )


_client: GitHubClient | None = None


def get_github_client() -> GitHubClient:
    global _client
    if _client is None:
        _client = GitHubClient()
    return _client


def use_github_client(client: GitHubClient) -> None:
    global _client
    _client = client
