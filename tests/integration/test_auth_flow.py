from urllib.parse import parse_qs, urlparse

import respx
from httpx import Response

TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"

PROFILE = {"id": 42, "login": "octocat", "name": "Octo Cat", "avatar_url": "https://img"}


def mock_github(router: respx.Router, token: str = "gho_abc"):
    router.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": token}))
    router.get(USER_URL).mock(return_value=Response(200, json=PROFILE))


async def login(client) -> str:
    """Run the full OAuth dance against mocked GitHub; returns the access JWT."""
    login_resp = await client.get("/auth/github/login")
    assert login_resp.status_code == 307
    redirect = urlparse(login_resp.headers["location"])
    state = parse_qs(redirect.query)["state"][0]

    with respx.mock as router:
        mock_github(router)
        cb = await client.get(f"/auth/github/callback?code=fake-code&state={state}")
    assert cb.status_code == 307
    fragment = urlparse(cb.headers["location"]).fragment
    return fragment.split("access_token=")[1]


async def test_full_login_flow_issues_jwt_and_refresh_cookie(client):
    access = await login(client)
    me = await client.get("/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["github_login"] == "octocat"
    assert "overwatch_refresh" in client.cookies


async def test_state_mismatch_rejected(client):
    await client.get("/auth/github/login")
    resp = await client.get("/auth/github/callback?code=x&state=wrong-state")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "oauth_state_mismatch"


async def test_refresh_rotates_and_reuse_revokes_family(client):
    await login(client)
    old_refresh = client.cookies["overwatch_refresh"]

    r1 = await client.post("/auth/refresh")
    assert r1.status_code == 200
    assert "access_token" in r1.json()
    new_refresh = client.cookies["overwatch_refresh"]
    assert new_refresh != old_refresh

    # replay the old (already-rotated) cookie -> reuse detection kills the family
    client.cookies.set("overwatch_refresh", old_refresh, path="/auth")
    r2 = await client.post("/auth/refresh")
    assert r2.status_code == 401
    assert r2.json()["error_code"] == "refresh_token_reused"

    # even the newest token is now revoked
    client.cookies.set("overwatch_refresh", new_refresh, path="/auth")
    r3 = await client.post("/auth/refresh")
    assert r3.status_code == 401


async def test_logout_invalidates_refresh(client):
    await login(client)
    out = await client.post("/auth/logout")
    assert out.status_code == 200

    refresh = await client.post("/auth/refresh")
    assert refresh.status_code == 401


async def test_protected_route_rejects_bad_tokens(client):
    no_token = await client.get("/me")
    assert no_token.status_code == 401

    bad = await client.get("/me", headers={"Authorization": "Bearer nonsense"})
    assert bad.status_code == 401
    assert bad.json()["error_code"] == "invalid_token"
