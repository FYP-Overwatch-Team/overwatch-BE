"""⌘K search: board items by key, title or description, and docs pages by title or text."""

from app.connectors.jira import JiraAdapter
from app.db import mongo
from app.search.service import snippet
from app.work_items import repository as work_items
from tests.integration.test_decisions_api import page
from tests.integration.test_work_item_sync import ISSUES
from tests.integration.test_work_items_api import CONN, board_id, seed_jira, signed_in
from tests.integration.test_work_items_api import create as create_item
from tests.integration.test_workspaces import user


async def search(client, headers: dict, q: str, **params) -> list[dict]:
    resp = await client.get("/search", params={"q": q, **params}, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["data"]


async def test_an_exact_key_comes_first_and_opens_on_its_board(client):
    headers = await signed_in(client)
    workspace = headers["X-Workspace-Id"]
    await seed_jira(workspace)
    await work_items.upsert_synced(workspace, JiraAdapter().normalise(CONN, "OVR", {**ISSUES[0], "id": "99010", "key": "OVR-10"}))

    found = await search(client, headers, "ovr-1")

    assert [r["key"] for r in found][:2] == ["OVR-1", "OVR-10"]
    first = found[0]
    assert (first["kind"], first["source"], first["snippet"]) == ("work_item", "jira", None)
    # no board synced for it yet: it opens on the board page by key alone
    assert first["board_id"] is None


async def test_an_exact_key_is_found_among_many_newer_keys_starting_with_it(client):
    headers = await signed_in(client)
    workspace = headers["X-Workspace-Id"]
    await seed_jira(workspace)
    for n in range(100, 160):
        issue = {**ISSUES[0], "id": str(90000 + n), "key": f"OVR-1{n}"}
        await work_items.upsert_synced(workspace, JiraAdapter().normalise(CONN, "OVR", issue))

    found = await search(client, headers, "ovr-1")

    assert found[0]["key"] == "OVR-1"


async def test_text_inside_an_item_or_page_is_found_with_the_passage_around_it(client):
    headers = await signed_in(client)
    item = await create_item(client, headers, title="Imports", description="Order imports become **job-based** and asynchronous.")
    body = await page(client, headers, md="# Plan\n\nWe keep the [CSV preview](https://x.test) while imports run in the background.")
    titled = await page(client, headers, md="Background jobs are covered elsewhere.")
    await mongo.documents().update_one({"_id": titled["id"]}, {"$set": {"title": "Background jobs"}})

    found = await search(client, headers, "background")

    assert [(r["kind"], r["id"]) for r in found] == [("document", titled["id"]), ("document", body["id"])]
    assert found[0]["snippet"] is None  # matched on its title
    assert found[1]["snippet"] == "Plan We keep the CSV preview while imports run in the background."
    [hit] = await search(client, headers, "job-based")
    assert (hit["id"], hit["key"], hit["board_id"]) == (item["id"], item["key"], await board_id(client, headers))
    assert hit["snippet"] == "Order imports become job-based and asynchronous."


async def test_only_what_the_workspace_can_see_is_found(client):
    headers, other = await signed_in(client), await user(client, "someone-else")
    archived = await page(client, headers, md="Secret export rules.")
    await mongo.documents().update_one({"_id": archived["id"]}, {"$set": {"archived_at": archived["created_at"]}})
    await create_item(client, other, title="Export rules", description="Theirs.")
    # a tracker project that isn't connected (any more)
    await work_items.upsert_synced(headers["X-Workspace-Id"], JiraAdapter().normalise(CONN, "GONE", {**ISSUES[0], "id": "5", "key": "GONE-1"}))

    assert await search(client, headers, "export rules") == []
    assert await search(client, headers, "GONE-1") == []


async def test_the_query_is_plain_text_and_needs_two_characters(client):
    headers = await signed_in(client)
    await create_item(client, headers, title="Totals (net)", description="x")

    assert [r["title"] for r in await search(client, headers, "(net")] == ["Totals (net)"]
    assert (await client.get("/search", params={"q": "a"}, headers=headers)).status_code == 422


def test_a_snippet_is_cut_around_the_match():
    text = "a" * 100 + " needle " + "b" * 100

    cut = snippet(text, "NEEDLE")

    assert cut.startswith("…") and cut.endswith("…") and "needle" in cut
    assert snippet("nothing here", "needle") is None
