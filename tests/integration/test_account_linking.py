"""Adding a sign-in method, merging two accounts one person owns, and removing a method again."""

import re
from datetime import timedelta

import pytest
from fastapi import BackgroundTasks

from app.accounts import merge as engine
from app.accounts import service as accounts
from app.core.constants import KeepProfile
from app.core.time import utcnow
from app.db import mongo
from tests.integration.test_auth_flow import PROFILE, auth_headers, login
from tests.integration.test_email_invites import _link, email_invite, token_in
from tests.integration.test_native_auth import EMAIL, PASSWORD, code_in, verified_account
from tests.integration.test_workspaces import bearer, join, team, user

NEW_PASSWORD = "another long passphrase"


async def add_email(client, headers: dict, email: str = EMAIL, password: str = NEW_PASSWORD):
    return await client.post("/me/email", json={"email": email, "password": password}, headers=bearer(headers))


async def verify_added(client, headers: dict, challenge_id: str, code: str):
    return await client.post("/me/email/verify", json={"challenge_id": challenge_id, "code": code}, headers=bearer(headers))


async def prove_email(client, outbox, headers: dict, email: str = EMAIL) -> dict:
    issued = (await add_email(client, headers, email)).json()["data"]
    return (await verify_added(client, headers, issued["challenge_id"], code_in(outbox, email))).json()["data"]


async def me(client, headers: dict) -> dict:
    return (await client.get("/me", headers=bearer(headers))).json()["data"]


async def sign_in(client, email: str, password: str):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def no_references_to(user_id: str) -> None:
    for collection, field in engine.USER_FIELDS:
        assert await collection().count_documents({field: user_id}) == 0, f"{collection.__name__}.{field}"
    for collection, person in engine.PEOPLE:
        assert await collection().count_documents({f"{person}.id": user_id}) == 0, f"{collection.__name__}.{person}"
    assert await mongo.workspace_members().count_documents({"user_id": user_id}) == 0
    assert await mongo.oauth_tokens().count_documents({"owner_id": user_id, "provider": "github"}) == 0
    assert await mongo.users().find_one({"_id": user_id}) is None


async def test_a_github_user_adds_an_email_and_password(client, outbox):
    alice = await user(client, "alice")
    issued = await add_email(client, alice)
    assert issued.status_code == 202 and set(issued.json()["data"]) == {"challenge_id", "email", "resend_available_in"}
    # the address isn't on the account until the code proves it, so the unverified clean-up can't touch it
    stored = await mongo.users().find_one({"_id": "id-alice"})
    assert "email" not in stored and "unverified_expires_at" not in stored

    added = await verify_added(client, alice, issued.json()["data"]["challenge_id"], code_in(outbox))
    data = added.json()["data"]
    assert data["outcome"] == "added" and data["me"]["email"] == EMAIL and data["me"]["has_password"]
    signed_in = await sign_in(client, EMAIL, NEW_PASSWORD)
    assert auth_headers(signed_in.json()["data"]["access_token"])["X-Workspace-Id"] == "id-alice"
    assert await mongo.workspaces().find_one({"_id": "id-alice", "personal": True})
    assert "unverified_expires_at" not in await mongo.users().find_one({"_id": "id-alice"})


async def test_the_answer_doesnt_say_whether_the_address_has_an_account(client, outbox):
    await verified_account(client, outbox)
    alice, bob = await user(client, "alice"), await user(client, "bob")
    taken = await add_email(client, alice, EMAIL)
    free = await add_email(client, bob, "free@example.com")
    assert (taken.status_code, set(taken.json()["data"])) == (free.status_code, set(free.json()["data"]))


async def test_only_the_account_that_asked_can_use_its_code(client, outbox):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    issued = (await add_email(client, alice)).json()["data"]
    stolen = await verify_added(client, bob, issued["challenge_id"], code_in(outbox))
    assert stolen.json()["meta"]["fields"]["code"]
    assert (await me(client, bob))["email"] is None


async def test_a_resent_code_goes_to_the_address_being_added(client, outbox):
    alice = await user(client, "alice")
    issued = (await add_email(client, alice)).json()["data"]
    await mongo.auth_challenges().update_one({"_id": issued["challenge_id"]}, {"$set": {"sent_at": utcnow() - timedelta(minutes=2)}})
    assert (await client.post("/auth/otp/resend", json={"challenge_id": issued["challenge_id"]})).status_code == 202
    assert len(outbox.to(EMAIL)) == 2


