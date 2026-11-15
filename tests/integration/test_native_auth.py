"""Email and password accounts: sign-up with an emailed code, sign-in, password reset, usernames."""

import re
from datetime import timedelta

import pytest
import respx
from httpx import Response

from app.auth import passwords
from app.core.time import utcnow
from app.db import mongo
from tests.integration.test_auth_flow import login as github_login

EMAIL = "ada@example.com"
PASSWORD = "correct horse battery"
ACCOUNT = {"name": "Ada Lovelace", "username": "ada", "email": EMAIL, "password": PASSWORD}


def code_in(outbox, address: str = EMAIL) -> str:
    return re.search(r"\b(\d{6})\b", outbox.to(address)[-1].text).group(1)


async def signup(client, **changes) -> dict:
    resp = await client.post("/auth/signup", json=ACCOUNT | changes)
    assert resp.status_code == 202, resp.json()
    return resp.json()["data"]


async def verified_account(client, outbox) -> str:
    """Sign up and verify; returns the access token."""
    issued = await signup(client)
    resp = await client.post("/auth/email/verify", json={"challenge_id": issued["challenge_id"], "code": code_in(outbox)})
    assert resp.status_code == 200
    return resp.json()["data"]["access_token"]


async def expire_cooldown(challenge_id: str) -> None:
    await mongo.auth_challenges().update_one({"_id": challenge_id}, {"$set": {"sent_at": utcnow() - timedelta(minutes=2)}})


async def test_signing_up_and_verifying_signs_the_user_in(client, outbox):
    issued = await signup(client)
    assert issued["email"] == EMAIL and issued["resend_available_in"] > 0
    email = outbox.to(EMAIL)[0]
    assert email.subject == "Verify your email for Overwatch" and code_in(outbox) not in email.subject

    resp = await client.post("/auth/email/verify", json={"challenge_id": issued["challenge_id"], "code": code_in(outbox)})
    access = resp.json()["data"]["access_token"]
    me = (await client.get("/me", headers={"Authorization": f"Bearer {access}"})).json()["data"]

    assert "overwatch_refresh" in client.cookies
    assert me | {"id": None} == {
        "id": None, "username": "ada", "email": EMAIL, "github_login": None, "name": "Ada Lovelace", "avatar_url": None,
        "email_verified": True, "has_password": True,
    }
    assert await mongo.workspaces().find_one({"_id": me["id"], "personal": True})
    user = await mongo.users().find_one({"_id": me["id"]})
    assert user["email_verified_at"] and "unverified_expires_at" not in user
    assert user["password_hash"].startswith("$argon2id$")


async def test_an_existing_email_gets_the_same_answer_and_its_owner_is_told(client, outbox):
    await verified_account(client, outbox)
    again = await signup(client, username="someone-else")

    assert set(again) == {"challenge_id", "email", "resend_available_in"}
    assert outbox.to(EMAIL)[-1].subject == "You already have an Overwatch account"
    # the decoy never verifies, whatever is guessed
    wrong = await client.post("/auth/email/verify", json={"challenge_id": again["challenge_id"], "code": "123456"})
    assert wrong.json()["meta"]["error_code"] == "validation_error"


async def test_signing_up_again_before_verifying_replaces_the_code(client, outbox):
    first = await signup(client)
    old_code = code_in(outbox)
    second = await signup(client, password="another long password")

    stale = await client.post("/auth/email/verify", json={"challenge_id": first["challenge_id"], "code": old_code})
    fresh = await client.post("/auth/email/verify", json={"challenge_id": second["challenge_id"], "code": code_in(outbox)})

    assert stale.status_code == 422
    assert fresh.status_code == 200
    assert await mongo.users().count_documents({"email": EMAIL}) == 1


async def test_a_code_only_works_with_its_own_challenge(client, outbox):
    """Someone else's sign-up attempt for this email can't be verified with the code mailed for it."""
    attacker = await signup(client, password="attacker password 1")
    await signup(client, password="owner password 123")  # the owner signs up; the code goes to them

    resp = await client.post("/auth/email/verify", json={"challenge_id": attacker["challenge_id"], "code": code_in(outbox)})
    assert resp.status_code == 422


