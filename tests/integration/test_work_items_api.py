"""The board API for native items, plus listing and visibility of mirrored ones."""

from app.connectors.jira import JiraAdapter
from app.db import mongo
from app.work_items import repository as work_items
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_work_item_sync import ISSUES, connect_jira
from tests.integration.test_workspaces import in_workspace, join, team, user

CONN = {"external_account": {"id": "cloud-1", "url": "https://acme.atlassian.net"}}


async def seed_jira(workspace_id: str) -> None:
    await connect_jira(workspace_id)
    await seed_items(workspace_id)


async def seed_items(workspace_id: str) -> None:
    for issue in ISSUES:
        await work_items.upsert_synced(workspace_id, JiraAdapter().normalise(CONN, "OVR", issue))


async def signed_in(client) -> dict:
    return auth_headers(await login(client))


async def board_id(client, headers: dict, source: str = "native") -> str:
    boards = (await client.get("/boards", headers=headers)).json()["data"]
    return next(board["id"] for board in boards if board["source"] == source)


async def create(client, headers: dict, **body) -> dict:
    body.setdefault("board_id", await board_id(client, headers))
    resp = await client.post("/work-items", json={"title": "Task", **body}, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()["data"]


async def keys(client, headers: dict, query: str = "") -> list[str]:
    return [i["key"] for i in (await client.get(f"/work-items{query}", headers=headers)).json()["data"]]


async def test_list_filters_sorts_by_board_position_and_paginates(client):
    headers = await signed_in(client)
    await seed_jira(headers["X-Workspace-Id"])

    resp = await client.get("/work-items", headers=headers)
    assert resp.json()["meta"] == {"total": 3, "page": 1, "page_size": 100}
    assert [i["key"] for i in resp.json()["data"]] == ["OVR-1", "OVR-2", "OVR-3"]  # oldest first
    assert all(i["editable"] for i in resp.json()["data"])
    assert "raw" not in resp.json()["data"][0]

    assert await keys(client, headers, "?status_category=in_progress") == ["OVR-2"]
    assert await keys(client, headers, "?assignee_id=acc-alex") == ["OVR-1"]
    assert await keys(client, headers, "?q=LOGIN") == ["OVR-2"]
    assert await keys(client, headers, "?q=ovr-3") == ["OVR-3"]
    assert await keys(client, headers, "?page=2&page_size=2") == ["OVR-3"]
    # an offset loads past a first batch of a different size
    assert await keys(client, headers, "?offset=1&page_size=1") == ["OVR-2"]


async def test_a_key_matches_exactly_and_only_on_its_board(client):
    headers = await signed_in(client)
    workspace = headers["X-Workspace-Id"]
    await seed_jira(workspace)
    # OVR-10 contains "OVR-1", which a substring search would also match
    await work_items.upsert_synced(workspace, JiraAdapter().normalise(CONN, "OVR", {**ISSUES[0], "id": "99010", "key": "OVR-10"}))
    jira_board, native_board = await board_id(client, headers, "jira"), await board_id(client, headers)
    native = await create(client, headers, title="Native")

    assert await keys(client, headers, "?key=OVR-1") == ["OVR-1"]
    assert await keys(client, headers, "?key=OVR") == []
    assert await keys(client, headers, "?key=ovr-1") == []
    assert await keys(client, headers, f"?board_id={jira_board}&key=OVR-2") == ["OVR-2"]
    assert await keys(client, headers, f"?board_id={native_board}&key=OVR-2") == []
    assert await keys(client, headers, f"?board_id={native_board}&key={native['key']}") == [native["key"]]

    too_long = await client.get(f"/work-items?key={'X' * 51}", headers=headers)
    assert too_long.status_code == 422


async def test_items_are_scoped_to_the_workspace(client):
    headers = await signed_in(client)
    await seed_jira("someone-else")
    other = (await mongo.work_items().find_one({}))["_id"]

    assert await keys(client, headers) == []
    resp = await client.patch(f"/work-items/{other}", json={"title": "x"}, headers=headers)
    assert resp.json()["meta"]["error_code"] == "work_item_not_found"


async def test_items_of_a_disconnected_target_are_hidden(client):
    headers = await signed_in(client)
    await seed_jira(headers["X-Workspace-Id"])
    await create(client, headers, title="Native")

    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"status": "disconnected"}})

    assert await keys(client, headers) == ["OW-1"]


async def test_native_items_get_workspace_keys_and_member_assignees(client):
    headers = await signed_in(client)
    me = headers["X-Workspace-Id"]

    first = await create(client, headers, title="Write docs", description="All of them", assignee_id=me)
    second = await create(client, headers, column_id="in_progress")
    refused = await client.post(
        "/work-items", json={"title": "x", "assignee_id": "stranger", "board_id": await board_id(client, headers)}, headers=headers,
    )

    assert (first["key"], second["key"]) == ("OW-1", "OW-2")
    assert first["source"] == "native" and first["editable"]
    assert first["assignee"] == {"id": me, "name": "Octo Cat"}
    assert (second["status_category"], second["column_id"]) == ("in_progress", "in_progress")
    assert refused.json()["meta"]["error_code"] == "member_not_found"