async def test_an_unverified_signup_for_the_address_gives_way(client, outbox):
    await client.post("/auth/signup", json={"name": "Squatter", "username": "squat", "email": EMAIL, "password": PASSWORD})
    squatter = await mongo.users().find_one({"email": EMAIL})
    alice = await user(client, "alice")
    assert (await prove_email(client, outbox, alice))["outcome"] == "added"
    assert await mongo.users().find_one({"_id": squatter["_id"]}) is None


async def test_an_account_with_an_email_cant_add_another(client, outbox):
    ada = auth_headers(await verified_account(client, outbox))
    resp = await add_email(client, ada, "second@example.com")
    assert resp.json()["meta"]["error_code"] == "email_already_set"


async def test_proving_another_accounts_email_merges_it_in(client, outbox):
    ada = auth_headers(await verified_account(client, outbox))
    ada_id = ada["X-Workspace-Id"]
    alice = await user(client, "alice")
    acme = await team(client, alice)
    await join(client, alice, ada, acme, role="admin")
    await mongo.work_items().insert_one({"_id": "item-1", "workspace_id": ada_id, "created_by": ada_id, "assignee": {"id": ada_id, "name": "Ada Lovelace"}})

    proposed = await prove_email(client, outbox, alice)
    assert proposed["outcome"] == "merge_required"
    preview = proposed["merge"]
    assert preview["other"]["username"] == "ada" and preview["other"]["email"] == EMAIL
    assert preview["moves"]["workspaces"] == 2 and preview["moves"]["work_items"] == 1
    assert (await client.get("/me/merge", headers=bearer(alice))).json()["data"] == preview

    merged = await client.post("/me/merge", json={"keep_profile": "other"}, headers=bearer(alice))
    assert merged.status_code == 200
    assert (merged.json()["data"]["id"], merged.json()["data"]["username"]) == ("id-alice", "ada")

    await no_references_to(ada_id)
    # the higher role of the two in a workspace both were in
    assert (await mongo.workspace_members().find_one({"workspace_id": acme, "user_id": "id-alice"}))["role"] == "owner"
    kept = await mongo.workspaces().find_one({"_id": ada_id})
    assert kept["personal"] is False and kept["name"].endswith("(from @ada)")
    assert await mongo.workspace_members().find_one({"workspace_id": ada_id, "user_id": "id-alice", "role": "owner"})
    assert (await mongo.work_items().find_one({"_id": "item-1"}))["assignee"] == {"id": "id-alice", "name": "Ada Lovelace"}
    # ada's own password still signs in, now to the merged account
    assert auth_headers((await sign_in(client, EMAIL, PASSWORD)).json()["data"]["access_token"])["X-Workspace-Id"] == "id-alice"
    # the absorbed account's leftover access token reaches nothing
    assert (await client.get("/me", headers=bearer(ada))).status_code == 401
    record = await mongo.account_merges().find_one({"absorbed_id": ada_id})
    assert record["status"] == "done" and "password_hash" not in record["absorbed_snapshot"]
    assert any("merged" in mail.subject for mail in outbox.to(EMAIL))


async def test_connecting_a_github_account_that_has_its_own_account_offers_a_merge(client, outbox):
    await login(client)  # octocat signs in with GitHub first
    octocat = await mongo.users().find_one({"github_id": PROFILE["id"]})
    ada = auth_headers(await verified_account(client, outbox))
    resp = await _link(client, ada)
    assert resp.headers["location"].endswith("/settings/profile?github=merge")

    merged = await client.post("/me/merge", json={}, headers=bearer(ada))
    assert merged.json()["data"]["github_login"] == "octocat" and merged.json()["data"]["username"] == "ada"
    await no_references_to(octocat["_id"])
    assert await mongo.oauth_tokens().find_one({"owner_id": ada["X-Workspace-Id"], "provider": "github"})

    # signing in with GitHub now lands on the merged account
    client.cookies.clear()
    access = await login(client)
    assert (await client.get("/me", headers={"Authorization": f"Bearer {access}"})).json()["data"]["email"] == EMAIL


async def test_accounts_with_different_logins_of_one_kind_cant_merge(client, outbox):
    await login(client)
    ada = auth_headers(await verified_account(client, outbox))
    await mongo.users().update_one({"email": EMAIL}, {"$set": {"github_id": 7, "github_login": "someone"}})
    resp = await _link(client, ada)
    assert resp.headers["location"].endswith("?github=in_use")
    assert (await client.get("/me/merge", headers=bearer(ada))).json()["meta"]["error_code"] == "merge_not_found"


