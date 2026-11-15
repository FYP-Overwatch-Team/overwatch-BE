"""Comments: kept here for native items, read from and posted to the tracker for its items."""

import json

import respx
from httpx import Response

from app.connectors.jira import markdown_to_adf
from app.db import mongo
from app.work_items import repository as work_items
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_work_items_api import board_id, seed_jira
from tests.integration.test_workspaces import in_workspace, join, team, user

API = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3"


def adf(*lines: str) -> dict:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else {"type": "paragraph"} for line in lines]}


def jira_comment(comment_id: str, author: str, *lines: str) -> dict:
    return {"id": comment_id, "author": {"accountId": f"acc-{author}", "displayName": author}, "body": adf(*lines), "created": "2026-10-01T09:00:00.000+0000", "updated": "2026-10-01T09:00:00.000+0000"}


async def native_item(client, headers: dict) -> str:
    resp = await client.post("/work-items", json={"title": "Native", "board_id": await board_id(client, headers)}, headers=headers)
    return resp.json()["data"]["id"]


async def test_native_comments_can_be_written_edited_and_deleted_by_their_author(client):
    headers = auth_headers(await login(client))
    item_id = await native_item(client, headers)

    added = (await client.post(f"/work-items/{item_id}/comments", json={"body": "  Looks **good**  "}, headers=headers)).json()["data"]
    assert (added["body"], added["author"]["name"], added["editable"]) == ("Looks **good**", "Octo Cat", True)

    edited = await client.patch(f"/work-items/{item_id}/comments/{added['id']}", json={"body": "Shipped"}, headers=headers)
    assert edited.json()["data"]["body"] == "Shipped"
    listed = (await client.get(f"/work-items/{item_id}/comments", headers=headers)).json()["data"]
    assert [c["body"] for c in listed] == ["Shipped"]

    assert (await client.delete(f"/work-items/{item_id}/comments/{added['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"/work-items/{item_id}/comments", headers=headers)).json()["data"] == []


async def test_only_the_author_edits_and_an_admin_may_delete(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    as_alice, as_bob = in_workspace(alice, acme), in_workspace(bob, acme)
    item_id = await native_item(client, as_alice)
    comment = (await client.post(f"/work-items/{item_id}/comments", json={"body": "Mine"}, headers=as_bob)).json()["data"]

    seen_by_alice = (await client.get(f"/work-items/{item_id}/comments", headers=as_alice)).json()["data"][0]
    edit = await client.patch(f"/work-items/{item_id}/comments/{comment['id']}", json={"body": "x"}, headers=as_alice)
    delete = await client.delete(f"/work-items/{item_id}/comments/{comment['id']}", headers=as_alice)

    assert (seen_by_alice["editable"], seen_by_alice["deletable"]) == (False, True)  # alice owns the workspace
    assert edit.json()["meta"]["error_code"] == "not_comment_author"
    assert delete.status_code == 200


async def test_jira_comments_are_read_from_jira_and_ours_show_their_overwatch_author(client):
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])
    item = await mongo.work_items().find_one({"key": "OVR-2"})

    with respx.mock as router:
        post = router.post(f"{API}/issue/{item['external_id']}/comment").mock(
            return_value=Response(201, json=jira_comment("501", "Connector Admin", "On it", "", "_Posted from Overwatch by Octo Cat_")),
        )
        added = (await client.post(f"/work-items/{item['_id']}/comments", json={"body": "On it"}, headers=headers)).json()["data"]
    # Markdown goes to Jira formatted, so the note reads in italics there
    assert json.loads(post.calls[0].request.content)["body"] == markdown_to_adf("On it\n\n_Posted from Overwatch by Octo Cat_")
    assert json.loads(post.calls[0].request.content)["body"]["content"][1]["content"][0]["marks"] == [{"type": "em"}]

    with respx.mock as router:
        router.get(f"{API}/issue/{item['external_id']}/comment").mock(return_value=Response(200, json={"total": 2, "comments": [
            jira_comment("500", "Jira User", "Written in Jira"),
            jira_comment("501", "Connector Admin", "On it", "", "_Posted from Overwatch by Octo Cat_"),
        ]}))
        listed = (await client.get(f"/work-items/{item['_id']}/comments", headers=headers)).json()["data"]

    assert [(c["author"]["name"], c["body"], c["editable"]) for c in listed] == [
        ("Jira User", "Written in Jira", False),
        ("Octo Cat", "On it", True),
    ]
    assert listed[1]["id"] == added["id"]

    with respx.mock as router:
        put = router.put(f"{API}/issue/{item['external_id']}/comment/501").mock(return_value=Response(200, json=jira_comment("501", "Connector Admin", "Done")))
        delete = router.delete(f"{API}/issue/{item['external_id']}/comment/501").mock(return_value=Response(204))
        await client.patch(f"/work-items/{item['_id']}/comments/{added['id']}", json={"body": "Done"}, headers=headers)
        await client.delete(f"/work-items/{item['_id']}/comments/{added['id']}", headers=headers)
    assert json.loads(put.calls[0].request.content)["body"] == markdown_to_adf("Done\n\n_Posted from Overwatch by Octo Cat_")
    assert delete.called and await mongo.work_item_comments().count_documents({}) == 0


async def test_a_comment_written_in_jira_cant_be_changed_from_here(client):
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])
    item = await mongo.work_items().find_one({"key": "OVR-2"})

    with respx.mock:  # strict: nothing reaches Jira
        resp = await client.delete(f"/work-items/{item['_id']}/comments/jira:500", headers=headers)

    assert resp.json()["meta"]["error_code"] == "comment_not_found"


async def test_deleting_an_item_deletes_its_comments(client):
    headers = auth_headers(await login(client))
    item_id = await native_item(client, headers)
    await client.post(f"/work-items/{item_id}/comments", json={"body": "x"}, headers=headers)

    await client.delete(f"/work-items/{item_id}", headers=headers)

    assert await mongo.work_item_comments().count_documents({}) == 0


async def test_a_ticket_gone_from_jira_takes_its_comment_records_with_it(client):
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])
    item = await mongo.work_items().find_one({"key": "OVR-1"})
    await mongo.work_item_comments().insert_one({"_id": "c1", "workspace_id": headers["X-Workspace-Id"], "item_id": item["_id"], "external_id": "10"})

    kept = [doc["external_id"] async for doc in mongo.work_items().find({"key": {"$ne": "OVR-1"}})]

    await work_items.sweep(headers["X-Workspace-Id"], "jira", "OVR", kept)

    assert await mongo.work_items().count_documents({}) == len(kept)
    assert await mongo.work_item_comments().count_documents({}) == 0
