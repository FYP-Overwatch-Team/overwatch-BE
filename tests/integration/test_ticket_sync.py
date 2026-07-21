import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.db import mongo
from app.services import jira_service
from app.workers import jobs
from tests.integration.test_auth_flow import login

FIXTURES = Path(__file__).parent.parent / "regression" / "fixtures"
SEARCH_URL = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/search/jql"
SEARCH_PARAMS = {
    "jql": "project = OVR",
    "maxResults": 100,
    "fields": "summary,status,assignee,priority,updated",
}
USER = "user-1"

ISSUES = json.loads((FIXTURES / "sample_jira_tickets.json").read_text(encoding="utf-8"))["issues"]


@pytest.fixture
async def jira_connected():
    await jira_service.store_tokens(
        USER, {"access_token": "jira-access", "refresh_token": "r", "expires_in": 3600},
        cloud_id="cloud-1",
    )
    await mongo.jira_projects().insert_one({
        "user_id": USER, "cloud_id": "cloud-1", "project_key": "OVR",
        "sync_status": "pending", "sync_error": None, "last_synced_at": None,
        "connected_at": 1, "updated_at": 1,
    })


def search_response(issues: list, next_page_token: str | None = None) -> Response:
    body = {"issues": issues}
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return Response(200, json=body)


async def test_sync_paginates_and_stores_tickets(jira_connected):
    def paginated_response(request):
        if request.url.params.get("nextPageToken") == "page-2":
            return search_response(ISSUES[2:])
        return search_response(ISSUES[:2], next_page_token="page-2")

    with respx.mock as router:
        search = router.get(SEARCH_URL).mock(side_effect=paginated_response)
        await jobs.run_ticket_sync(USER, "OVR")

    assert len(search.calls) == 2
    first_page = search.calls[0].request.url.params
    second_page = search.calls[1].request.url.params
    assert first_page["jql"] == SEARCH_PARAMS["jql"]
    assert first_page["maxResults"] == str(SEARCH_PARAMS["maxResults"])
    assert first_page["fields"] == SEARCH_PARAMS["fields"]
    assert first_page.get("nextPageToken") is None
    assert second_page["nextPageToken"] == "page-2"
    assert await mongo.tickets().count_documents({}) == 3
    doc = await mongo.tickets().find_one({"ticket_key": "OVR-2"})
    assert doc["summary"] == "Fix login redirect loop"
    assert doc["status"] == "In Progress"
    assert doc["assignee"] == "Sam Lee"
    assert doc["raw"]["key"] == "OVR-2"

    project = await mongo.jira_projects().find_one({"project_key": "OVR"})
    assert project["sync_status"] == "done"
    assert project["last_synced_at"] is not None


async def test_resync_updates_and_removes_deleted_tickets(jira_connected):
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response(ISSUES))
        await jobs.run_ticket_sync(USER, "OVR")

    changed = json.loads(json.dumps(ISSUES[0]))
    changed["fields"]["status"] = {"name": "Reopened"}
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response([changed, ISSUES[1]]))
        await mongo.jira_projects().update_one(
            {"project_key": "OVR"}, {"$set": {"sync_status": "done"}})
        await jobs.run_ticket_sync(USER, "OVR")

    assert await mongo.tickets().count_documents({}) == 2  # OVR-3 gone
    doc = await mongo.tickets().find_one({"ticket_key": "OVR-1"})
    assert doc["status"] == "Reopened"


async def test_sync_failure_recorded(jira_connected):
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=Response(500, json={}))
        await jobs.run_ticket_sync(USER, "OVR")

    project = await mongo.jira_projects().find_one({"project_key": "OVR"})
    assert project["sync_status"] == "failed"
    assert project["sync_error"]


async def test_sync_lock_prevents_overlap(jira_connected):
    await mongo.jira_projects().update_one(
        {"project_key": "OVR"}, {"$set": {"sync_status": "in_progress"}})
    with respx.mock:  # strict: no outbound calls allowed
        await jobs.run_ticket_sync(USER, "OVR")
    assert await mongo.tickets().count_documents({}) == 0


async def seed_tickets_for(user_id: str) -> None:
    await mongo.jira_projects().insert_one({
        "user_id": user_id,
        "project_key": "OVR",
        "status": "active",
        "sync_status": "done",
        "connected_at": 1,
    })
    for issue in ISSUES:
        from app.services.ticket_service import _to_doc
        await mongo.tickets().insert_one(_to_doc(user_id, "OVR", issue))


async def test_ticket_list_filters_sort_and_pagination(client):
    access = await login(client)
    headers = {"Authorization": f"Bearer {access}"}
    user = await mongo.users().find_one({})
    await seed_tickets_for(user["_id"])

    resp = await client.get("/tickets", headers=headers)
    body = resp.json()
    assert body["total"] == 3
    assert [t["ticket_key"] for t in body["tickets"]] == ["OVR-3", "OVR-2", "OVR-1"]  # updated desc

    resp = await client.get("/tickets?status=In Progress", headers=headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["tickets"][0]["ticket_key"] == "OVR-2"

    resp = await client.get("/tickets?assignee=Alex Doe", headers=headers)
    assert resp.json()["tickets"][0]["ticket_key"] == "OVR-1"

    resp = await client.get("/tickets?page=2&page_size=2", headers=headers)
    body = resp.json()
    assert len(body["tickets"]) == 1
    assert body["tickets"][0]["ticket_key"] == "OVR-1"


async def test_tickets_scoped_to_user(client):
    access = await login(client)
    headers = {"Authorization": f"Bearer {access}"}
    await seed_tickets_for("someone-else")

    resp = await client.get("/tickets", headers=headers)
    assert resp.json()["total"] == 0


async def test_resync_all_projects_covers_every_connection(jira_connected):
    await mongo.jira_projects().insert_one({
        "user_id": "user-2", "cloud_id": "cloud-1", "project_key": "OTHER",
        "sync_status": "pending", "sync_error": None, "last_synced_at": None,
        "connected_at": 1, "updated_at": 1,
    })
    await jira_service.store_tokens(
        "user-2", {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600},
        cloud_id="cloud-1",
    )
    with respx.mock as router:
        router.get(SEARCH_URL).mock(return_value=search_response([]))
        await jobs.resync_all_projects()

    for key in ("OVR", "OTHER"):
        project = await mongo.jira_projects().find_one({"project_key": key})
        assert project["sync_status"] == "done"
