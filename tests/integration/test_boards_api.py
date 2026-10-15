"""Boards: one per tracker target plus a native one, and importing the tracker's columns."""

import json

import respx
from httpx import Response

from app.boards import repository as board_store
from app.connectors.jira import AGILE_SCOPES, column_id
from app.db import mongo
from app.workers import jobs
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_work_item_sync import ISSUES, SEARCH_URL, WRITE_SCOPES, connect_jira, search_response
from tests.integration.test_workspaces import in_workspace, join, team, user

API = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3"
AGILE = "https://api.atlassian.com/ex/jira/cloud-1/rest/agile/1.0"
# Jira status ids for the fixture's three issues: Done, In Progress, To Do
STATUS_IDS = {"OVR-1": "10003", "OVR-2": "3", "OVR-3": "1"}


def issues() -> list[dict]:
    copied = json.loads(json.dumps(ISSUES))
    for issue in copied:
        issue["fields"]["status"]["id"] = STATUS_IDS[issue["key"]]
    return copied


def statuses(*extra: dict) -> Response:
    """`/project/OVR/statuses`: the same status under two issue types is listed once."""
    task = [
        {"id": "10003", "name": "Done", "statusCategory": {"key": "done"}},
        {"id": "3", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
        *extra,
    ]
    return Response(200, json=[{"name": "Task", "statuses": task}, {"name": "Bug", "statuses": task[:1]}])


async def full_sync(workspace_id: str, *extra_statuses: dict) -> None:
    await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response(issues()))
        router.get(f"{API}/project/OVR/statuses").mock(return_value=statuses(*extra_statuses))
        await jobs.run_connector_sync(workspace_id, "jira", "OVR")


async def boards(client, headers: dict) -> dict:
    return {b["source"]: b for b in (await client.get("/boards", headers=headers)).json()["data"]}


async def signed_in_with_jira(client) -> dict:
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"])
    return headers


async def test_every_workspace_has_a_native_board_and_one_per_target(client):
    headers = await signed_in_with_jira(client)

    found = await boards(client, headers)

    assert set(found) == {"native", "jira"}
    assert found["jira"]["target_key"] == "OVR" and found["native"]["target_key"] is None
    assert [c["id"] for c in found["jira"]["columns"]] == ["todo", "in_progress", "done"]
    assert found["jira"]["column_prompt"] == {"status": "none", "columns": []}


async def test_reading_a_board_keeps_the_name_its_target_gave_it(client):
    headers = await signed_in_with_jira(client)
    await mongo.sync_targets().update_one({}, {"$set": {"name": "Overvault"}})
    board = (await boards(client, headers))["jira"]

    # what a move, an edit or a sync does when it only needs the board
    await board_store.ensure(headers["X-Workspace-Id"], "jira", "OVR")

    assert (await mongo.boards().find_one({"_id": board["id"]}))["name"] == "Overvault"


async def test_choosing_columns_reports_a_read_only_board_as_read_only(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=["read:jira-work", "offline_access"])
    await full_sync(headers["X-Workspace-Id"])
    board = (await boards(client, headers))["jira"]

    resp = await client.post(f"/boards/{board['id']}/columns/tracker", headers=headers)

    assert resp.json()["data"]["editable"] is False


async def test_a_full_sync_offers_the_trackers_columns(client):
    headers = await signed_in_with_jira(client)

    await full_sync(headers["X-Workspace-Id"], {"id": "4", "name": "In Review", "statusCategory": {"key": "indeterminate"}})

    board = (await boards(client, headers))["jira"]
    assert board["column_prompt"] == {"status": "pending", "columns": ["To Do", "In Progress", "In Review", "Done"]}
    # nothing changes until an admin says so
    assert board["column_mode"] == "default"
    assert (await mongo.work_items().find_one({"key": "OVR-2"}))["column_id"] == "in_progress"


async def test_accepting_moves_items_into_the_trackers_columns(client):
    headers = await signed_in_with_jira(client)
    await full_sync(headers["X-Workspace-Id"])
    board = (await boards(client, headers))["jira"]

    resp = await client.post(f"/boards/{board['id']}/columns/tracker", headers=headers)

    assert resp.json()["data"]["column_mode"] == "tracker"
    assert [c["name"] for c in resp.json()["data"]["columns"]] == ["To Do", "In Progress", "Done"]
    assert (await mongo.work_items().find_one({"key": "OVR-2"}))["column_id"] == column_id("In Progress")
    listed = (await client.get(f"/work-items?board_id={board['id']}&column_id={column_id('Done')}", headers=headers)).json()["data"]
    assert [i["key"] for i in listed] == ["OVR-1"]


