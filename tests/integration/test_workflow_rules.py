"""Workflow rules: reading Jira's workflows, where a card may move, and refusing moves a board doesn't allow."""

import respx
from httpx import Response

from app.connectors.jira import WORKFLOW_SCOPE
from app.db import mongo
from app.workers import jobs
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_boards_api import API, boards, issues, statuses
from tests.integration.test_jira_writeback import TRANSITIONS, setup
from tests.integration.test_work_item_sync import SEARCH_URL, WRITE_SCOPES, connect_jira, search_response

WORKFLOWS = {
    "statuses": [
        {"id": "1", "name": "To Do", "statusReference": "r1", "statusCategory": "TODO"},
        {"id": "3", "name": "In Progress", "statusReference": "r3", "statusCategory": "IN_PROGRESS"},
        {"id": "10003", "name": "Done", "statusReference": "r9", "statusCategory": "DONE"},
    ],
    "workflows": [{"id": "wf-1", "name": "Software", "transitions": [
        {"id": "11", "name": "Start", "type": "DIRECTED", "toStatusReference": "r3", "links": [{"fromStatusReference": "r1"}]},
        {"id": "31", "name": "Finish", "type": "DIRECTED", "toStatusReference": "r9", "links": [{"fromStatusReference": "r3"}]},
        {"id": "41", "name": "Reopen", "type": "GLOBAL", "toStatusReference": "r1"},
    ]}],
}


async def full_sync(workspace_id: str, workflows: Response) -> respx.Route:
    await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})
    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(return_value=search_response(issues()))
        router.get(f"{API}/project/OVR/statuses").mock(return_value=statuses())
        router.get(f"{API}/project/OVR").mock(return_value=Response(200, json={"id": "100", "issueTypes": [{"id": "10001", "name": "Task"}]}))
        route = router.post(f"{API}/workflows").mock(return_value=workflows)
        await jobs.run_connector_sync(workspace_id, "jira", "OVR")
    return route


async def test_a_full_sync_reads_the_projects_workflows_for_hints(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=[*WRITE_SCOPES, WORKFLOW_SCOPE])

    route = await full_sync(headers["X-Workspace-Id"], Response(200, json=WORKFLOWS))

    assert route.calls[0].request.content == b'{"projectAndIssueTypes":[{"projectId":"100","issueTypeId":"10001"}]}'
    workflow = (await boards(client, headers))["jira"]["workflow"]
    assert workflow["by_issue_type"] == {"10001": "wf-1"}
    assert workflow["workflows"]["wf-1"]["transitions"] == [
        {"id": "11", "name": "Start", "from": ["1"], "to": "3"},
        {"id": "31", "name": "Finish", "from": ["3"], "to": "10003"},
        {"id": "41", "name": "Reopen", "from": None, "to": "1"},
    ]
    assert workflow["statuses"]["3"] == {"name": "In Progress", "category": "in_progress"}


async def test_a_grant_that_cant_read_workflows_still_syncs(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=[*WRITE_SCOPES, WORKFLOW_SCOPE])

    await full_sync(headers["X-Workspace-Id"], Response(403, json={}))

    assert (await mongo.sync_targets().find_one({"key": "OVR"}))["sync_status"] == "done"
    assert (await boards(client, headers))["jira"]["workflow"] is None


async def test_a_jira_items_moves_come_from_jira_with_the_transition_names(client):
    headers, item_id = await setup(client)  # OVR-2, In Progress
    with respx.mock as router:
        router.get(TRANSITIONS).mock(return_value=Response(200, json={"transitions": [
            {"id": "31", "name": "Finish", "to": {"id": "10003", "name": "Done", "statusCategory": {"key": "done"}}},
            {"id": "41", "name": "Reopen", "to": {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}}},
        ]}))
        moves = (await client.get(f"/work-items/{item_id}/moves", headers=headers)).json()["data"]

    assert moves == {"basis": "live", "targets": [
        {"column_id": "done", "status_id": "10003", "status": "Done", "transition_name": "Finish"},
        {"column_id": "todo", "status_id": "1", "status": "To Do", "transition_name": "Reopen"},
    ]}


