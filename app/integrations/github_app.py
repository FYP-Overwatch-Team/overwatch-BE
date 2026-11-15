"""GitHub App client.

Three kinds of credentials:
- user token (user-to-server OAuth with PKCE): sign-in, and listing what the user can see
- app JWT (signed with the App private key): app-level calls like finding a repo's installation
- installation token (minted from the JWT, ~1h): cloning and fetching repos
"""

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
import jwt
import structlog

from app.core.config import get_settings
from app.core.exceptions import AppError, ExternalServiceError, UnauthorizedError
from app.core.time import utcnow
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.github")

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_BASE = "https://api.github.com"


class GitHubAuthExpired(UnauthorizedError):
    """GitHub rejected the stored user token; the user must sign in again."""

    error_code = "github_needs_reauth"


class InstallationGone(AppError):
    """The App was uninstalled (or lost access), so the repo can no longer be read."""

    status_code = 404
    error_code = "github_installation_gone"


# reuse an installation token until this close to its expiry
INSTALLATION_TOKEN_MARGIN = timedelta(minutes=5)
PAGE_SIZE = 100


class GitHubApp:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=15)
        self._installation_tokens: dict[int, tuple[str, datetime]] = {}

    def authorize_url(self, state: str, code_challenge: str) -> str:
        settings = get_settings()
        params = httpx.QueryParams(
            client_id=settings.github_app_client_id,
            redirect_uri=f"{settings.app_base_url}/auth/github/callback",
            state=state,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        return f"{AUTHORIZE_URL}?{params}"

    async def exchange_code(self, code: str, code_verifier: str) -> dict:
        """Code -> {access_token, expires_in, refresh_token, refresh_token_expires_in}."""
        settings = get_settings()
        return await self._token_request({
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": f"{settings.app_base_url}/auth/github/callback",
        })

    async def refresh_user_token(self, refresh_token: str) -> dict:
        """GitHub rotates the refresh token on every use; store both returned tokens."""
        try:
            return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        except ExternalServiceError as exc:
            raise GitHubAuthExpired("GitHub refresh token rejected") from exc

    async def get_user(self, user_token: str) -> dict:
        return (await self._api("GET", "/user", user_token)).json()

    async def list_user_installations(self, user_token: str) -> list[dict]:
        """Installations of this App the user can access (their account and their orgs)."""
        return await self._paginate("/user/installations", user_token, "installations")

    async def list_installation_repos(self, user_token: str, installation_id: int) -> list[dict]:
        """Repos in an installation that the user can also access."""
        return await self._paginate(f"/user/installations/{installation_id}/repositories", user_token, "repositories")

    async def get_repo(self, user_token: str, repo_full_name: str) -> dict | None:
        """The repo as the user sees it; None unless the user has access *and* the App is installed."""
        resp = await self._api("GET", f"/repos/{repo_full_name}", user_token, allow_404=True)
        return None if resp.status_code == 404 else resp.json()

    async def get_repo_installation(self, repo_full_name: str) -> dict | None:
        resp = await self._api("GET", f"/repos/{repo_full_name}/installation", self.app_jwt(), allow_404=True)
        return None if resp.status_code == 404 else resp.json()

    async def installation_token(self, installation_id: int) -> str:
        """A short-lived token scoped to one installation, cached until near expiry."""
        cached = self._installation_tokens.get(installation_id)
        if cached and cached[1] - INSTALLATION_TOKEN_MARGIN > utcnow():
            return cached[0]

        resp = await self._api(
            "POST", f"/app/installations/{installation_id}/access_tokens", self.app_jwt(), allow_404=True,
        )
        if resp.status_code == 404:
            self._installation_tokens.pop(installation_id, None)
            raise InstallationGone("the GitHub App is no longer installed for this repository")
        body = resp.json()
        expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        self._installation_tokens[installation_id] = (body["token"], expires_at)
        return body["token"]

    def app_jwt(self) -> str:
        settings = get_settings()
        now = int(utcnow().timestamp())
        # iat in the past absorbs clock drift; GitHub caps exp at 10 minutes
        claims = {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_client_id}
        return jwt.encode(claims, _private_key(settings.github_app_private_key_path), algorithm="RS256")

    async def _paginate(self, path: str, token: str, key: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            body = (await self._api("GET", path, token, params={"per_page": PAGE_SIZE, "page": page})).json()
            batch = body[key]
            items.extend(batch)
            if len(batch) < PAGE_SIZE or len(items) >= body.get("total_count", 0):
                return items
            page += 1

    async def _api(self, method: str, path: str, token: str, *, allow_404: bool = False, **kwargs) -> httpx.Response:
        resp = await self._send(method, f"{API_BASE}{path}", headers=_auth_headers(token), **kwargs)
        if resp.status_code == 401:
            raise GitHubAuthExpired("GitHub token is no longer valid")
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            raise ExternalServiceError("GitHub rate limit exhausted", error_code="github_rate_limited")
        if resp.is_success or (allow_404 and resp.status_code == 404):
            return resp
        raise ExternalServiceError(f"GitHub {method} {path} failed with {resp.status_code}")

    async def _token_request(self, data: dict) -> dict:
        settings = get_settings()
        resp = await self._send(
            "POST", TOKEN_URL,
            data={"client_id": settings.github_app_client_id, "client_secret": settings.github_app_client_secret, **data},
            headers={"Accept": "application/json"},
        )
        body = resp.json() if resp.status_code == 200 else {}
        # failures come back as 200 with an "error" field
        if "access_token" not in body:
            raise ExternalServiceError(f"GitHub token request rejected: {body.get('error', resp.status_code)}")
        return body

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ExternalServiceError("GitHub is unreachable") from exc
        log_call(logger, "github_api_call", method, httpx.URL(url).path, resp)
        return resp


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@lru_cache
def _private_key(path: str) -> str:
    if not path:
        raise RuntimeError("GITHUB_APP_PRIVATE_KEY_PATH is not configured")
    return Path(path).read_text()


_app: GitHubApp | None = None


def get_github_app() -> GitHubApp:
    global _app
    if _app is None:
        _app = GitHubApp()
    return _app
