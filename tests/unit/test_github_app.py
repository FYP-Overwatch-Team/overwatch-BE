from datetime import timedelta

import httpx
import pytest
import respx
from httpx import Response

from app.core.exceptions import ExternalServiceError
from app.core.time import utcnow
from app.integrations.github_app import TOKEN_URL, GitHubApp, GitHubAuthExpired, InstallationGone


async def test_refresh_rejection_means_reauth():
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(200, json={"error": "bad_refresh_token"}))
        with pytest.raises(GitHubAuthExpired):
            await GitHubApp().refresh_user_token("ghr_old")


async def test_refresh_sends_grant_type_and_client_credentials():
    with respx.mock as router:
        route = router.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "ghu_new"}))
        assert (await GitHubApp().refresh_user_token("ghr_old"))["access_token"] == "ghu_new"
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body and "refresh_token=ghr_old" in body and "client_secret=" in body


async def test_network_failure_is_an_external_service_error():
    with respx.mock as router:
        router.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ExternalServiceError):
            await GitHubApp().exchange_code("code", "verifier")


def _token_response(token: str, minutes: int) -> Response:
    expires = (utcnow() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return Response(201, json={"token": token, "expires_at": expires})


INSTALLATION_TOKEN_URL = "https://api.github.com/app/installations/9/access_tokens"


async def test_installation_token_is_cached_until_near_expiry():
    app = GitHubApp()
    with respx.mock as router:
        route = router.post(INSTALLATION_TOKEN_URL).mock(return_value=_token_response("ghs_1", 60))
        assert await app.installation_token(9) == "ghs_1"
        assert await app.installation_token(9) == "ghs_1"
    assert route.call_count == 1


async def test_installation_token_close_to_expiry_is_replaced():
    app = GitHubApp()
    with respx.mock as router:
        route = router.post(INSTALLATION_TOKEN_URL).mock(
            side_effect=[_token_response("ghs_old", 3), _token_response("ghs_new", 60)],
        )
        await app.installation_token(9)
        assert await app.installation_token(9) == "ghs_new"
    assert route.call_count == 2


async def test_uninstalled_app_raises_installation_gone():
    with respx.mock as router:
        router.post(INSTALLATION_TOKEN_URL).mock(return_value=Response(404, json={"message": "Not Found"}))
        with pytest.raises(InstallationGone):
            await GitHubApp().installation_token(9)


async def test_expired_user_token_needs_reauth():
    with respx.mock as router:
        router.get("https://api.github.com/user/installations").mock(return_value=Response(401))
        with pytest.raises(GitHubAuthExpired):
            await GitHubApp().list_user_installations("ghu_dead")