async def test_a_proposal_is_used_once_and_only_by_its_account(client, outbox):
    await verified_account(client, outbox)
    alice, bob = await user(client, "alice"), await user(client, "bob")
    await prove_email(client, outbox, alice)
    assert (await client.post("/me/merge", json={}, headers=bearer(bob))).json()["meta"]["error_code"] == "merge_not_found"
    assert (await client.post("/me/merge", json={}, headers=bearer(alice))).status_code == 200
    assert (await client.post("/me/merge", json={}, headers=bearer(alice))).json()["meta"]["error_code"] == "merge_not_found"


async def test_a_cancelled_proposal_is_gone(client, outbox):
    await verified_account(client, outbox)
    alice = await user(client, "alice")
    await prove_email(client, outbox, alice)
    assert (await client.delete("/me/merge", headers=bearer(alice))).status_code == 200
    assert (await client.get("/me/merge", headers=bearer(alice))).json()["meta"]["error_code"] == "merge_not_found"


async def test_an_interrupted_merge_is_finished_later(client, outbox, monkeypatch):
    ada = auth_headers(await verified_account(client, outbox))
    alice = await user(client, "alice")
    await prove_email(client, outbox, alice)
    original = engine._move_memberships

    async def crash(*args):
        raise RuntimeError("the process stopped")

    monkeypatch.setattr(engine, "_move_memberships", crash)
    survivor = await mongo.users().find_one({"_id": "id-alice"})
    with pytest.raises(RuntimeError):
        await accounts.confirm(BackgroundTasks(), survivor, KeepProfile.CURRENT, "1.2.3.4")
    # held while unfinished: neither account can sign in or start another merge
    assert (await sign_in(client, EMAIL, PASSWORD)).json()["meta"]["error_code"] == "account_merging"

    monkeypatch.setattr(engine, "_move_memberships", original)
    monkeypatch.setattr(engine, "STALL_AFTER", timedelta(seconds=-1))
    await engine.resume_stalled()
    await no_references_to(ada["X-Workspace-Id"])
    finished = await mongo.users().find_one({"_id": "id-alice"})
    assert finished["email"] == EMAIL and "merge_id" not in finished


async def test_removing_a_sign_in_method_needs_another_and_the_password(client, outbox):
    alice = await user(client, "alice")
    only_github = await client.request("DELETE", "/me/github", json={"password": "x"}, headers=bearer(alice))
    assert only_github.json()["meta"]["error_code"] == "last_login_method"

    await prove_email(client, outbox, alice)
    wrong = await client.request("DELETE", "/me/github", json={"password": "wrong password"}, headers=bearer(alice))
    assert wrong.json()["meta"]["error_code"] == "invalid_credentials"
    unlinked = await client.request("DELETE", "/me/github", json={"password": NEW_PASSWORD}, headers=bearer(alice))
    assert unlinked.json()["data"]["github_login"] is None

    only_email = await client.request("DELETE", "/me/email", json={"password": NEW_PASSWORD}, headers=bearer(alice))
    assert only_email.json()["meta"]["error_code"] == "last_login_method"


async def test_removing_the_email_keeps_github(client, outbox):
    alice = await user(client, "alice")
    await prove_email(client, outbox, alice)
    removed = await client.request("DELETE", "/me/email", json={"password": NEW_PASSWORD}, headers=bearer(alice))
    assert removed.json()["data"]["email"] is None and removed.json()["data"]["has_password"] is False
    assert (await sign_in(client, EMAIL, NEW_PASSWORD)).status_code == 401


async def test_a_github_user_adds_the_invited_address_and_joins(client, outbox):
    owner = await user(client, "owner")
    acme = await team(client, owner)
    await email_invite(client, owner, acme)
    token = token_in(outbox)
    alice = await user(client, "alice")
    wrong = await client.post("/invites/accept", json={"token": token}, headers=bearer(alice))
    assert wrong.json()["meta"]["error_code"] == "invite_wrong_account"

    assert (await prove_email(client, outbox, alice, "grace@example.com"))["outcome"] == "added"
    joined = await client.post("/invites/accept", json={"token": token}, headers=bearer(alice))
    assert joined.status_code == 200 and joined.json()["data"]["id"] == acme
    assert re.search(r"/invite#", outbox.to("grace@example.com")[0].text)
