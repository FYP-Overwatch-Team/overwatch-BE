"""What Atlassian's products share: one OAuth app, one token per workspace, and one site.

Jira and Confluence are separate connections, but a single consent covers both, so connecting
the second asks for its scopes on top of the first's and replaces the shared token.
"""

import structlog

from app.connectors.oauth import OAuthConfig, http
from app.core.constants import CredentialGroup
from app.core.exceptions import ExternalServiceError, NotFoundError
from app.integrations.http_log import log_call

logger = structlog.get_logger("app.connectors.atlassian")

ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# each product adds its own scopes; offline_access is what returns a (rotating) refresh token
OAUTH = OAuthConfig(
    provider=CredentialGroup.ATLASSIAN,
    authorize_url="https://auth.atlassian.com/authorize",
    token_url="https://auth.atlassian.com/oauth/token",
    scopes=("offline_access",),
    client_id_setting="jira_client_id",
    client_secret_setting="jira_client_secret",
    redirect_path="/jira/callback",
    extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
)


async def pick_site(access_token: str, known_site_id: str | None, product: str, error_code: str) -> dict:
    """The site a connection uses: the one the workspace already uses, else the first offering `product`.

    `product` appears in the product's scope names ("jira", "confluence"); a site listing no scopes is taken as offering it.
    """
    resp = await http.get(ACCESSIBLE_RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    log_call(logger, "atlassian_api_call", "GET", "/oauth/token/accessible-resources", resp)
    if resp.status_code != 200:
        raise ExternalServiceError("failed to fetch accessible Atlassian resources")

    def offers(site: dict) -> bool:
        return "scopes" not in site or any(product in scope for scope in site["scopes"])

    # one site per workspace: once a product is connected, the others must be on its site
    candidates = [site for site in resp.json() if not known_site_id or site["id"] == known_site_id]
    site = next((site for site in candidates if offers(site)), None)
    if site is None:
        raise NotFoundError(f"no accessible {product} site for this account", error_code=error_code)
    return {"id": site["id"], "name": site.get("name"), "url": site.get("url")}
