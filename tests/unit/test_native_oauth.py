from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from app.connectors import registry
from app.core import security
from app.core.exceptions import ExternalServiceError, ForbiddenError, UnauthorizedError
from app.db import mongo, oauth_tokens

TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API_URL = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/myself"
CONN = {"workspace_id": "ws-1", "provider": "jira", "auth_backend": "native_oauth"}

auth = registry.auth_for(CONN)


async def seed_tokens(expires_in_seconds: int) -> None:
    await oauth_tokens.save(
        "ws-1", "jira", {"access_token": "old-access", "refresh_token": "old-refresh", "expires_in": expires_in_seconds},
    )


def bearer(route: respx.Route) -> str:
    return route.calls[0].request.headers["Authorization"]


async def test_fresh_token_signs_the_request_without_refresh():
    await seed_tokens(expires_in_seconds=3600)
    with respx.mock as router:  # strict: only the API call is allowed
        api = router.get(API_URL).mock(return_value=Response(200, json={}))
        await auth.request(CONN, "GET", API_URL)
    assert bearer(api) == "Bearer old-access"


async def test_token_inside_buffer_refreshes_and_rotates():
    await seed_tokens(expires_in_seconds=60)  # < 2-min buffer, still technically valid
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(200, json={
            "access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 3600,
        }))
        api = router.get(API_URL).mock(return_value=Response(200, json={}))
        await auth.request(CONN, "GET", API_URL)

    assert bearer(api) == "Bearer new-access"
    doc = await mongo.oauth_tokens().find_one({"owner_id": "ws-1", "provider": "jira"})
    assert security.decrypt_secret(doc["refresh_token_encrypted"]) == "rotated-refresh"


async def test_rejected_refresh_marks_needs_reauth_and_fails_fast_after():
    await seed_tokens(expires_in_seconds=0)
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(403, json={"error": "invalid_grant"}))
        with pytest.raises(UnauthorizedError) as exc:
            await auth.request(CONN, "GET", API_URL)
    assert exc.value.error_code == "jira_needs_reauth"
    assert (await oauth_tokens.get("ws-1", "jira"))["status"] == "needs_reauth"

    with respx.mock, pytest.raises(UnauthorizedError):
        await auth.request(CONN, "GET", API_URL)


async def test_a_revoked_token_needs_reauth():
    await seed_tokens(expires_in_seconds=3600)
    with respx.mock as router:
        router.get(API_URL).mock(return_value=Response(401))
        with pytest.raises(UnauthorizedError):
            await auth.request(CONN, "GET", API_URL)
    assert (await oauth_tokens.get("ws-1", "jira"))["status"] == "needs_reauth"


async def test_a_call_outside_the_grants_scopes_fails_alone(monkeypatch):
    await seed_tokens(expires_in_seconds=3600)
    with respx.mock as router:
        router.get(API_URL).mock(return_value=Response(401, json={"code": 401, "message": "Unauthorized; scope does not match"}))
        with pytest.raises(ForbiddenError) as exc:
            await auth.request(CONN, "GET", API_URL)
    assert exc.value.error_code == "jira_scope_missing"
    # the token still works for everything else, so the connection isn't flagged
    assert (await oauth_tokens.get("ws-1", "jira"))["status"] == "active"


async def test_a_rate_limit_with_a_short_retry_after_is_retried_once(monkeypatch):
    await seed_tokens(expires_in_seconds=3600)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("app.connectors.auth_backends.asyncio.sleep", sleep)
    with respx.mock as router:
        route = router.get(API_URL).mock(side_effect=[Response(429, headers={"Retry-After": "2"}), Response(200, json={})])
        resp = await auth.request(CONN, "GET", API_URL)
    assert (resp.status_code, route.call_count, waits) == (200, 2, [2.0])


async def test_rate_limits_surface_as_their_own_error():
    await seed_tokens(expires_in_seconds=3600)
    with respx.mock as router:
        router.get(API_URL).mock(return_value=Response(429))
        with pytest.raises(ExternalServiceError) as exc:
            await auth.request(CONN, "GET", API_URL)
    assert exc.value.error_code == "jira_rate_limited"


async def test_no_connection_needs_reauth():
    with pytest.raises(UnauthorizedError) as exc:
        await auth.request(CONN, "GET", API_URL)
    assert exc.value.error_code == "jira_needs_reauth"


def test_the_authorize_url_asks_for_write_access_and_a_refresh_token():
    params = parse_qs(urlparse(registry.get("jira").flow.authorize_url("s")).query)
    assert params["scope"] == ["read:jira-work write:jira-work read:jira-user offline_access"]
    assert params["redirect_uri"] == ["http://localhost:8000/jira/callback"]
    assert (params["audience"], params["prompt"], params["state"]) == (["api.atlassian.com"], ["consent"], ["s"])
