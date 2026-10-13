"""Workspaces: personal workspace on sign-in, roles, invites and isolation between workspaces."""

from datetime import timedelta

import pytest

from app.core import security
from app.core.time import utcnow
from app.db import mongo
from app.workspaces import repository as workspaces
from tests.integration.test_auth_flow import auth_headers, login

REPO = "octocat/hello-world"


async def user(client, login_name: str) -> dict:
    """A signed-in user other than the default one; returns headers for their personal workspace."""
    doc = {"_id": f"id-{login_name}", "github_id": hash(login_name), "github_login": login_name, "name": None}
    await mongo.users().insert_one(doc)
    await workspaces.ensure_personal(doc)
    return auth_headers(security.issue_access_token(doc["_id"]))


def bearer(headers: dict) -> dict:
    return {"Authorization": headers["Authorization"]}


def in_workspace(headers: dict, workspace_id: str) -> dict:
    return {**headers, "X-Workspace-Id": workspace_id}


async def team(client, owner: dict, name: str = "Acme") -> str:
    resp = await client.post("/workspaces", json={"name": name}, headers=bearer(owner))
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


async def invite(client, admin: dict, workspace_id: str, **body) -> str:
    resp = await client.post(f"/workspaces/{workspace_id}/invites", json=body, headers=bearer(admin))
    assert resp.status_code == 201
    return resp.json()["data"]["url"].rsplit("/", 1)[1]


async def join(client, owner: dict, member: dict, workspace_id: str, role: str = "member") -> None:
    token = await invite(client, owner, workspace_id, role=role)
    assert (await client.post(f"/invites/{token}/accept", headers=bearer(member))).status_code == 200


async def test_sign_in_creates_one_personal_workspace(client):
    await login(client)
    await login(client)

    access = await login(client)
    resp = await client.get("/workspaces", headers=bearer(auth_headers(access)))

    assert [(w["name"], w["personal"], w["role"]) for w in resp.json()["data"]] == [("Octo Cat", True, "owner")]


async def test_workspace_routes_need_the_header(client):
    access = await login(client)
    resp = await client.get("/github/repos/connected", headers=bearer(auth_headers(access)))
    assert resp.json()["meta"]["error_code"] == "workspace_required"


async def test_a_non_member_cannot_tell_a_workspace_exists(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)

    for resp in (
        await client.get(f"/workspaces/{acme}", headers=bearer(bob)),
        await client.get("/github/repos/connected", headers=in_workspace(bob, acme)),
    ):
        assert resp.status_code == 404
        assert resp.json()["meta"]["error_code"] == "workspace_not_found"