async def test_codes_allow_five_guesses_and_are_single_use(client, outbox):
    issued = await signup(client)
    body = {"challenge_id": issued["challenge_id"]}
    for _ in range(5):
        wrong = await client.post("/auth/email/verify", json=body | {"code": "000000" if code_in(outbox) != "000000" else "111111"})
        assert wrong.json()["meta"]["fields"] == {"code": "That code is incorrect or has expired."}
    locked = await client.post("/auth/email/verify", json=body | {"code": code_in(outbox)})
    assert locked.json()["meta"]["error_code"] == "otp_attempts_exceeded"

    # a new code resets the attempts; once used it's gone
    await expire_cooldown(issued["challenge_id"])
    await client.post("/auth/otp/resend", json=body)
    used = await client.post("/auth/email/verify", json=body | {"code": code_in(outbox)})
    reused = await client.post("/auth/email/verify", json=body | {"code": code_in(outbox)})
    assert used.status_code == 200 and reused.status_code == 422


async def test_an_expired_code_is_refused(client, outbox):
    issued = await signup(client)
    await mongo.auth_challenges().update_one({"_id": issued["challenge_id"]}, {"$set": {"code_expires_at": utcnow() - timedelta(seconds=1)}})
    resp = await client.post("/auth/email/verify", json={"challenge_id": issued["challenge_id"], "code": code_in(outbox)})
    assert resp.status_code == 422


async def test_resending_waits_out_a_cooldown_and_is_capped(client, outbox):
    issued = await signup(client)
    body = {"challenge_id": issued["challenge_id"]}
    too_soon = await client.post("/auth/otp/resend", json=body)
    assert too_soon.json()["meta"]["error_code"] == "otp_resend_cooldown"
    assert too_soon.json()["meta"]["retry_after_seconds"] > 0

    for _ in range(5):
        await expire_cooldown(issued["challenge_id"])
        assert (await client.post("/auth/otp/resend", json=body)).status_code == 202
    await expire_cooldown(issued["challenge_id"])
    capped = await client.post("/auth/otp/resend", json=body)
    assert capped.json()["meta"]["error_code"] == "otp_resend_limit"
    assert len(outbox.to(EMAIL)) == 6


