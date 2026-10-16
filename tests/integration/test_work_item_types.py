"""Work item types: a workspace's defaults, its own, its tracker's, and changing an item's type."""

import json

import respx
from httpx import Response

from app.db import mongo
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_jira_writeback import ISSUE, issue, setup
from app.workers import jobs
from tests.integration.test_work_item_sync import ISSUES, SEARCH_URL, connect_jira, search_response
from tests.integration.test_workspaces import in_workspace, join, team, user


async def types(client, headers: dict) -> dict[str, dict]:
    return {t["name"]: t for t in (await client.get("/work-item-types", headers=headers)).json()["data"]}


async def native_board(client, headers: dict) -> str:
    boards = (await client.get("/boards", headers=headers)).json()["data"]
    return next(b["id"] for b in boards if b["source"] == "native")


async def test_every_workspace_starts_with_task_bug_and_research_once(client):
    headers = auth_headers(await login(client))

    first = await types(client, headers)
    await types(client, headers)

    assert [(t["name"], t["icon"], t["color"]) for t in first.values()] == [
        ("Task", "CheckSquare", "blue"), ("Bug", "Bug", "red"), ("Research", "Flask", "purple"),
    ]
    assert await mongo.work_item_types().count_documents({}) == 3


async def test_admins_add_rename_and_delete_types_members_only_read_them(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    as_alice, as_bob = in_workspace(alice, acme), in_workspace(bob, acme)

    added = await client.post("/work-item-types", json={"name": "Chore", "icon": "Wrench", "color": "gray"}, headers=as_alice)
    clash = await client.post("/work-item-types", json={"name": " chore ", "icon": "Wrench", "color": "gray"}, headers=as_alice)
    denied = await client.post("/work-item-types", json={"name": "Spike", "icon": "Flask", "color": "amber"}, headers=as_bob)
    renamed = await client.patch(f"/work-item-types/{added.json()['data']['id']}", json={"name": "Housekeeping", "color": "green"}, headers=as_alice)

    assert added.status_code == 201
    assert clash.json()["meta"]["error_code"] == "work_item_type_exists"
    assert denied.json()["meta"]["error_code"] == "insufficient_role"
    assert (renamed.json()["data"]["name"], renamed.json()["data"]["icon"], renamed.json()["data"]["color"]) == ("Housekeeping", "Wrench", "green")
    assert list(await types(client, as_bob)) == ["Task", "Bug", "Research", "Housekeeping"]


async def test_new_items_are_tasks_unless_told_and_a_type_in_use_needs_a_replacement(client):
    headers = auth_headers(await login(client))
    board = await native_board(client, headers)
    by_name = await types(client, headers)

    task = (await client.post("/work-items", json={"title": "Plain", "board_id": board}, headers=headers)).json()["data"]
    bug = (await client.post("/work-items", json={"title": "Broken", "board_id": board, "type_id": by_name["Bug"]["id"]}, headers=headers)).json()["data"]
    changed = await client.patch(f"/work-items/{task['id']}", json={"type_id": by_name["Research"]["id"]}, headers=headers)

    assert (task["type_id"], bug["type_id"]) == (by_name["Task"]["id"], by_name["Bug"]["id"])
    assert changed.json()["data"]["type_id"] == by_name["Research"]["id"]
    refused = await client.delete(f"/work-item-types/{by_name['Bug']['id']}", headers=headers)
    assert refused.json()["meta"]["error_code"] == "work_item_type_in_use"
    await client.delete(f"/work-item-types/{by_name['Bug']['id']}?reassign_to={by_name['Task']['id']}", headers=headers)
    assert (await mongo.work_items().find_one({"_id": bug["id"]}))["type_id"] == by_name["Task"]["id"]
    assert (await client.get(f"/work-items/{bug['id']}/types", headers=headers)).json()["data"] == {"type_ids": None}


async def test_jira_types_are_matched_by_name_and_new_ones_created_looking_the_part(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"])
    story = json.loads(json.dumps(ISSUES[0]))
    story["fields"]["issuetype"] = {"id": "10001", "name": "Story"}

    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response([story, *ISSUES[1:]]))
        await jobs.run_connector_sync(headers["X-Workspace-Id"], "jira", "OVR")

    by_name = await types(client, headers)
    assert (by_name["Story"]["icon"], by_name["Story"]["color"], by_name["Story"]["source"]) == ("BookmarkSimple", "green", "jira")
    assert (await mongo.work_items().find_one({"key": "OVR-1"}))["type_id"] == by_name["Story"]["id"]
    assert (await mongo.work_items().find_one({"key": "OVR-2"}))["type_id"] == by_name["Task"]["id"]
    # Jira owns its type's name; its look is ours
    renamed = await client.patch(f"/work-item-types/{by_name['Story']['id']}", json={"name": "Feature"}, headers=headers)
    recolored = await client.patch(f"/work-item-types/{by_name['Story']['id']}", json={"color": "amber"}, headers=headers)
    assert renamed.json()["meta"]["error_code"] == "work_item_type_from_tracker"
    assert recolored.json()["data"]["color"] == "amber"


async def test_a_jira_items_type_changes_in_jira_only_to_types_it_allows(client):
    headers, item_id = await setup(client)
    by_name = await types(client, headers)
    editmeta = Response(200, json={"fields": {"issuetype": {"allowedValues": [{"id": "10001", "name": "Task"}, {"id": "10004", "name": "Bug"}]}}})

    with respx.mock as router:
        router.get(f"{ISSUE}/editmeta").mock(return_value=editmeta)
        put = router.put(ISSUE).mock(return_value=Response(204))
        router.get(ISSUE).mock(return_value=Response(200, json=issue(issuetype={"id": "10004", "name": "Bug"})))
        allowed = (await client.get(f"/work-items/{item_id}/types", headers=headers)).json()["data"]["type_ids"]
        changed = await client.patch(f"/work-items/{item_id}", json={"type_id": by_name["Bug"]["id"]}, headers=headers)
        refused = await client.patch(f"/work-items/{item_id}", json={"type_id": by_name["Research"]["id"]}, headers=headers)

    assert sorted(allowed) == sorted([by_name["Task"]["id"], by_name["Bug"]["id"]])
    assert json.loads(put.calls[0].request.content) == {"fields": {"issuetype": {"id": "10004"}}}
    assert changed.json()["data"]["type_id"] == by_name["Bug"]["id"]
    assert refused.json()["meta"]["error_code"] == "type_change_in_tracker"