async def test_once_accepted_later_tracker_changes_follow_and_unmapped_statuses_hide(client):
    headers = await signed_in_with_jira(client)
    await full_sync(headers["X-Workspace-Id"])
    board = (await boards(client, headers))["jira"]
    await client.post(f"/boards/{board['id']}/columns/tracker", headers=headers)

    # Jira drops "In Progress" from the project's workflow and adds "Doing"
    await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response(issues()))
        router.get(f"{API}/project/OVR/statuses").mock(return_value=Response(200, json=[{"name": "Task", "statuses": [
            {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
            {"id": "5", "name": "Doing", "statusCategory": {"key": "indeterminate"}},
            {"id": "10003", "name": "Done", "statusCategory": {"key": "done"}},
        ]}]))
        await jobs.run_connector_sync(headers["X-Workspace-Id"], "jira", "OVR")

    board = (await boards(client, headers))["jira"]
    assert [c["name"] for c in board["columns"]] == ["To Do", "Doing", "Done"]
    assert board["column_prompt"]["status"] == "none"
    # its status is in no column any more, as Jira would hide it
    assert (await mongo.work_items().find_one({"key": "OVR-2"}))["column_id"] is None


async def test_declining_keeps_the_defaults_until_the_trackers_columns_change(client):
    headers = await signed_in_with_jira(client)
    await full_sync(headers["X-Workspace-Id"])
    board = (await boards(client, headers))["jira"]

    resp = await client.post(f"/boards/{board['id']}/columns/default", headers=headers)
    assert resp.json()["data"]["column_prompt"]["status"] == "dismissed"

    await full_sync(headers["X-Workspace-Id"])
    assert (await boards(client, headers))["jira"]["column_prompt"]["status"] == "dismissed"

    await full_sync(headers["X-Workspace-Id"], {"id": "4", "name": "In Review", "statusCategory": {"key": "indeterminate"}})
    assert (await boards(client, headers))["jira"]["column_prompt"]["status"] == "pending"


async def test_only_admins_change_a_boards_columns(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await connect_jira(acme)
    await full_sync(acme)
    board = (await boards(client, in_workspace(alice, acme)))["jira"]

    resp = await client.post(f"/boards/{board['id']}/columns/tracker", headers=in_workspace(bob, acme))

    assert resp.json()["meta"]["error_code"] == "insufficient_role"


async def test_a_board_of_a_removed_target_is_gone(client):
    headers = await signed_in_with_jira(client)
    board = (await boards(client, headers))["jira"]
    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"status": "disconnected"}})

    assert "jira" not in await boards(client, headers)
    resp = await client.post(f"/boards/{board['id']}/columns/default", headers=headers)
    assert resp.json()["meta"]["error_code"] == "board_not_found"


async def test_with_jira_software_access_the_boards_own_columns_are_offered(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=[*WRITE_SCOPES, *AGILE_SCOPES])
    await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})

    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response(issues()))
        router.get(f"{API}/project/OVR/statuses").mock(return_value=statuses())
        router.get(f"{AGILE}/board").mock(return_value=Response(200, json={"values": [
            {"id": 7, "name": "OVR kanban", "type": "kanban"}, {"id": 9, "name": "OVR scrum", "type": "scrum"},
        ]}))
        config = router.get(f"{AGILE}/board/9/configuration").mock(return_value=Response(200, json={"columnConfig": {"columns": [
            {"name": "Backlog", "statuses": []},
            {"name": "Ready", "statuses": [{"id": "1"}]},
            {"name": "Working", "statuses": [{"id": "3"}, {"id": "999"}]},
            {"name": "Shipped", "statuses": [{"id": "10003"}]},
        ]}}))
        await jobs.run_connector_sync(headers["X-Workspace-Id"], "jira", "OVR")

    assert config.called  # the scrum board, not the first one listed
    # a column with none of the project's statuses is left out
    assert (await boards(client, headers))["jira"]["column_prompt"]["columns"] == ["Ready", "Working", "Shipped"]