async def test_signing_in(client, outbox):
    await verified_account(client, outbox)
    client.cookies.clear()

    ok = await client.post("/auth/login", json={"email": "ADA@example.com ", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["data"]["access_token"]
    assert (await client.post("/auth/refresh")).status_code == 200


async def test_every_failed_sign_in_reads_the_same(client, outbox):
    await verified_account(client, outbox)
    await github_login(client)  # a GitHub-only account has no password to match
    wrong = await client.post("/auth/login", json={"email": EMAIL, "password": "not the password"})
    unknown = await client.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["meta"] == unknown.json()["meta"] == {"error_code": "invalid_credentials", "message": "Invalid email or password."}


async def test_signing_in_unverified_sends_a_fresh_code_only_after_the_right_password(client, outbox):
    issued = await signup(client)
    await expire_cooldown(issued["challenge_id"])

    resp = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    meta = resp.json()["meta"]
    assert resp.status_code == 403 and meta["error_code"] == "email_not_verified"
    assert meta["challenge_id"] != issued["challenge_id"] and meta["email"] == EMAIL
    assert len(outbox.to(EMAIL)) == 2
    verified = await client.post("/auth/email/verify", json={"challenge_id": meta["challenge_id"], "code": code_in(outbox)})
    assert verified.status_code == 200


async def test_unverified_accounts_expire(client, outbox):
    await signup(client)
    user = await mongo.users().find_one({"email": EMAIL})
    assert timedelta(days=6) < user["unverified_expires_at"] - utcnow() <= timedelta(days=7)
    indexes = await mongo.users().index_information()
    assert indexes["unverified_expires_at_1"]["expireAfterSeconds"] == 0


async def test_too_many_sign_ins_are_slowed_down(client):
    for _ in range(10):
        await client.post("/auth/login", json={"email": EMAIL, "password": "wrong password"})
    resp = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 429 and resp.json()["meta"]["error_code"] == "rate_limited"


async def test_password_rules_come_back_on_their_field(client, monkeypatch):
    short = await client.post("/auth/signup", json=ACCOUNT | {"password": "short"})
    with_email = await client.post("/auth/signup", json=ACCOUNT | {"password": "ada-is-my-password"})
    bad_email = await client.post("/auth/signup", json=ACCOUNT | {"email": "not-an-email"})

    async def breached(password: str) -> bool:
        return True

    monkeypatch.setattr(passwords, "is_breached", breached)
    leaked = await client.post("/auth/signup", json=ACCOUNT)

    assert short.json()["meta"]["fields"] == {"password": "Use at least 8 characters"}
    assert with_email.json()["meta"]["fields"] == {"password": "Don't include your email address in your password"}
    assert set(bad_email.json()["meta"]["fields"]) == {"email"}
    assert "data breach" in leaked.json()["meta"]["fields"]["password"]


async def test_forgot_password_resets_and_signs_out_everywhere(client, outbox):
    await verified_account(client, outbox)
    old_cookie = client.cookies["overwatch_refresh"]
    unknown = await client.post("/auth/password/forgot", json={"email": "nobody@example.com"})
    issued = (await client.post("/auth/password/forgot", json={"email": EMAIL})).json()["data"]

    assert unknown.status_code == 202 and not outbox.to("nobody@example.com")
    assert outbox.to(EMAIL)[-1].subject == "Reset your Overwatch password"
    reset = await client.post("/auth/password/reset", json={
        "challenge_id": issued["challenge_id"], "code": code_in(outbox), "password": "a brand new password",
    })
    assert reset.status_code == 200

    client.cookies.set("overwatch_refresh", old_cookie, path="/auth")
    assert (await client.post("/auth/refresh")).status_code == 401
    old = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    new = await client.post("/auth/login", json={"email": EMAIL, "password": "a brand new password"})
    assert old.status_code == 401 and new.status_code == 200


async def test_session_endpoints_refuse_other_origins(client):
    resp = await client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


@pytest.mark.parametrize(("username", "reason"), [("ada", "taken"), ("admin", "reserved"), ("a", "invalid"), ("a--b", "invalid"), ("New_One", None)])
async def test_username_availability(client, outbox, username, reason):
    await verified_account(client, outbox)
    resp = await client.get("/auth/username-availability", params={"username": username})
    data = resp.json()["data"]
    assert (data["available"], data["reason"]) == (reason is None, reason)
    assert resp.headers["cache-control"] == "no-store"


async def test_a_taken_username_comes_back_on_its_field(client, outbox):
    await verified_account(client, outbox)
    resp = await client.post("/auth/signup", json=ACCOUNT | {"email": "grace@example.com", "username": "ADA"})
    assert resp.json()["meta"]["fields"] == {"username": "That username is taken"}


async def test_github_users_get_their_login_as_username_or_choose_one_once(client):
    access = await github_login(client)
    headers = {"Authorization": f"Bearer {access}"}
    assert (await client.get("/me", headers=headers)).json()["data"]["username"] == "octocat"

    await mongo.users().update_many({}, {"$set": {"username": None}})
    first = await client.put("/me/username", json={"username": "Octo"}, headers=headers)
    second = await client.put("/me/username", json={"username": "octo2"}, headers=headers)
    assert first.json()["data"]["username"] == "octo"
    assert second.json()["meta"]["error_code"] == "username_already_set"


async def test_hibp_is_asked_with_a_hash_prefix_and_failures_let_the_password_through(monkeypatch):
    monkeypatch.setenv("HIBP_ENABLED", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    digest = "CBFDAC6008F9CAB4083784CBD1874F76618D2A97"  # sha1("password123")
    with respx.mock as router:
        route = router.get(f"https://api.pwnedpasswords.com/range/{digest[:5]}").mock(
            return_value=Response(200, text=f"{digest[5:]}:42\r\n0000000000000000000000000000000000A:0"),
        )
        assert await passwords.is_breached("password123")
        assert route.calls.last.request.headers["add-padding"] == "true"
        route.mock(return_value=Response(503))
        assert not await passwords.is_breached("password123")
