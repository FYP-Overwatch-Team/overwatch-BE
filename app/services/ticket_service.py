from datetime import datetime, timezone

import structlog

from app.core.exceptions import ExternalServiceError
from app.db import mongo
from app.integrations.jira_client import get_jira_client
from app.services import jira_service

logger = structlog.get_logger("app.tickets")

PAGE_SIZE = 100  # Jira caps search results at 100/page


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_all_tickets(user_id: str, project_key: str) -> list[dict]:
    access_token, cloud_id = await jira_service.get_access_token(user_id)
    client = get_jira_client()
    issues: list[dict] = []
    start_at = 0
    while True:
        resp = await client.api_get(
            access_token, cloud_id, "/rest/api/3/search",
            jql=f"project = {project_key}",
            startAt=start_at,
            maxResults=PAGE_SIZE,
            fields="summary,status,assignee,priority,updated",
        )
        if resp.status_code != 200:
            raise ExternalServiceError("Jira ticket search failed", error_code="jira_search_failed")
        body = resp.json()
        batch = body.get("issues", [])
        issues.extend(batch)
        start_at += len(batch)
        if start_at >= body.get("total", 0) or not batch:
            return issues


def _to_doc(user_id: str, project_key: str, issue: dict) -> dict:
    fields = issue.get("fields", {})
    return {
        "user_id": user_id,
        "project_key": project_key,
        "ticket_key": issue["key"],
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "priority": (fields.get("priority") or {}).get("name"),
        "updated_at": fields.get("updated"),
        "raw": issue,
    }


async def sync_project(user_id: str, project_key: str) -> int:
    """Pull every ticket for the project into Mongo. Stored mostly as-is (raw kept)."""
    issues = await fetch_all_tickets(user_id, project_key)
    seen_keys = []
    for issue in issues:
        doc = _to_doc(user_id, project_key, issue)
        seen_keys.append(doc["ticket_key"])
        await mongo.tickets().update_one(
            {"user_id": user_id, "ticket_key": doc["ticket_key"]},
            {"$set": doc},
            upsert=True,
        )
    # tickets deleted in Jira disappear on the next sync
    await mongo.tickets().delete_many({
        "user_id": user_id, "project_key": project_key, "ticket_key": {"$nin": seen_keys},
    })
    return len(seen_keys)


async def list_tickets(
    user_id: str,
    project_key: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict:
    query: dict = {"user_id": user_id}
    if project_key:
        query["project_key"] = project_key
    if status:
        query["status"] = status
    if assignee:
        query["assignee"] = assignee

    total = await mongo.tickets().count_documents(query)
    cursor = (
        mongo.tickets().find(query)
        .sort("updated_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    tickets = [
        {
            "ticket_key": d["ticket_key"],
            "project_key": d["project_key"],
            "summary": d["summary"],
            "status": d["status"],
            "assignee": d["assignee"],
            "priority": d["priority"],
            "updated_at": d["updated_at"],
        }
        async for d in cursor
    ]
    return {"tickets": tickets, "total": total, "page": page, "page_size": page_size}