async def test_repos_belong_to_the_workspace_not_the_user(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await mongo.repos().insert_one({"workspace_id": acme, "repo_full_name": REPO, "default_branch": "main", "parse_status": "done"})

    shared = await client.get("/github/repos/connected", headers=in_workspace(bob, acme))
    personal = await client.get("/github/repos/connected", headers=bob)

    assert [r["repo_full_name"] for r in shared.json()["data"]] == [REPO]
    assert personal.json()["data"] == []


async def test_a_repo_connected_in_another_workspace_is_not_readable(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    await mongo.repos().insert_one({"workspace_id": alice["X-Workspace-Id"], "repo_full_name": REPO, "default_branch": "main", "parse_status": "done"})

    resp = await client.get(f"/graph?repo_full_name={REPO}", headers=bob)

    assert resp.json()["meta"]["error_code"] == "repo_not_connected"


async def test_only_admins_connect_and_disconnect_repos(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)

    connect = await client.post("/github/repos/connect", json={"repo_full_name": REPO}, headers=in_workspace(bob, acme))
    disconnect = await client.delete(f"/github/repos/{REPO}", headers=in_workspace(bob, acme))

    assert [connect.status_code, disconnect.status_code] == [403, 403]
    assert connect.json()["meta"]["error_code"] == "insufficient_role"


async def test_admins_rename_and_owners_delete(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme, role="admin")

    renamed = await client.patch(f"/workspaces/{acme}", json={"name": "Acme Inc"}, headers=bearer(bob))
    refused = await client.delete(f"/workspaces/{acme}", headers=bearer(bob))
    deleted = await client.delete(f"/workspaces/{acme}", headers=bearer(alice))

    assert renamed.json()["data"]["name"] == "Acme Inc"
    assert refused.status_code == 403
    assert deleted.status_code == 200
    assert (await client.get(f"/workspaces/{acme}", headers=bearer(alice))).status_code == 404


async def test_deleting_a_workspace_keeps_its_data(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    await mongo.repos().insert_one({"workspace_id": acme, "repo_full_name": REPO, "default_branch": "main", "parse_status": "done"})

    await client.delete(f"/workspaces/{acme}", headers=bearer(alice))

    assert await mongo.workspaces().find_one({"_id": acme, "deleted_at": {"$ne": None}})
    assert await mongo.repos().count_documents({"workspace_id": acme}) == 1
    assert [w["id"] for w in (await client.get("/workspaces", headers=bearer(alice))).json()["data"]] == [alice["X-Workspace-Id"]]


async def test_a_personal_workspace_cannot_be_deleted_or_left(client):
    alice = await user(client, "alice")
    personal = alice["X-Workspace-Id"]

    deleted = await client.delete(f"/workspaces/{personal}", headers=bearer(alice))
    left = await client.delete(f"/workspaces/{personal}/members/{personal}", headers=bearer(alice))

    assert [deleted.json()["meta"]["error_code"], left.json()["meta"]["error_code"]] == ["personal_workspace"] * 2


async def test_the_last_owner_cannot_leave_or_be_demoted(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)

    left = await client.delete(f"/workspaces/{acme}/members/id-alice", headers=bearer(alice))
    demoted = await client.patch(f"/workspaces/{acme}/members/id-alice", json={"role": "admin"}, headers=bearer(alice))

    assert [left.json()["meta"]["error_code"], demoted.json()["meta"]["error_code"]] == ["last_owner"] * 2


async def test_an_admin_cannot_touch_an_owner_or_grant_ownership(client):
    alice, bob, carol = await user(client, "alice"), await user(client, "bob"), await user(client, "carol")
    acme = await team(client, alice)
    await join(client, alice, bob, acme, role="admin")
    await join(client, alice, carol, acme)

    for resp in (
        await client.patch(f"/workspaces/{acme}/members/id-carol", json={"role": "owner"}, headers=bearer(bob)),
        await client.patch(f"/workspaces/{acme}/members/id-alice", json={"role": "member"}, headers=bearer(bob)),
        await client.delete(f"/workspaces/{acme}/members/id-alice", headers=bearer(bob)),
    ):
        assert resp.status_code == 403

    promoted = await client.patch(f"/workspaces/{acme}/members/id-carol", json={"role": "admin"}, headers=bearer(bob))
    assert promoted.status_code == 200


async def test_a_member_can_leave_but_not_remove_others(client):
    alice, bob, carol = await user(client, "alice"), await user(client, "bob"), await user(client, "carol")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await join(client, alice, carol, acme)

    removed = await client.delete(f"/workspaces/{acme}/members/id-carol", headers=bearer(bob))
    left = await client.delete(f"/workspaces/{acme}/members/id-bob", headers=bearer(bob))

    assert [removed.status_code, left.status_code] == [403, 200]
    members = (await client.get(f"/workspaces/{acme}/members", headers=bearer(alice))).json()["data"]
    assert [m["github_login"] for m in members] == ["alice", "carol"]


async def test_an_invite_link_works_once(client):
    alice, bob, carol = await user(client, "alice"), await user(client, "bob"), await user(client, "carol")
    acme = await team(client, alice)
    token = await invite(client, alice, acme, role="admin")

    preview = await client.get(f"/invites/{token}", headers=bearer(bob))
    accepted = await client.post(f"/invites/{token}/accept", headers=bearer(bob))
    reused = await client.post(f"/invites/{token}/accept", headers=bearer(carol))

    assert preview.json()["data"] == {"workspace_name": "Acme", "invited_by": "alice", "role": "admin"}
    assert accepted.json()["data"] == {"id": acme, "name": "Acme", "personal": False, "role": "admin"}
    assert reused.json()["meta"]["error_code"] == "invite_invalid"


async def test_an_existing_member_keeps_their_role_and_the_link(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    token = await invite(client, alice, acme)

    resp = await client.post(f"/invites/{token}/accept", headers=bearer(alice))

    assert resp.json()["data"]["role"] == "owner"
    assert (await client.post(f"/invites/{token}/accept", headers=bearer(bob))).status_code == 200


async def test_an_invite_locked_to_an_account_refuses_others(client):
    alice, bob, carol = await user(client, "alice"), await user(client, "bob"), await user(client, "carol")
    acme = await team(client, alice)
    token = await invite(client, alice, acme, github_login="Bob")

    refused = await client.post(f"/invites/{token}/accept", headers=bearer(carol))
    accepted = await client.post(f"/invites/{token}/accept", headers=bearer(bob))

    assert refused.json()["meta"]["error_code"] == "invite_wrong_account"
    assert accepted.status_code == 200


@pytest.mark.parametrize("spoil", ["revoke", "expire", "delete_workspace"])
async def test_a_dead_invite_is_refused(client, spoil):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    token = await invite(client, alice, acme)
    invite_id = (await client.get(f"/workspaces/{acme}/invites", headers=bearer(alice))).json()["data"][0]["id"]

    if spoil == "revoke":
        await client.delete(f"/workspaces/{acme}/invites/{invite_id}", headers=bearer(alice))
    elif spoil == "expire":
        await mongo.workspace_invites().update_one({"_id": invite_id}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}})
    else:
        await client.delete(f"/workspaces/{acme}", headers=bearer(alice))

    resp = await client.post(f"/invites/{token}/accept", headers=bearer(bob))
    assert resp.json()["meta"]["error_code"] == "invite_invalid"


async def test_members_cannot_invite_and_admins_cannot_invite_owners(client):
    alice, bob, carol = await user(client, "alice"), await user(client, "bob"), await user(client, "carol")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await join(client, alice, carol, acme, role="admin")

    by_member = await client.post(f"/workspaces/{acme}/invites", json={}, headers=bearer(bob))
    as_owner = await client.post(f"/workspaces/{acme}/invites", json={"role": "owner"}, headers=bearer(carol))

    assert [by_member.status_code, as_owner.status_code] == [403, 422]


async def test_the_invite_token_is_stored_only_as_a_hash(client):
    alice = await user(client, "alice")
    token = await invite(client, alice, await team(client, alice))

    doc = await mongo.workspace_invites().find_one({})
    assert doc["token_hash"] == security.hash_refresh_token(token)
    assert token not in str(doc)
