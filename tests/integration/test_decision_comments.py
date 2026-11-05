"""A decision's discussion: every member of its workspace reads and writes it; authors edit, authors or admins delete."""

import uuid

from app.db import mongo
from app.decision_comments.schemas import MAX_REACTIONS
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_workspaces import in_workspace, join, team, user


async def workspace_of_three(client) -> tuple[dict, dict, dict, str]:
    """An owner, a member and an admin in one team workspace, with a decision there."""
    owner = auth_headers(await login(client))
    workspace_id = await team(client, owner)
    member, admin = await user(client, "sara"), await user(client, "ali")
    await join(client, owner, member, workspace_id)
    await join(client, owner, admin, workspace_id, role="admin")
    owner, member, admin = (in_workspace(h, workspace_id) for h in (owner, member, admin))
    decision = (await client.post("/decisions", json={"title": "Guest export", "statement": "No."}, headers=owner)).json()["data"]
    return owner, member, admin, decision["id"]


async def send(client, headers: dict, decision_id: str, body: str, client_id: str | None = None):
    return await client.post(
        f"/decisions/{decision_id}/comments", json={"client_id": client_id or uuid.uuid4().hex, "body": body}, headers=headers,
    )


async def test_members_send_and_read_each_others_messages_in_order(client):
    owner, member, _, decision_id = await workspace_of_three(client)

    first = (await send(client, owner, decision_id, "  Does this cover **CSV**?  ")).json()["data"]
    await send(client, member, decision_id, "Yes, CSV too.")
    listed = await client.get(f"/decisions/{decision_id}/comments", headers=member)

    assert (first["body"], first["author"]["name"], first["version"], first["edited"]) == ("Does this cover **CSV**?", "Octo Cat", 1, False)
    assert [m["body"] for m in listed.json()["data"]] == ["Does this cover **CSV**?", "Yes, CSV too."]
    assert listed.json()["meta"]["has_more"] is False


async def test_a_retried_send_isnt_posted_twice(client):
    owner, _, _, decision_id = await workspace_of_three(client)
    client_id = uuid.uuid4().hex

    first = (await send(client, owner, decision_id, "Once", client_id)).json()["data"]
    again = await send(client, owner, decision_id, "Once", client_id)

    assert again.status_code == 201 and again.json()["data"]["id"] == first["id"]
    assert await mongo.decision_comments().count_documents({}) == 1


async def test_the_discussion_pages_back_from_the_newest(client):
    owner, _, _, decision_id = await workspace_of_three(client)
    for n in range(5):
        await send(client, owner, decision_id, f"m{n}")

    newest = await client.get(f"/decisions/{decision_id}/comments", params={"limit": 2}, headers=owner)
    older = await client.get(
        f"/decisions/{decision_id}/comments", params={"limit": 2, "before": newest.json()["data"][0]["id"]}, headers=owner,
    )
    oldest = await client.get(
        f"/decisions/{decision_id}/comments", params={"limit": 2, "before": older.json()["data"][0]["id"]}, headers=owner,
    )

    assert [m["body"] for m in newest.json()["data"]] == ["m3", "m4"]
    assert [m["body"] for m in older.json()["data"]] == ["m1", "m2"]
    assert ([m["body"] for m in oldest.json()["data"]], oldest.json()["meta"]["has_more"]) == (["m0"], False)


async def test_only_the_author_edits_and_the_author_or_an_admin_deletes(client):
    owner, member, admin, decision_id = await workspace_of_three(client)
    mine = (await send(client, member, decision_id, "Draft")).json()["data"]
    theirs = (await send(client, owner, decision_id, "Owner's")).json()["data"]
    url = f"/decisions/{decision_id}/comments"

    edited = await client.patch(f"{url}/{mine['id']}", json={"body": "Final"}, headers=member)
    not_mine = await client.patch(f"{url}/{theirs['id']}", json={"body": "x"}, headers=member)
    refused = await client.delete(f"{url}/{theirs['id']}", headers=member)
    by_admin = await client.delete(f"{url}/{mine['id']}", headers=admin)
    gone = await client.patch(f"{url}/{mine['id']}", json={"body": "again"}, headers=member)

    assert (edited.json()["data"]["body"], edited.json()["data"]["version"], edited.json()["data"]["edited"]) == ("Final", 2, True)
    assert (not_mine.status_code, not_mine.json()["meta"]["error_code"]) == (403, "not_comment_author")
    assert refused.status_code == 403
    assert by_admin.status_code == 200
    assert (gone.status_code, gone.json()["meta"]["error_code"]) == (404, "comment_not_found")


async def test_another_workspaces_decision_and_a_blank_message_are_refused(client):
    owner, _, _, decision_id = await workspace_of_three(client)
    outsider = await user(client, "zed")

    other = await send(client, outsider, decision_id, "Hi")
    listed = await client.get(f"/decisions/{decision_id}/comments", headers=outsider)
    blank = await send(client, owner, decision_id, "   ")
    bad_id = await client.post(f"/decisions/{decision_id}/comments", json={"client_id": "x", "body": "Hi"}, headers=owner)

    assert (other.status_code, other.json()["meta"]["error_code"]) == (404, "decision_not_found")
    assert listed.status_code == 404
    assert (blank.status_code, bad_id.status_code) == (422, 422)


async def test_dismissing_a_suggestion_removes_its_discussion(client):
    owner, _, _, decision_id = await workspace_of_three(client)
    await send(client, owner, decision_id, "Is this real?")
    await mongo.decisions().update_one({"_id": decision_id}, {"$set": {"origin": "suggested", "status": "proposed"}})

    dismissed = await client.post(f"/decisions/{decision_id}/dismiss", headers=owner)

    assert dismissed.status_code == 200
    assert await mongo.decision_comments().count_documents({}) == 0


async def test_reactions_toggle_per_member_in_the_order_first_used(client):
    owner, member, _, decision_id = await workspace_of_three(client)
    sent = (await send(client, owner, decision_id, "Ship it?")).json()["data"]
    url = f"/decisions/{decision_id}/comments/{sent['id']}/reactions"

    await client.put(f"{url}/👍", headers=owner)
    await client.put(f"{url}/🎉", headers=member)
    again = await client.put(f"{url}/👍", headers=owner)
    joined = await client.put(f"{url}/👍", headers=member)
    off = await client.delete(f"{url}/🎉", headers=member)

    # reacting again with the same emoji changes nothing
    assert [(r["emoji"], len(r["user_ids"])) for r in again.json()["data"]["reactions"]] == [("👍", 1), ("🎉", 1)]
    assert [(r["emoji"], len(r["user_ids"])) for r in joined.json()["data"]["reactions"]] == [("👍", 2), ("🎉", 1)]
    # nobody left on 🎉: it goes
    assert [r["emoji"] for r in off.json()["data"]["reactions"]] == ["👍"]
    assert off.json()["data"]["version"] > sent["version"]


async def test_a_message_holds_a_limited_set_of_reactions_and_markup_isnt_one(client):
    owner, _, _, decision_id = await workspace_of_three(client)
    sent = (await send(client, owner, decision_id, "Vote")).json()["data"]
    url = f"/decisions/{decision_id}/comments/{sent['id']}/reactions"
    for n in range(MAX_REACTIONS):
        assert (await client.put(f"{url}/e{n}", headers=owner)).status_code == 200

    full = await client.put(f"{url}/🙂", headers=owner)
    markup = await client.put(f"{url}/<b>", headers=owner)
    missing = await client.put(f"/decisions/{decision_id}/comments/nope/reactions/👍", headers=owner)

    assert (full.status_code, markup.status_code, missing.status_code) == (422, 422, 404)
