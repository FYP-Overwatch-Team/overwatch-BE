import json
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.connectors.jira import FIELDS
from app.core.time import utcnow
from app.db import mongo, oauth_tokens
from app.workers import jobs

FIXTURES = Path(__file__).parent.parent / "regression" / "fixtures"
SEARCH_URL = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/search/jql"
WS = "user-1"
WRITE_SCOPES = ["read:jira-work", "write:jira-work", "read:jira-user", "offline_access"]

ISSUES = json.loads((FIXTURES / "sample_jira_tickets.json").read_text(encoding="utf-8"))["issues"]


async def connect_jira(workspace_id: str, scopes: list[str] = WRITE_SCOPES, key: str = "OVR") -> None:
    await oauth_tokens.save(workspace_id, "atlassian", {"access_token": "jira-access", "refresh_token": "r", "expires_in": 3600})
    await mongo.connections().insert_one({
        "_id": f"conn-{workspace_id}", "workspace_id": workspace_id, "provider": "jira", "auth_backend": "native_oauth",
        "status": "active", "scopes": scopes, "connected_by": workspace_id,
        "external_account": {"id": "cloud-1", "name": "acme", "url": "https://acme.atlassian.net"},
    })
    await mongo.sync_targets().insert_one({
        "workspace_id": workspace_id, "provider": "jira", "key": key, "name": None, "status": "active",
        "sync_status": "pending", "sync_error": None, "last_synced_at": None, "last_full_sync_at": None,
        "connected_at": utcnow(),
    })


@pytest.fixture
async def jira_connected():
    await connect_jira(WS)


def search_response(issues: list, next_page_token: str | None = None) -> Response:
    body = {"issues": issues}
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return Response(200, json=body)


async def target() -> dict:
    return await mongo.sync_targets().find_one({"key": "OVR"})


async def sync(issues: list) -> respx.Route:
    with respx.mock as router:
        route = router.get(SEARCH_URL).mock(return_value=search_response(issues))
        await jobs.run_connector_sync(WS, "jira", "OVR")
    return route


async def test_the_first_sync_is_full_paginated_and_normalised(jira_connected):
    def paginated(request):
        if request.url.params.get("nextPageToken") == "page-2":
            return search_response(ISSUES[2:])
        return search_response(ISSUES[:2], next_page_token="page-2")

    with respx.mock as router:
        search = router.get(SEARCH_URL).mock(side_effect=paginated)
        await jobs.run_connector_sync(WS, "jira", "OVR")

    first, second = (call.request.url.params for call in search.calls)
    assert (first["jql"], first["maxResults"], first["fields"]) == ('project = "OVR"', "100", FIELDS)
    assert first.get("nextPageToken") is None and second["nextPageToken"] == "page-2"

    assert await mongo.work_items().count_documents({}) == 3
    item = await mongo.work_items().find_one({"key": "OVR-2"})
    assert {k: item[k] for k in ("source", "external_id", "target_key", "title", "status", "status_category", "assignee")} == {
        "source": "jira", "external_id": "10002", "target_key": "OVR", "title": "Fix login redirect loop",
        "status": "In Progress", "status_category": "in_progress", "assignee": {"id": "acc-sam", "name": "Sam Lee"},
    }
    assert item["external_url"] == "https://acme.atlassian.net/browse/OVR-2"
    assert item["raw"]["key"] == "OVR-2"

    synced = await target()
    assert synced["sync_status"] == "done"
    assert synced["last_synced_at"] == synced["last_full_sync_at"] is not None


async def test_a_recent_sync_reads_only_updated_items_and_keeps_the_rest(jira_connected):
    await sync(ISSUES)
    changed = json.loads(json.dumps(ISSUES[0]))
    changed["fields"]["status"] = {"name": "Reopened", "statusCategory": {"key": "indeterminate"}}

    route = await sync([changed])

    assert "AND updated >= -" in route.calls[0].request.url.params["jql"]
    assert await mongo.work_items().count_documents({}) == 3  # nothing swept on an incremental run
    item = await mongo.work_items().find_one({"key": "OVR-1"})
    assert (item["status"], item["status_category"]) == ("Reopened", "in_progress")


async def test_an_hourly_full_sync_drops_items_deleted_in_jira(jira_connected):
    await sync(ISSUES)
    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"last_full_sync_at": utcnow() - timedelta(hours=2)}})

    route = await sync(ISSUES[:2])

    assert route.calls[0].request.url.params["jql"] == 'project = "OVR"'
    assert {d["key"] async for d in mongo.work_items().find({})} == {"OVR-1", "OVR-2"}


async def test_a_resync_keeps_the_board_position(jira_connected):
    await sync(ISSUES)
    await mongo.work_items().update_one({"key": "OVR-1"}, {"$set": {"rank": 0.5}})

    await sync(ISSUES)

    assert (await mongo.work_items().find_one({"key": "OVR-1"}))["rank"] == 0.5


async def test_sync_failure_recorded(jira_connected):
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=Response(500, json={"errorMessages": ["boom"]}))
        await jobs.run_connector_sync(WS, "jira", "OVR")

    failed = await target()
    assert failed["sync_status"] == "failed"
    assert "boom" in failed["sync_error"]


async def test_a_disconnected_connection_fails_the_sync(jira_connected):
    await mongo.connections().update_one({"provider": "jira"}, {"$set": {"status": "disconnected"}})
    with respx.mock:  # strict: no outbound calls allowed
        await jobs.run_connector_sync(WS, "jira", "OVR")
    assert (await target())["sync_status"] == "failed"


async def test_sync_lock_prevents_overlap(jira_connected):
    await mongo.sync_targets().update_one({"key": "OVR"}, {"$set": {"sync_status": "in_progress"}})
    with respx.mock:
        await jobs.run_connector_sync(WS, "jira", "OVR")
    assert await mongo.work_items().count_documents({}) == 0


async def test_resync_all_covers_every_target(jira_connected):
    await connect_jira("user-2", key="OTHER")
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response([]))
        await jobs.resync_all_targets()

    assert {t["key"] async for t in mongo.sync_targets().find({"sync_status": "done"})} == {"OVR", "OTHER"}