async def test_editing_changes_only_the_fields_sent(client):
    headers = await signed_in(client)
    item = await create(client, headers, title="Old", description="Keep me", assignee_id=headers["X-Workspace-Id"])

    resp = await client.patch(f"/work-items/{item['id']}", json={"title": "New", "assignee_id": None}, headers=headers)
    empty = await client.patch(f"/work-items/{item['id']}", json={"title": None}, headers=headers)

    assert {k: resp.json()["data"][k] for k in ("title", "description", "assignee")} == {
        "title": "New", "description": "Keep me", "assignee": None,
    }
    assert empty.status_code == 422


async def test_moving_places_a_card_between_its_neighbours(client):
    headers = await signed_in(client)
    a, b, c = [await create(client, headers, title=t) for t in "ABC"]

    async def move(item, category="todo", before=None, after=None):
        body = {"column_id": category, "before_id": before and before["id"], "after_id": after and after["id"]}
        resp = await client.post(f"/work-items/{item['id']}/move", json=body, headers=headers)
        assert resp.status_code == 200
        return resp.json()["data"]

    await move(c, before=a, after=b)
    assert await keys(client, headers, "?status_category=todo") == [a["key"], c["key"], b["key"]]

    moved = await move(a, "done")
    assert moved["status_category"] == "done"
    assert await keys(client, headers, "?status_category=done") == [a["key"]]

    # neighbours too close for a midpoint: the column is renumbered first
    await mongo.work_items().update_one({"_id": c["id"]}, {"$set": {"rank": 1.0}})
    await mongo.work_items().update_one({"_id": b["id"]}, {"$set": {"rank": 1.0 + 1e-9}})
    d = await create(client, headers, title="D")
    await move(d, before=c, after=b)
    assert await keys(client, headers, "?status_category=todo") == [c["key"], d["key"], b["key"]]


async def test_any_member_can_change_and_delete_items(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    item = await create(client, in_workspace(alice, acme), title="Alice's")

    edited = await client.patch(f"/work-items/{item['id']}", json={"title": "Bob's now"}, headers=in_workspace(bob, acme))
    deleted = await client.delete(f"/work-items/{item['id']}", headers=in_workspace(bob, acme))

    assert edited.json()["data"]["title"] == "Bob's now"
    assert deleted.status_code == 200
    assert await mongo.work_items().count_documents({}) == 0


async def test_a_drop_beside_one_card_lands_next_to_it_not_past_unseen_ones(client):
    """A partly loaded column only knows one neighbour; the card still goes right beside it."""
    headers = await signed_in(client)
    a, b, c, d = [await create(client, headers, title=t) for t in "ABCD"]
    for item, rank in ((a, 1.0), (b, 2.0), (c, 3.0), (d, 10.0)):
        await mongo.work_items().update_one({"_id": item["id"]}, {"$set": {"rank": rank}})

    async def move(item, **neighbours):
        body = {"column_id": "todo", "before_id": None, "after_id": None, **neighbours}
        return (await client.post(f"/work-items/{item['id']}/move", json=body, headers=headers)).json()["data"]

    # after A, with B unseen: between A and B, not at A + 1 (which would be past B)
    assert (await move(d, before_id=a["id"]))["rank"] == 1.5
    # before C, with B unseen above it: between B and C
    assert (await move(d, after_id=c["id"]))["rank"] == 2.5
    # after the real last card: one past it
    assert (await move(a, before_id=c["id"]))["rank"] == 4.0
    assert await keys(client, headers, "?column_id=todo") == [b["key"], d["key"], c["key"], a["key"]]


async def test_a_board_view_lists_its_lanes_and_filters_by_one(client):
    headers = await signed_in(client)
    await seed_jira(headers["X-Workspace-Id"])
    board = await board_id(client, headers, "jira")

    by_assignee = (await client.get(f"/work-items/lanes?board_id={board}&by=assignee", headers=headers)).json()["data"]
    by_priority = (await client.get(f"/work-items/lanes?board_id={board}&by=priority", headers=headers)).json()["data"]

    assert sorted((lane["key"], lane["total"]) for lane in by_assignee) == [("acc-alex", 1), ("acc-sam", 1), ("none", 1)]
    assert {lane["key"] for lane in by_priority} == {"High", "Highest", "Low"}
    assert next(lane for lane in by_assignee if lane["key"] == "acc-alex")["label"]
    assert await keys(client, headers, f"?board_id={board}&assignee_id=none") == ["OVR-3"]
    assert await keys(client, headers, f"?board_id={board}&priority=Highest") == ["OVR-2"]
