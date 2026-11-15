"""Syncing a tracker board on demand: opening it, or its refresh button."""

import asyncio
from datetime import timedelta

import respx

from app.connectors import sync
from app.core.time import utcnow
from app.db import mongo
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_work_item_sync import ISSUES, SEARCH_URL, connect_jira, search_response
from tests.integration.test_workspaces import in_workspace, join, team, user


async def jira_board(client, headers: dict) -> dict:
    boards = (await client.get("/boards", headers=headers)).json()["data"]
    return next(board for board in boards if board["source"] == "jira")


async def setup(client) -> tuple[dict, dict]:
    headers = auth_headers(await login(client))
    await connect_jira(headers["X-Workspace-Id"])
    return headers, await jira_board(client, headers)


async def sync_board(client, headers: dict, board_id: str, issues: list = ISSUES, force: bool = False) -> tuple[dict, respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(SEARCH_URL).mock(return_value=search_response(issues))
        resp = await client.post(f"/boards/{board_id}/sync", params={"force": str(force).lower()}, headers=headers)
    return resp.json(), route


async def test_the_first_sync_reads_everything_then_only_changes(client):
    headers, board = await setup(client)

    first, route = await sync_board(client, headers, board["id"])
    assert route.calls[0].request.url.params["jql"] == 'project = "OVR"'
    assert first["data"]["sync_status"] == "done" and first["data"]["last_synced_at"]
    assert await mongo.work_items().count_documents({}) == 3

    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"last_synced_at": utcnow() - timedelta(minutes=10)}})
    _, route = await sync_board(client, headers, board["id"], ISSUES[:1])
    # even with the hourly full sync due, a sync asked for here stays incremental
    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"last_full_sync_at": utcnow() - timedelta(hours=2), "last_synced_at": utcnow() - timedelta(minutes=10)}})
    _, due = await sync_board(client, headers, board["id"], ISSUES[:1])
    assert "AND updated >= -" in route.calls[0].request.url.params["jql"]
    assert "AND updated >= -" in due.calls[0].request.url.params["jql"]
    assert await mongo.work_items().count_documents({}) == 3


async def test_a_board_synced_moments_ago_is_left_alone_unless_refreshed(client):
    headers, board = await setup(client)
    await sync_board(client, headers, board["id"])

    _, skipped = await sync_board(client, headers, board["id"])
    _, forced = await sync_board(client, headers, board["id"], force=True)

    assert (skipped.call_count, forced.call_count) == (0, 1)


async def test_a_sync_already_running_is_not_started_twice(client):
    headers, board = await setup(client)
    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"sync_status": "in_progress", "sync_started_at": utcnow()}})

    body, route = await sync_board(client, headers, board["id"], force=True)

    assert route.call_count == 0 and body["data"]["sync_status"] == "in_progress"


async def test_a_sync_lost_to_a_restart_is_taken_over(client):
    headers, board = await setup(client)
    # one stuck for an hour, and one marked running before syncs recorded when they started
    for stuck in ({"sync_started_at": utcnow() - timedelta(hours=1)}, {"sync_started_at": None}):
        await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"sync_status": "in_progress", **stuck}})
        body, route = await sync_board(client, headers, board["id"], force=True)
        assert route.call_count == 1 and body["data"]["sync_status"] == "done"
    assert (await mongo.sync_targets().find_one({"key": "OVR"}))["sync_started_at"] is None


async def test_a_slow_sync_reports_itself_running_and_still_finishes(client, monkeypatch):
    headers, board = await setup(client)
    monkeypatch.setattr(sync, "WAIT_SECONDS", 0.01)

    async def slow(request):
        await asyncio.sleep(0.1)
        return search_response(ISSUES)

    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(side_effect=slow)
        resp = await client.post(f"/boards/{board['id']}/sync", headers=headers)
        assert resp.json()["data"]["sync_status"] == "in_progress"
        await asyncio.gather(*sync._running)

    assert (await mongo.sync_targets().find_one({"key": "OVR"}))["sync_status"] == "done"
    assert await mongo.work_items().count_documents({}) == 3


async def test_any_member_can_sync_but_only_tracker_boards_of_their_workspace(client):
    alice, bob, eve = await user(client, "alice"), await user(client, "bob"), await user(client, "eve")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    await connect_jira(acme)
    as_bob = in_workspace(bob, acme)
    boards = (await client.get("/boards", headers=as_bob)).json()["data"]
    native = next(b for b in boards if b["source"] == "native")
    tracker = next(b for b in boards if b["source"] == "jira")

    member, _ = await sync_board(client, as_bob, tracker["id"])
    mine, _ = await sync_board(client, as_bob, native["id"])
    other, _ = await sync_board(client, eve, tracker["id"])

    assert member["data"]["sync_status"] == "done"
    assert mine["meta"]["error_code"] == "board_not_synced"
    assert other["meta"]["error_code"] == "board_not_found"