async def test_moving_into_a_tracker_column_takes_a_transition_into_its_statuses(client):
    headers = await signed_in_with_jira(client)
    await full_sync(headers["X-Workspace-Id"], {"id": "4", "name": "In Review", "statusCategory": {"key": "indeterminate"}})
    board = (await boards(client, headers))["jira"]
    await client.post(f"/boards/{board['id']}/columns/tracker", headers=headers)
    item = await mongo.work_items().find_one({"key": "OVR-3"})
    in_review = issues()[2]
    in_review["fields"]["status"] = {"id": "4", "name": "In Review", "statusCategory": {"key": "indeterminate"}}

    with respx.mock as router:
        router.get(f"{API}/issue/{item['external_id']}/transitions").mock(return_value=Response(200, json={"transitions": [
            {"id": "21", "to": {"id": "3", "statusCategory": {"key": "indeterminate"}}},
            {"id": "41", "to": {"id": "4", "statusCategory": {"key": "indeterminate"}}},
        ]}))
        post = router.post(f"{API}/issue/{item['external_id']}/transitions").mock(return_value=Response(204))
        router.get(f"{API}/issue/{item['external_id']}").mock(return_value=Response(200, json=in_review))
        resp = await client.post(f"/work-items/{item['_id']}/move", json={"column_id": column_id("In Review")}, headers=headers)

    # both are "in progress"; only the In Review status is in that column
    assert json.loads(post.calls[0].request.content) == {"transition": {"id": "41"}}
    assert resp.json()["data"]["column_id"] == column_id("In Review")


async def test_column_order_and_colors_are_kept_by_any_member(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await connect_jira(acme)
    board = (await boards(client, in_workspace(alice, acme)))["jira"]

    resp = await client.patch(
        f"/boards/{board['id']}/columns/settings",
        json={"column_order": ["done", "todo"], "column_colors": {"done": "purple"}},
        headers=in_workspace(bob, acme),  # a member, not an admin
    )

    columns = resp.json()["data"]["columns"]
    assert [c["id"] for c in columns] == ["done", "todo", "in_progress"]  # one left out keeps its place after
    assert [c["color"] for c in columns] == ["purple", None, None]
    reset = await client.patch(f"/boards/{board['id']}/columns/settings", json={"column_colors": {"done": None}}, headers=in_workspace(bob, acme))
    assert reset.json()["data"]["columns"][0]["color"] is None


async def test_column_settings_survive_the_trackers_columns_changing(client):
    headers = await signed_in_with_jira(client)
    await full_sync(headers["X-Workspace-Id"])
    board = (await boards(client, headers))["jira"]
    await client.post(f"/boards/{board['id']}/columns/tracker", headers=headers)
    await client.patch(
        f"/boards/{board['id']}/columns/settings",
        json={"column_order": [column_id("Done"), column_id("To Do")], "column_colors": {column_id("Done"): "slate"}},
        headers=headers,
    )

    # Jira adds a column; it joins after the ones the team ordered
    await full_sync(headers["X-Workspace-Id"], {"id": "4", "name": "In Review", "statusCategory": {"key": "indeterminate"}})

    columns = (await boards(client, headers))["jira"]["columns"]
    assert [c["name"] for c in columns] == ["Done", "To Do", "In Progress", "In Review"]
    assert columns[0]["color"] == "slate"


async def test_settings_for_columns_the_board_doesnt_have_are_refused(client):
    headers = await signed_in_with_jira(client)
    board = (await boards(client, headers))["jira"]

    unknown = await client.patch(f"/boards/{board['id']}/columns/settings", json={"column_order": ["nope"]}, headers=headers)
    bad_color = await client.patch(f"/boards/{board['id']}/columns/settings", json={"column_colors": {"todo": "pink"}}, headers=headers)

    assert unknown.json()["meta"]["error_code"] == "column_not_found"
    assert bad_color.status_code == 422


async def test_items_in_statuses_the_jira_board_leaves_out_are_hidden_as_in_jira(client):
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"], scopes=[*WRITE_SCOPES, *AGILE_SCOPES])
    await mongo.sync_targets().update_many({}, {"$set": {"last_full_sync_at": None}})

    # the Jira board maps To Do and In Progress, not Done (as a team might leave out "Archived")
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response(issues()))
        router.get(f"{API}/project/OVR/statuses").mock(return_value=statuses())
        router.get(f"{AGILE}/board").mock(return_value=Response(200, json={"values": [{"id": 9, "name": "OVR", "type": "kanban"}]}))
        router.get(f"{AGILE}/board/9/configuration").mock(return_value=Response(200, json={"columnConfig": {"columns": [
            {"name": "Ready", "statuses": [{"id": "1"}]}, {"name": "Working", "statuses": [{"id": "3"}]},
        ]}}))
        await jobs.run_connector_sync(headers["X-Workspace-Id"], "jira", "OVR")

    board = (await boards(client, headers))["jira"]
    listed = (await client.get(f"/work-items?board_id={board['id']}", headers=headers)).json()["data"]
    assert sorted(i["key"] for i in listed) == ["OVR-2", "OVR-3"]
    # kept, just off the board
    assert (await mongo.work_items().find_one({"key": "OVR-1"}))["tracker_hidden"] is True
