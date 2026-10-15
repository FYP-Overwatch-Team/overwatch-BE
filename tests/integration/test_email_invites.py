"""Invites by email: mailed, never shown, accepted by that address only, and an account made straight from the link."""

import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import respx
from httpx import Response

from app.core.time import utcnow
from app.db import mongo
from tests.integration.test_auth_flow import PROFILE, TOKENS, USER_URL, TOKEN_URL, auth_headers, login
from tests.integration.test_native_auth import PASSWORD, verified_account
from tests.integration.test_workspaces import bearer, team, user

INVITEE = "grace@example.com"
NEW_ACCOUNT = {"name": "Grace Hopper", "username": "grace", "password": "a long enough password"}


def token_in(outbox, address: str = INVITEE) -> str:
    return re.search(r"/invite#([\w-]+)", outbox.to(address)[-1].text).group(1)


async def email_invite(client, admin: dict, workspace_id: str, email: str = INVITEE, role: str = "member"):
    return await client.post(f"/workspaces/{workspace_id}/invites", json={"email": email, "role": role}, headers=bearer(admin))


async def test_an_email_invite_is_mailed_and_its_link_never_returned(client, outbox):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    resp = await email_invite(client, alice, acme, email="Grace@Example.com ")

    data = resp.json()["data"]
    assert resp.status_code == 201 and data["url"] is None
    assert (data["kind"], data["email"]) == ("email", INVITEE)
    mail = outbox.to(INVITEE)[0]
    assert mail.subject == "alice invited you to Acme on Overwatch"
    assert "<script" not in mail.html and token_in(outbox)


async def test_one_pending_invite_per_email_and_none_for_members(client, outbox):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    await email_invite(client, alice, acme)
    duplicate = await email_invite(client, alice, acme)
    restricted_twice = await client.post(f"/workspaces/{acme}/invites", json={"email": INVITEE, "github_login": "grace"}, headers=bearer(alice))

    assert duplicate.json()["meta"]["error_code"] == "invite_exists"
    assert restricted_twice.status_code == 422

    # an expired invite stops holding the address
    await mongo.workspace_invites().update_many({}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}})
    assert (await email_invite(client, alice, acme)).status_code == 201


async def test_resending_replaces_the_link_after_a_cooldown(client, outbox):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    invite_id = (await email_invite(client, alice, acme)).json()["data"]["id"]
    first = token_in(outbox)
    too_soon = await client.post(f"/workspaces/{acme}/invites/{invite_id}/resend", headers=bearer(alice))
    await mongo.workspace_invites().update_one({"_id": invite_id}, {"$set": {"last_sent_at": utcnow() - timedelta(minutes=2)}})
    resent = await client.post(f"/workspaces/{acme}/invites/{invite_id}/resend", headers=bearer(alice))

    assert too_soon.json()["meta"]["error_code"] == "invite_resend_cooldown"
    assert resent.status_code == 200 and token_in(outbox) != first
    stale = await client.post("/invites/public-preview", json={"token": first})
    assert stale.json()["meta"]["error_code"] == "invite_invalid"


async def test_a_workspace_can_only_send_so_many_invite_emails_a_day(client, outbox, monkeypatch):
    monkeypatch.setenv("INVITE_EMAIL_DAILY_LIMIT_WORKSPACE", "2")
    from app.core.config import get_settings

    get_settings.cache_clear()
    alice = await user(client, "alice")
    acme = await team(client, alice)
    sent = [await email_invite(client, alice, acme, email=f"p{i}@example.com") for i in range(3)]
    assert [r.status_code for r in sent] == [201, 201, 429]
    assert not outbox.to("p2@example.com")


async def test_a_new_person_signs_up_from_the_link_and_joins(client, outbox):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    await email_invite(client, alice, acme, role="admin")
    token = token_in(outbox)

    preview = (await client.post("/invites/public-preview", json={"token": token})).json()["data"]
    assert preview == {
        "workspace_name": "Acme", "invited_by": "alice", "role": "admin", "kind": "email", "email": INVITEE, "account_exists": False,
    }
    # the email is the invite's; a different one sent alongside is ignored
    resp = await client.post("/auth/signup/invite", json=NEW_ACCOUNT | {"token": token, "email": "evil@example.com"})
    data = resp.json()["data"]
    assert resp.status_code == 200 and data["workspace"]["id"] == acme and data["workspace"]["role"] == "admin"

    me = (await client.get("/me", headers={"Authorization": f"Bearer {data['access_token']}"})).json()["data"]
    assert (me["email"], me["username"]) == (INVITEE, "grace")
    assert (await mongo.users().find_one({"email": INVITEE}))["email_verified_at"]
    assert not await mongo.users().find_one({"email": "evil@example.com"})
    used = await client.post("/auth/signup/invite", json=NEW_ACCOUNT | {"token": token, "username": "grace2"})
    assert used.json()["meta"]["error_code"] == "invite_invalid"


