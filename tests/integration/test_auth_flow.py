import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import respx
from httpx import Response

TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

PROFILE = {"id": 42, "login": "octocat", "name": "Octo Cat", "avatar_url": "https://img"}
TOKENS = {
    "access_token": "ghu_abc",
    "expires_in": 28800,
    "refresh_token": "ghr_abc",
    "refresh_token_expires_in": 15897600,
    "token_type": "bearer",
}


def mock_github(router: respx.Router, tokens: dict = TOKENS):
    token_route = router.post(TOKEN_URL).mock(return_value=Response(200, json=tokens))
    router.get(USER_URL).mock(return_value=Response(200, json=PROFILE))
    return token_route


async def _start_login(client) -> dict:
    resp = await client.get("/auth/github/login")
    assert resp.status_code == 307
    return {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}


async def login(client) -> str:
    """Sign in against mocked GitHub; returns the access JWT."""
    params = await _start_login(client)
    with respx.mock as router:
        mock_github(router)
        cb = await client.get(f"/auth/github/callback?code=fake-code&state={params['state']}")
    assert cb.status_code == 307
    refreshed = await client.post("/auth/refresh")
    assert refreshed.status_code == 200
    return refreshed.json()["data"]["access_token"]


async def test_login_redirects_to_github_with_pkce(client):
    params = await _start_login(client)
    assert params["client_id"] == "test-client-id"
    assert params["code_challenge_method"] == "S256"
    assert params["redirect_uri"].endswith("/auth/github/callback")


async def test_callback_sends_the_matching_code_verifier(client):
    params = await _start_login(client)
    with respx.mock as router:
        token_route = mock_github(router)
        await client.get(f"/auth/github/callback?code=fake-code&state={params['state']}")

    sent = parse_qs(token_route.calls.last.request.content.decode())
    digest = hashlib.sha256(sent["code_verifier"][0].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == params["code_challenge"]


async def test_full_login_flow_issues_jwt_and_refresh_cookie(client):
    params = await _start_login(client)
    with respx.mock as router:
        mock_github(router)
        cb = await client.get(f"/auth/github/callback?code=fake-code&state={params['state']}")

    # no token in the redirect: the frontend exchanges the cookie via /auth/refresh
    assert cb.headers["location"] == "http://localhost:3000/auth/complete"
    assert "overwatch_refresh" in client.cookies

    access = (await client.post("/auth/refresh")).json()["data"]["access_token"]
    me = await client.get("/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["data"]["github_login"] == "octocat"


async def test_state_mismatch_redirects_with_error(client):
    await _start_login(client)
    resp = await client.get("/auth/github/callback?code=x&state=wrong-state")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/sign-in?error=oauth_state_mismatch")
    assert "overwatch_refresh" not in client.cookies


async def test_denied_consent_redirects_with_error(client):
    params = await _start_login(client)
    resp = await client.get(f"/auth/github/callback?error=access_denied&state={params['state']}")
    assert resp.headers["location"].endswith("/sign-in?error=oauth_cancelled")


async def test_rejected_code_redirects_with_error(client):
    params = await _start_login(client)
    with respx.mock as router:
        mock_github(router, tokens={"error": "bad_verification_code"})
        resp = await client.get(f"/auth/github/callback?code=stale&state={params['state']}")
    assert resp.headers["location"].endswith("/sign-in?error=github_unavailable")


async def test_refresh_rotates_and_reuse_revokes_family(client):
    await login(client)
    old_refresh = client.cookies["overwatch_refresh"]

    r1 = await client.post("/auth/refresh")
    assert r1.status_code == 200
    new_refresh = client.cookies["overwatch_refresh"]
    assert new_refresh != old_refresh

    # replay the old (already-rotated) cookie -> reuse detection kills the family
    client.cookies.set("overwatch_refresh", old_refresh, path="/auth")
    r2 = await client.post("/auth/refresh")
    assert r2.status_code == 401
    assert r2.json()["meta"]["error_code"] == "refresh_token_reused"

    # even the newest token is now revoked
    client.cookies.set("overwatch_refresh", new_refresh, path="/auth")
    assert (await client.post("/auth/refresh")).status_code == 401


async def test_logout_invalidates_refresh(client):
    await login(client)
    assert (await client.post("/auth/logout")).status_code == 200
    assert (await client.post("/auth/refresh")).status_code == 401


async def test_logout_all_revokes_every_session(client):
    access = await login(client)
    first_device = client.cookies["overwatch_refresh"]
    await login(client)  # a second sign-in starts a second family
    second_device = client.cookies["overwatch_refresh"]

    out = await client.post("/auth/logout-all", headers={"Authorization": f"Bearer {access}"})
    assert out.status_code == 200

    for raw in (first_device, second_device):
        client.cookies.set("overwatch_refresh", raw, path="/auth")
        assert (await client.post("/auth/refresh")).status_code == 401


async def test_logout_all_requires_bearer(client):
    assert (await client.post("/auth/logout-all")).status_code == 401


async def test_protected_route_rejects_bad_tokens(client):
    assert (await client.get("/me")).status_code == 401

    bad = await client.get("/me", headers={"Authorization": "Bearer nonsense"})
    assert bad.status_code == 401
    assert bad.json()["meta"]["error_code"] == "invalid_token"
