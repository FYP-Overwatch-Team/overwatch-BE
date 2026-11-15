"""The ticket view's fields: priority, labels, due date and story points, natively and in Jira."""

import json

import respx
from httpx import Response

from app.db import mongo
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_work_items_api import board_id, seed_jira

API = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3"


async def test_a_native_items_details_are_edited_here(client):
    headers = auth_headers(await login(client))
    created = (await client.post("/work-items", json={"title": "x", "description": "# Plan", "board_id": await board_id(client, headers)}, headers=headers)).json()["data"]
    assert created["reporter"]["name"] == "Octo Cat" and created["description_md"] == "# Plan"

    resp = await client.patch(f"/work-items/{created['id']}", json={
        "priority": "High", "labels": ["api", "urgent"], "due_date": "2026-10-20", "story_points": 3,
    }, headers=headers)

    data = resp.json()["data"]
    assert (data["priority"], data["labels"], data["due_date"], data["story_points"]) == ("High", ["api", "urgent"], "2026-10-20", 3)
    bad = await client.patch(f"/work-items/{created['id']}", json={"labels": ["has space"]}, headers=headers)
    assert bad.status_code == 422


async def test_jira_details_go_to_jira_with_the_boards_story_points_field(client):
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])
    await board_id(client, headers, "jira")  # boards are created on first sight
    await mongo.boards().update_one({"source": "jira"}, {"$set": {"fields": {"sprint": None, "story_points": "customfield_10016"}}})
    item = await mongo.work_items().find_one({"key": "OVR-2"})

    with respx.mock as router:
        put = router.put(f"{API}/issue/{item['external_id']}").mock(return_value=Response(204))
        router.get(f"{API}/issue/{item['external_id']}").mock(return_value=Response(200, json=item["raw"]))
        await client.patch(f"/work-items/{item['_id']}", json={"priority": "High", "labels": ["api"], "due_date": "2026-10-20", "story_points": 2}, headers=headers)

    assert json.loads(put.calls[0].request.content) == {"fields": {
        "priority": {"name": "High"}, "labels": ["api"], "duedate": "2026-10-20", "customfield_10016": 2.0,
    }}


async def test_story_points_need_a_field_to_go_to(client):
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])
    item = await mongo.work_items().find_one({"key": "OVR-2"})

    with respx.mock:
        resp = await client.patch(f"/work-items/{item['_id']}", json={"story_points": 2}, headers=headers)

    assert resp.json()["meta"]["error_code"] == "jira_no_story_points"


async def test_priorities_come_from_the_board(client):
    headers = auth_headers(await login(client))
    await seed_jira(headers["X-Workspace-Id"])

    native = (await client.get(f"/boards/{await board_id(client, headers)}/priorities", headers=headers)).json()["data"]
    with respx.mock as router:
        router.get(f"{API}/priority").mock(return_value=Response(200, json=[{"name": "Blocker"}, {"name": "Minor"}]))
        jira = (await client.get(f"/boards/{await board_id(client, headers, 'jira')}/priorities", headers=headers)).json()["data"]

    assert native == ["Highest", "High", "Medium", "Low", "Lowest"]
    assert jira == ["Blocker", "Minor"]