async def test_a_native_board_refuses_moves_its_workflow_doesnt_allow_and_says_what_to_do(client):
    headers = auth_headers(await login(client))
    board = next(b for b in (await client.get("/boards", headers=headers)).json()["data"] if b["source"] == "native")
    item = (await client.post("/work-items", json={"title": "Ship it", "board_id": board["id"]}, headers=headers)).json()["data"]
    open_moves = (await client.get(f"/work-items/{item['id']}/moves", headers=headers)).json()["data"]
    # step by step: To do -> In progress -> Done
    stored = await mongo.boards().find_one({"_id": board["id"]})
    rules = {"todo": ["in_progress"], "in_progress": ["todo", "done"], "done": ["in_progress"]}
    await mongo.boards().update_one({"_id": board["id"]}, {"$set": {"columns": [{**c, "next": rules[c["id"]]} for c in stored["columns"]]}})

    refused = await client.post(f"/work-items/{item['id']}/move", json={"column_id": "done"}, headers=headers)
    moves = (await client.get(f"/work-items/{item['id']}/moves", headers=headers)).json()["data"]
    allowed = await client.post(f"/work-items/{item['id']}/move", json={"column_id": "in_progress"}, headers=headers)

    assert open_moves["basis"] == "open" and len(open_moves["targets"]) == 2
    assert refused.json()["meta"]["error_code"] == "transition_not_allowed"
    assert refused.json()["meta"]["message"].startswith("Move it to In progress first")
    assert moves == {"basis": "board", "targets": [{"column_id": "in_progress", "status_id": None, "status": None, "transition_name": None}]}
    assert allowed.status_code == 200


async def test_admins_set_a_native_boards_columns_and_workflow_moving_items_out_of_removed_ones(client):
    headers = auth_headers(await login(client))
    board = next(b for b in (await client.get("/boards", headers=headers)).json()["data"] if b["source"] == "native")
    item = (await client.post("/work-items", json={"title": "Waiting", "board_id": board["id"], "column_id": "in_progress"}, headers=headers)).json()["data"]

    resp = await client.put(f"/boards/{board['id']}/columns", json={
        "columns": [
            {"id": "todo", "name": "To do", "status_category": "todo", "next": ["review"]},
            {"id": "review", "name": "Review", "status_category": "in_progress", "color": "purple", "next": None},
            {"id": "done", "name": "Done", "status_category": "done", "next": ["review"]},
        ],
        "reassign": {"in_progress": "review"},
    }, headers=headers)

    saved = resp.json()["data"]
    assert [(c["id"], c["name"], c["color"], c["next"]) for c in saved["columns"]] == [
        ("todo", "To do", None, ["review"]), ("review", "Review", "purple", None), ("done", "Done", None, ["review"]),
    ]
    moved = await mongo.work_items().find_one({"_id": item["id"]})
    assert (moved["column_id"], moved["status_category"]) == ("review", "in_progress")


async def test_column_changes_are_checked_and_admin_only(client):
    headers = auth_headers(await login(client))
    boards_ = (await client.get("/boards", headers=headers)).json()["data"]
    native = next(b for b in boards_ if b["source"] == "native")
    column = {"id": "todo", "name": "To do", "status_category": "todo"}

    unknown = await client.put(f"/boards/{native['id']}/columns", json={"columns": [{**column, "next": ["nowhere"]}]}, headers=headers)
    duplicate = await client.put(f"/boards/{native['id']}/columns", json={"columns": [column, {**column, "id": "again"}]}, headers=headers)

    assert unknown.json()["meta"]["error_code"] == "column_not_found"
    assert duplicate.json()["meta"]["error_code"] == "duplicate_columns"
