"""Sprints and the backlog: mirrored from Jira on sync, managed from Overwatch, written through."""

import json
from datetime import timedelta

import respx
from httpx import Response

from app.connectors.jira import AGILE_SCOPES
from app.core.time import utcnow
from app.db import mongo
from app.sprints import repository as sprint_store
from app.workers import jobs
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_boards_api import AGILE, API, boards, issues, statuses
from tests.integration.test_work_item_sync import SEARCH_URL, WRITE_SCOPES, connect_jira, search_response
from tests.integration.test_workspaces import in_workspace, join, team, user

SPRINT_FIELD = "customfield_10020"
FIELDS = Response(200, json=[{"id": "summary", "schema": {}}, {"id": SPRINT_FIELD, "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}}])
ACTIVE = {"id": 41, "name": "DEV Sprint 39", "state": "active", "startDate": "2026-10-01T09:00:00.000Z", "endDate": "2026-10-15T09:00:00.000Z", "boardId": 9}
NEXT = {"id": 42, "name": "DEV Sprint 40", "state": "future", "boardId": 9}
CLOSED = {"id": 40, "name": "DEV Sprint 38", "state": "closed", "boardId": 9}


def sprinted_issues(active: dict = ACTIVE) -> list[dict]:
    """OVR-1 (done) was in the closed sprint, OVR-2 is in the active one, OVR-3 is in the backlog."""
    found = issues()
    found[0]["fields"][SPRINT_FIELD] = [CLOSED]
    found[1]["fields"][SPRINT_FIELD] = [CLOSED, active]
    found[2]["fields"][SPRINT_FIELD] = None
    return found


async def sync(workspace_id: str, *, agile: bool, sprints: list[dict] | None = None, full: bool = True, active: dict = ACTIVE) -> None:
    if full:
        await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})
    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(return_value=search_response(sprinted_issues(active)))
        router.get(f"{API}/project/OVR/statuses").mock(return_value=statuses())
        router.get(f"{API}/field").mock(return_value=FIELDS)
        if agile:
            router.get(f"{AGILE}/board").mock(return_value=Response(200, json={"values": [{"id": 9, "name": "OVR", "type": "scrum"}]}))
            router.get(f"{AGILE}/board/9/configuration").mock(return_value=Response(200, json={"columnConfig": {"columns": []}}))
            router.get(f"{AGILE}/board/9/sprint").mock(return_value=Response(200, json={"values": sprints if sprints is not None else [ACTIVE, NEXT], "isLast": True}))
        await jobs.run_connector_sync(workspace_id, "jira", "OVR")


async def keys(client, headers: dict, board_id: str, query: str) -> list[str]:
    resp = await client.get(f"/work-items?board_id={board_id}&{query}", headers=headers)
    return sorted(item["key"] for item in resp.json()["data"])


async def jira_workspace(client, *, agile: bool) -> dict:
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=[*WRITE_SCOPES, *(AGILE_SCOPES if agile else ())])
    return headers


async def test_sprint_membership_comes_from_the_issues_own_field(client):
    headers = await jira_workspace(client, agile=False)

    await sync(headers["X-Workspace-Id"], agile=False)

    board = (await boards(client, headers))["jira"]
    assert board["kind"] == "scrum"  # its issues are in sprints
    assert await keys(client, headers, board["id"], "sprint=current") == ["OVR-2"]
    # the done issue only ever sat in a closed sprint, so it's not in the backlog either way
    assert await keys(client, headers, board["id"], "sprint=backlog") == ["OVR-1", "OVR-3"]
    # the backlog view leaves finished work out
    assert await keys(client, headers, board["id"], "sprint=backlog&open_only=true") == ["OVR-3"]
    sprints = (await client.get(f"/boards/{board['id']}/sprints", headers=headers)).json()["data"]
    assert [(s["name"], s["state"]) for s in sprints] == [("DEV Sprint 39", "active")]