async def test_someone_with_an_account_signs_in_and_accepts(client, outbox):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    grace = auth_headers(await verified_account(client, outbox))
    await mongo.users().update_one({"email": "ada@example.com"}, {"$set": {"email": INVITEE}})
    await email_invite(client, alice, acme)
    token = token_in(outbox)

    preview = (await client.post("/invites/public-preview", json={"token": token})).json()["data"]
    signup = await client.post("/auth/signup/invite", json=NEW_ACCOUNT | {"token": token})
    accepted = await client.post("/invites/accept", json={"token": token}, headers=bearer(grace))

    assert preview["account_exists"] is True
    assert signup.json()["meta"]["error_code"] == "account_exists"
    assert accepted.status_code == 200 and accepted.json()["data"]["id"] == acme


async def test_only_the_invited_address_may_accept(client, outbox):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    other = auth_headers(await verified_account(client, outbox))  # ada@example.com
    github_only = await user(client, "bob")
    await email_invite(client, alice, acme)
    token = token_in(outbox)

    for headers in (other, github_only):
        resp = await client.post("/invites/accept", json={"token": token}, headers=bearer(headers))
        assert resp.json()["meta"] == {
            "error_code": "invite_wrong_account", "message": "this invite is for g***@example.com", "invited": "g***@example.com",
        }


async def test_old_style_links_still_work(client):
    alice = await user(client, "alice")
    bob = await user(client, "bob")
    acme = await team(client, alice)
    url = (await client.post(f"/workspaces/{acme}/invites", json={}, headers=bearer(alice))).json()["data"]["url"]
    token = url.rsplit("#", 1)[1]

    assert url.endswith(f"/invite#{token}")
    assert (await client.post(f"/invites/{token}/accept", headers=bearer(bob))).status_code == 200


async def _link(client, headers: dict, profile: dict = PROFILE):
    resp = await client.post("/auth/github/link", json={"return_to": "onboarding"}, headers=bearer(headers))
    state = parse_qs(urlparse(resp.json()["data"]["authorization_url"]).query)["state"][0]
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(200, json=TOKENS))
        router.get(USER_URL).mock(return_value=Response(200, json=profile))
        return await client.get(f"/auth/github/callback?code=fake&state={state}")


async def test_an_email_account_connects_github_without_changing_its_session(client, outbox):
    ada = auth_headers(await verified_account(client, outbox))
    cookie = client.cookies["overwatch_refresh"]
    resp = await _link(client, ada)

    assert resp.headers["location"] == "http://localhost:3000/onboarding?github=linked"
    assert client.cookies["overwatch_refresh"] == cookie
    stored = await mongo.users().find_one({"email": "ada@example.com"})
    assert (stored["github_id"], stored["github_login"], stored["name"]) == (42, "octocat", "Ada Lovelace")
    assert await mongo.oauth_tokens().find_one({"owner_id": stored["_id"], "provider": "github"})

    # signing in with GitHub afterwards lands on the same account
    client.cookies.clear()
    access = await login(client)
    me = (await client.get("/me", headers={"Authorization": f"Bearer {access}"})).json()["data"]
    assert me["email"] == "ada@example.com"


async def test_a_github_account_can_only_belong_to_one_user(client, outbox):
    await login(client)  # octocat signs in with GitHub
    ada = auth_headers(await verified_account(client, outbox))
    resp = await _link(client, ada)
    assert resp.headers["location"].endswith("/onboarding?github=in_use")


async def test_a_forged_state_cookie_is_refused(client, outbox):
    ada = auth_headers(await verified_account(client, outbox))
    await client.post("/auth/github/link", json={}, headers=bearer(ada))
    client.cookies.set("overwatch_oauth_state", "not-encrypted", path="/auth")
    resp = await client.get("/auth/github/callback?code=fake&state=anything")
    assert resp.headers["location"].endswith("/sign-in?error=oauth_state_mismatch")


async def test_password_sign_in_keeps_working_for_two_users_without_github(client, outbox):
    await verified_account(client, outbox)
    second = await client.post("/auth/signup", json={"name": "B", "username": "bee", "email": "b@example.com", "password": PASSWORD})
    assert second.status_code == 202
