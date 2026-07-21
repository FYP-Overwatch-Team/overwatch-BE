from datetime import datetime, timedelta, timezone

import structlog

from app.core import security
from app.core.exceptions import ConflictError, NotFoundError, UnauthorizedError
from app.db import mongo
from app.integrations.jira_client import get_jira_client

logger = structlog.get_logger("app.jira")

# Refresh proactively when the access token is within this window of expiry,
# so a token never dies mid-request (or mid-demo).
REFRESH_BUFFER = timedelta(minutes=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def store_tokens(user_id: str, token_response: dict, cloud_id: str | None = None) -> None:
    """Persist an Atlassian token response. Always overwrites the refresh token —
    Atlassian rotates it on every refresh and the old one becomes useless."""
    fields = {
        "access_token_encrypted": security.encrypt_secret(token_response["access_token"]),
        "expires_at": _now() + timedelta(seconds=token_response.get("expires_in", 3600)),
        "status": "active",
        "updated_at": _now(),
    }
    if token_response.get("refresh_token"):
        fields["refresh_token_encrypted"] = security.encrypt_secret(token_response["refresh_token"])
    if cloud_id is not None:
        fields["cloud_id"] = cloud_id
    await mongo.oauth_tokens().update_one(
        {"user_id": user_id, "provider": "jira"},
        {"$set": fields},
        upsert=True,
    )


async def get_access_token(user_id: str) -> tuple[str, str]:
    """Return (access_token, cloud_id), refreshing proactively near expiry."""
    doc = await mongo.oauth_tokens().find_one({"user_id": user_id, "provider": "jira"})
    if doc is None or doc.get("status") == "needs_reauth":
        raise UnauthorizedError("Jira connection needs re-authorization", error_code="jira_needs_reauth")

    expires_at = doc["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at - REFRESH_BUFFER <= _now():
        refresh_raw = security.decrypt_secret(doc["refresh_token_encrypted"])
        try:
            token_response = await get_jira_client().refresh_tokens(refresh_raw)
        except UnauthorizedError:
            await mongo.oauth_tokens().update_one(
                {"user_id": user_id, "provider": "jira"},
                {"$set": {"status": "needs_reauth", "updated_at": _now()}},
            )
            raise
        await store_tokens(user_id, token_response)
        logger.info("jira_token_refreshed_proactively", user_id=user_id)
        doc = await mongo.oauth_tokens().find_one({"user_id": user_id, "provider": "jira"})

    return security.decrypt_secret(doc["access_token_encrypted"]), doc.get("cloud_id")


async def complete_oauth(user_id: str, code: str) -> dict:
    client = get_jira_client()
    token_response = await client.exchange_code(code)
    resources = await client.get_accessible_resources(token_response["access_token"])
    if not resources:
        raise NotFoundError("no accessible Jira sites for this account", error_code="no_jira_sites")
    # Eval-1 simplification: use the first accessible site.
    site = resources[0]
    await store_tokens(user_id, token_response, cloud_id=site["id"])
    return {"cloud_id": site["id"], "site_name": site.get("name"), "site_url": site.get("url")}


async def list_projects(user_id: str) -> list[dict]:
    access_token, cloud_id = await get_access_token(user_id)
    resp = await get_jira_client().api_get(access_token, cloud_id, "/rest/api/3/project/search", maxResults=100)
    if resp.status_code != 200:
        raise NotFoundError("could not list Jira projects", error_code="jira_projects_unavailable")
    return [
        {"key": p["key"], "name": p.get("name"), "id": p.get("id")}
        for p in resp.json().get("values", [])
    ]


async def connect_project(user_id: str, cloud_id: str, project_key: str) -> dict:
    existing = await mongo.jira_projects().find_one({"user_id": user_id, "project_key": project_key})
    if existing:
        raise ConflictError("project already connected", error_code="project_already_connected")
    now = _now()
    doc = {
        "user_id": user_id,
        "cloud_id": cloud_id,
        "project_key": project_key,
        "project_name": None,
        "sync_status": "pending",
        "sync_error": None,
        "last_synced_at": None,
        "connected_at": now,
        "updated_at": now,
    }
    await mongo.jira_projects().insert_one(doc)
    return doc