async def test_with_jira_software_access_empty_future_sprints_appear_and_closed_ones_go(client):
    headers = await jira_workspace(client, agile=True)
    await sync(headers["X-Workspace-Id"], agile=True)
    board = (await boards(client, headers))["jira"]

    sprints = (await client.get(f"/boards/{board['id']}/sprints", headers=headers)).json()["data"]
    assert [(s["name"], s["state"]) for s in sprints] == [("DEV Sprint 39", "active"), ("DEV Sprint 40", "future")]

    # completed in Jira: no longer listed as open there
    await sync(headers["X-Workspace-Id"], agile=True, sprints=[NEXT], full=False, active={**ACTIVE, "state": "closed"})
    sprints = (await client.get(f"/boards/{board['id']}/sprints", headers=headers)).json()["data"]
    assert [s["name"] for s in sprints] == ["DEV Sprint 40"]


async def test_a_kanban_boards_done_column_keeps_recent_work_and_epics_stay_off(client):
    headers = await jira_workspace(client, agile=False)
    await sync(headers["X-Workspace-Id"], agile=False)
    board = (await boards(client, headers))["jira"]
    await mongo.work_items().update_one({"key": "OVR-1"}, {"$set": {"status_changed_at": utcnow() - timedelta(days=30)}})
    await mongo.work_items().update_one({"key": "OVR-3"}, {"$set": {"issue_type": "Epic"}})

    assert await keys(client, headers, board["id"], "done_within_days=14&hide_epics=true") == ["OVR-2"]


async def test_a_native_sprint_runs_from_planning_to_completion(client):
    headers = auth_headers(await login(client))
    native = (await boards(client, headers))["native"]["id"]

    created = (await client.post(f"/boards/{native}/sprints", json={"goal": "Ship boards"}, headers=headers)).json()["data"]
    assert (created["name"], created["state"]) == ("Overwatch Sprint 1", "future")
    later = (await client.post(f"/boards/{native}/sprints", json={"name": "Next"}, headers=headers)).json()["data"]

    started = (await client.post(f"/sprints/{created['id']}/start", json={}, headers=headers)).json()["data"]
    assert started["state"] == "active" and started["end_at"] is not None
    second = await client.post(f"/sprints/{later['id']}/start", json={}, headers=headers)
    assert second.json()["meta"]["error_code"] == "sprint_already_active"

    open_item = (await client.post("/work-items", json={"title": "Open", "board_id": native, "sprint_id": created["id"]}, headers=headers)).json()["data"]
    done_item = (await client.post("/work-items", json={"title": "Done", "board_id": native, "column_id": "done", "sprint_id": created["id"]}, headers=headers)).json()["data"]
    backlog = (await client.post("/work-items", json={"title": "Later", "board_id": native}, headers=headers)).json()["data"]
    assert await keys(client, headers, native, "sprint=current") == sorted([open_item["key"], done_item["key"]])

    moved = await client.post(f"/work-items/{backlog['id']}/sprint", json={"sprint_id": created["id"]}, headers=headers)
    assert moved.json()["data"]["sprint_id"] == created["id"]
    await client.post(f"/work-items/{backlog['id']}/sprint", json={"sprint_id": None}, headers=headers)

    completed = (await client.post(f"/sprints/{created['id']}/complete", json={"move_open_to": later["id"]}, headers=headers)).json()["data"]
    assert completed["state"] == "closed" and completed["completed_at"] is not None
    assert (await mongo.work_items().find_one({"_id": open_item["id"]}))["sprint_id"] == later["id"]
    # finished work stays in the sprint it was done in
    assert (await mongo.work_items().find_one({"_id": done_item["id"]}))["sprint_id"] == created["id"]
    assert await keys(client, headers, native, "sprint=current") == []
    assert (await client.get(f"/boards/{native}/sprints", headers=headers)).json()["data"][0]["name"] == "Next"


async def test_two_native_sprints_started_at_once_leave_one_active(client, monkeypatch):
    headers = auth_headers(await login(client))
    native = (await boards(client, headers))["native"]["id"]
    first = (await client.post(f"/boards/{native}/sprints", json={}, headers=headers)).json()["data"]
    second = (await client.post(f"/boards/{native}/sprints", json={}, headers=headers)).json()["data"]
    await client.post(f"/sprints/{first['id']}/start", json={}, headers=headers)

    async def no_active_yet(board_id):
        return None

    # the second start read "no active sprint" before the first one was saved
    monkeypatch.setattr(sprint_store, "active", no_active_yet)
    resp = await client.post(f"/sprints/{second['id']}/start", json={}, headers=headers)

    assert resp.json()["meta"]["error_code"] == "sprint_already_active"
    assert await mongo.sprints().count_documents({"board_id": native, "state": "active"}) == 1


