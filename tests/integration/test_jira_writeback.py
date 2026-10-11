"""Board changes to Jira items go to Jira first; the board then shows what Jira has."""

import json

import respx
from httpx import Response

from app.db import mongo
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_work_item_sync import ISSUES, connect_jira
from tests.integration.test_work_items_api import seed_items, seed_jira
from tests.integration.test_workspaces import in_workspace, join, team, user

API = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3"
ISSUE = f"{API}/issue/10002"
TRANSITIONS = f"{ISSUE}/transitions"


def issue(**changes) -> dict:
    raw = json.loads(json.dumps(ISSUES[1]))  # OVR-2, In Progress
    raw["fields"].update(changes)
    return raw


async def setup(client) -> tuple[dict, str]:
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])
    item = await mongo.work_items().find_one({"key": "OVR-2"})
    return headers, item["_id"]


async def stored() -> dict:
    return await mongo.work_items().find_one({"key": "OVR-2"})


async def test_an_edit_is_written_to_jira_then_read_back(client):
    headers, item_id = await setup(client)
    with respx.mock as router:
        put = router.put(ISSUE).mock(return_value=Response(204))
        router.get(ISSUE).mock(return_value=Response(200, json=issue(summary="Renamed in Jira")))
        resp = await client.patch(
            f"/work-items/{item_id}", json={"title": "Renamed", "description": "Line", "assignee_id": "acc-alex"},
            headers=headers,
        )

    assert json.loads(put.calls[0].request.content) == {"fields": {
        "summary": "Renamed",
        "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Line"}]}]},
        "assignee": {"accountId": "acc-alex"},
    }}
    assert resp.json()["data"]["title"] == "Renamed in Jira"


async def test_moving_a_card_applies_a_matching_jira_transition(client):
    headers, item_id = await setup(client)
    done = issue(status={"name": "Shipped", "statusCategory": {"key": "done"}})
    with respx.mock as router:
        router.get(TRANSITIONS).mock(return_value=Response(200, json={"transitions": [
            {"id": "11", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
            {"id": "31", "to": {"name": "Shipped", "statusCategory": {"key": "done"}}},
        ]}))
        post = router.post(TRANSITIONS).mock(return_value=Response(204))
        router.get(ISSUE).mock(return_value=Response(200, json=done))
        resp = await client.post(f"/work-items/{item_id}/move", json={"status_category": "done"}, headers=headers)

    assert json.loads(post.calls[0].request.content) == {"transition": {"id": "31"}}
    assert (resp.json()["data"]["status"], resp.json()["data"]["status_category"]) == ("Shipped", "done")


async def test_reordering_within_a_column_stays_in_overwatch(client):
    headers, item_id = await setup(client)
    with respx.mock:  # strict: no Jira call
        resp = await client.post(f"/work-items/{item_id}/move", json={"status_category": "in_progress"}, headers=headers)
    assert resp.status_code == 200


async def test_a_column_the_workflow_cant_reach_is_refused(client):
    headers, item_id = await setup(client)
    with respx.mock as router:
        router.get(TRANSITIONS).mock(return_value=Response(200, json={"transitions": []}))
        resp = await client.post(f"/work-items/{item_id}/move", json={"status_category": "done"}, headers=headers)

    assert resp.json()["meta"]["error_code"] == "jira_transition_unavailable"
    assert (await stored())["status_category"] == "in_progress"


async def test_a_grant_without_write_access_must_reconnect(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=["read:jira-work", "read:jira-user", "offline_access"])
    await seed_items(headers["X-Workspace-Id"])
    item_id = (await stored())["_id"]

    with respx.mock:
        resp = await client.patch(f"/work-items/{item_id}", json={"title": "x"}, headers=headers)
    listed = (await client.get("/work-items", headers=headers)).json()["data"]

    assert resp.json()["meta"]["error_code"] == "jira_reconnect_required"
    assert not any(i["editable"] for i in listed)


async def test_when_jira_refuses_nothing_changes_here(client):
    headers, item_id = await setup(client)
    with respx.mock as router:
        router.put(ISSUE).mock(return_value=Response(403, json={"errorMessages": ["You can't edit this issue"]}))
        resp = await client.patch(f"/work-items/{item_id}", json={"title": "x"}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["meta"] == {"error_code": "jira_forbidden", "message": "could not update the Jira issue: You can't edit this issue"}
    assert (await stored())["title"] == "Fix login redirect loop"


async def test_deleting_a_card_deletes_the_jira_issue(client):
    headers, item_id = await setup(client)
    with respx.mock as router:
        delete = router.delete(ISSUE).mock(return_value=Response(204))
        resp = await client.delete(f"/work-items/{item_id}", headers=headers)

    assert resp.status_code == 200 and delete.called
    assert await stored() is None


async def test_only_admins_delete_jira_items(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await seed_jira(acme)
    item_id = (await stored())["_id"]

    with respx.mock:  # strict: nothing reaches Jira
        resp = await client.delete(f"/work-items/{item_id}", headers=in_workspace(bob, acme))

    assert resp.json()["meta"]["error_code"] == "insufficient_role"
    assert await stored() is not None


async def test_a_new_item_can_be_created_in_a_jira_project(client):
    headers, _ = await setup(client)
    created = {"id": "10010", "key": "OVR-10", "fields": {
        "summary": "From the board", "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "created": "2026-10-01T09:00:00.000+0000",
    }}
    with respx.mock as router:
        router.get(f"{API}/project/OVR").mock(return_value=Response(200, json={"issueTypes": [
            {"id": "5", "name": "Sub-task", "subtask": True}, {"id": "1", "name": "Story"}, {"id": "2", "name": "Task"},
        ]}))
        post = router.post(f"{API}/issue").mock(return_value=Response(201, json={"id": "10010", "key": "OVR-10"}))
        router.get(f"{API}/issue/10010/transitions").mock(return_value=Response(200, json={"transitions": [
            {"id": "21", "to": {"statusCategory": {"key": "indeterminate"}}},
        ]}))
        router.post(f"{API}/issue/10010/transitions").mock(return_value=Response(204))
        router.get(f"{API}/issue/10010").mock(return_value=Response(200, json=created))
        resp = await client.post("/work-items", json={
            "title": "From the board", "status_category": "in_progress", "provider": "jira", "target_key": "OVR",
        }, headers=headers)

    assert json.loads(post.calls[0].request.content)["fields"] == {
        "project": {"key": "OVR"}, "issuetype": {"id": "2"}, "summary": "From the board", "description": None,
    }
    data = resp.json()["data"]
    assert (data["key"], data["source"], data["status_category"]) == ("OVR-10", "jira", "in_progress")
    assert (await mongo.work_items().find_one({"key": "OVR-10"}))["created_by"] == headers["X-Workspace-Id"]


async def test_creating_in_a_project_that_isnt_synced_is_refused(client):
    headers, _ = await setup(client)
    resp = await client.post("/work-items", json={"title": "x", "provider": "jira", "target_key": "NOPE"}, headers=headers)
    assert resp.json()["meta"]["error_code"] == "target_not_found"
