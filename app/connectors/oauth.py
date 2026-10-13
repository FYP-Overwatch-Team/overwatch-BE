"""One OAuth2 authorization-code flow for every provider; each supplies an `OAuthConfig`."""

from dataclasses import dataclass, field

import httpx
import structlog

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError, UnauthorizedError
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors")

http = httpx.AsyncClient(timeout=15)


@dataclass(frozen=True)
class OAuthConfig:
    provider: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id_setting: str
    client_secret_setting: str
    # must match the redirect URI registered with the provider
    redirect_path: str
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


class OAuth2Flow:
    def __init__(self, config: OAuthConfig):
        self.config = config

    @property
    def redirect_uri(self) -> str:
        return f"{get_settings().app_base_url}{self.config.redirect_path}"

    def _client(self) -> tuple[str, str]:
        settings = get_settings()
        return getattr(settings, self.config.client_id_setting), getattr(settings, self.config.client_secret_setting)

    def authorize_url(self, state: str) -> str:
        params = httpx.QueryParams(
            client_id=self._client()[0],
            scope=" ".join(self.config.scopes),
            redirect_uri=self.redirect_uri,
            state=state,
            response_type="code",
            **self.config.extra_authorize_params,
        )
        return f"{self.config.authorize_url}?{params}"

    async def exchange_code(self, code: str) -> dict:
        return await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
        )

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _token_request(self, payload: dict) -> dict:
        client_id, client_secret = self._client()
        resp = await http.post(self.config.token_url, json={**payload, "client_id": client_id, "client_secret": client_secret})
        log_call(logger, f"{self.config.provider}_api_call", "POST", "/oauth/token", resp)
        if resp.status_code != 200:
            if payload["grant_type"] == "refresh_token":
                raise UnauthorizedError(
                    f"{self.config.provider} refresh token rejected; re-authorization required",
                    error_code=f"{self.config.provider}_needs_reauth",
                )
            raise ExternalServiceError(f"{self.config.provider} token exchange failed")
        body = resp.json()
        if "access_token" not in body:
            raise ExternalServiceError(f"{self.config.provider} token response missing access_token")
        return body