async def test_only_admins_start_and_complete_sprints(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    native = (await boards(client, in_workspace(alice, acme)))["native"]["id"]
    sprint = (await client.post(f"/boards/{native}/sprints", json={}, headers=in_workspace(bob, acme))).json()["data"]

    resp = await client.post(f"/sprints/{sprint['id']}/start", json={}, headers=in_workspace(bob, acme))

    assert resp.json()["meta"]["error_code"] == "insufficient_role"


async def test_jira_sprints_are_created_started_and_completed_in_jira_first(client):
    headers = await jira_workspace(client, agile=True)
    await sync(headers["X-Workspace-Id"], agile=True)
    board = (await boards(client, headers))["jira"]
    sprints = (await client.get(f"/boards/{board['id']}/sprints", headers=headers)).json()["data"]
    active, future = sprints[0]["id"], sprints[1]["id"]

    with respx.mock as router:
        create = router.post(f"{AGILE}/sprint").mock(return_value=Response(201, json={"id": 43, "name": "DEV Sprint 41", "state": "future"}))
        resp = await client.post(f"/boards/{board['id']}/sprints", json={"name": "DEV Sprint 41"}, headers=headers)
    assert json.loads(create.calls[0].request.content) == {"originBoardId": 9, "name": "DEV Sprint 41"}
    assert resp.json()["data"]["name"] == "DEV Sprint 41"

    with respx.mock as router:
        to_next = router.post(f"{AGILE}/sprint/42/issue").mock(return_value=Response(204))
        close = router.post(f"{AGILE}/sprint/41").mock(return_value=Response(200, json={**ACTIVE, "state": "closed"}))
        resp = await client.post(f"/sprints/{active}/complete", json={"move_open_to": future}, headers=headers)

    item = await mongo.work_items().find_one({"key": "OVR-2"})
    assert json.loads(to_next.calls[0].request.content) == {"issues": [item["external_id"]]}
    assert json.loads(close.calls[0].request.content) == {"state": "closed"}
    assert resp.json()["data"]["state"] == "closed" and item["sprint_id"] == future

    with respx.mock as router:
        start = router.post(f"{AGILE}/sprint/42").mock(return_value=Response(200, json={**NEXT, "state": "active", "startDate": "2026-10-16T09:00:00.000Z", "endDate": "2026-10-30T09:00:00.000Z"}))
        resp = await client.post(f"/sprints/{future}/start", json={}, headers=headers)
    body = json.loads(start.calls[0].request.content)
    assert body["state"] == "active" and body["startDate"] and body["endDate"]
    assert resp.json()["data"]["state"] == "active"


async def test_moving_a_jira_issue_to_the_backlog_happens_in_jira(client):
    headers = await jira_workspace(client, agile=True)
    await sync(headers["X-Workspace-Id"], agile=True)
    item = await mongo.work_items().find_one({"key": "OVR-2"})

    with respx.mock as router:
        backlog = router.post(f"{AGILE}/backlog/issue").mock(return_value=Response(204))
        resp = await client.post(f"/work-items/{item['_id']}/sprint", json={"sprint_id": None}, headers=headers)

    assert json.loads(backlog.calls[0].request.content) == {"issues": [item["external_id"]]}
    assert resp.json()["data"]["sprint_id"] is None


async def test_without_jira_software_access_sprints_are_read_only(client):
    headers = await jira_workspace(client, agile=False)
    await sync(headers["X-Workspace-Id"], agile=False)
    board = (await boards(client, headers))["jira"]
    item = await mongo.work_items().find_one({"key": "OVR-2"})

    with respx.mock:  # strict: nothing reaches Jira
        created = await client.post(f"/boards/{board['id']}/sprints", json={}, headers=headers)
        moved = await client.post(f"/work-items/{item['_id']}/sprint", json={"sprint_id": None}, headers=headers)

    assert created.json()["meta"]["error_code"] == moved.json()["meta"]["error_code"] == "jira_reconnect_required"
    assert (await mongo.work_items().find_one({"key": "OVR-2"}))["sprint_id"] is not None
