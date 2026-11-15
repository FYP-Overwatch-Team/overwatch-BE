"""AI tools a member connected over MCP, and the tokens they sign in with.

A grant ties one tool to one member and one workspace, so a tool never chooses the workspace
it reads or records in. Tokens are opaque and looked up on every call, so revoking a grant,
leaving the workspace or deleting it takes effect on the tool's next request.
"""

import secrets
import uuid
from datetime import datetime, timedelta

from pymongo import ReturnDocument

from app.core.config import get_settings
from app.core.constants import MCP_TOKEN_PREFIX, McpGrantKind, McpScope, McpTokenKind
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import hash_token
from app.core.time import utcnow
from app.db import mongo
from app.workspaces import repository as workspaces

#: how much of a token is kept readable, to tell one from another in the list
SHOWN_PREFIX = len(MCP_TOKEN_PREFIX) + 4
LIVE = {"revoked_at": None}


async def _new_grant(member: dict, kind: McpGrantKind, label: str, *, read_only: bool, ask_first: bool, **fields) -> dict:
    """A grant for the member's workspace, as stored; refused once they have as many as one person may."""
    user_id = member["user_id"]
    if await mongo.mcp_grants().count_documents({"user_id": user_id, **LIVE}) >= get_settings().mcp_max_connections_per_user:
        raise ConflictError("Remove a connected AI tool before adding another.", error_code="too_many_connections")
    grant = {
        "_id": uuid.uuid4().hex, "user_id": user_id, "workspace_id": member["workspace_id"],
        "kind": kind, "label": label, "ask_first": ask_first,
        "scopes": [McpScope.READ] if read_only else [McpScope.READ, McpScope.RECORD],
        "token_prefix": None, "created_at": utcnow(), "last_used_at": None, "records_count": 0,
        "expires_at": None, "revoked_at": None, **fields,
    }
    await mongo.mcp_grants().insert_one(grant)
    return grant


async def _store_token(grant: dict, raw: str, kind: McpTokenKind, expires_at: datetime) -> None:
    await mongo.mcp_tokens().insert_one({
        "_id": uuid.uuid4().hex, "token_hash": hash_token(raw), "grant_id": grant["_id"], "kind": kind,
        "expires_at": expires_at, "replaced_at": None,
    })


async def create_token(member: dict, label: str, *, read_only: bool, ask_first: bool, days: int | None) -> tuple[dict, str]:
    """A personal token for the member's current workspace; the raw token is returned once and never stored."""
    raw = MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=days or get_settings().mcp_token_default_days)
    grant = await _new_grant(
        member, McpGrantKind.TOKEN, label, read_only=read_only, ask_first=ask_first,
        token_prefix=raw[:SHOWN_PREFIX], expires_at=expires_at,
    )
    await _store_token(grant, raw, McpTokenKind.ACCESS, expires_at)
    return (await _with_workspace([grant]))[0], raw


async def create_signed_in(member: dict, client: dict, *, read_only: bool, ask_first: bool) -> dict:
    """A grant for a tool the member signed in to over OAuth, named after the tool; it lasts until revoked or left unused."""
    return await _new_grant(member, McpGrantKind.OAUTH, client["name"], read_only=read_only, ask_first=ask_first, client_id=client["_id"])


async def issue(grant: dict) -> dict:
    """A short-lived access token and the refresh token that gets the next one, as an OAuth token response."""
    settings = get_settings()
    now = utcnow()
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    await _store_token(grant, access, McpTokenKind.ACCESS, now + timedelta(minutes=settings.mcp_access_token_minutes))
    await _store_token(grant, refresh, McpTokenKind.REFRESH, now + timedelta(days=settings.mcp_refresh_token_days))
    return {
        "access_token": access, "token_type": "Bearer", "expires_in": settings.mcp_access_token_minutes * 60,
        "refresh_token": refresh, "scope": " ".join(grant["scopes"]),
    }


async def refresh(raw: str, client_id: str) -> dict | None:
    """Trade a refresh token for the next pair; None when it isn't a live one of that client.

    Each refresh token works once. One presented again was copied, so the whole grant is revoked.
    """
    token = await mongo.mcp_tokens().find_one_and_update(
        {"token_hash": hash_token(raw), "kind": McpTokenKind.REFRESH, "expires_at": {"$gt": utcnow()}, "replaced_at": None},
        {"$set": {"replaced_at": utcnow()}},
    )
    if token is None:
        reused = await mongo.mcp_tokens().find_one({"token_hash": hash_token(raw), "kind": McpTokenKind.REFRESH})
        if reused:
            await _revoke({"_id": reused["grant_id"]})
        return None
    grant = await mongo.mcp_grants().find_one({"_id": token["grant_id"], "client_id": client_id, **LIVE})
    return await issue(grant) if grant else None


async def _with_workspace(grants: list[dict]) -> list[dict]:
    ids = list({grant["workspace_id"] for grant in grants})
    names = {w["_id"]: w["name"] async for w in mongo.workspaces().find({"_id": {"$in": ids}}, {"name": 1})}
    return [{**grant, "id": grant["_id"], "workspace_name": names.get(grant["workspace_id"], "")} for grant in grants]


async def list_for_user(user_id: str) -> list[dict]:
    """The member's connected AI tools in every workspace, newest first; revoked ones are left out."""
    cursor = mongo.mcp_grants().find({"user_id": user_id, **LIVE}).sort([("created_at", -1), ("_id", 1)])
    return await _with_workspace([grant async for grant in cursor])


async def change(user_id: str, grant_id: str, fields: dict) -> dict:
    """Rename a connected tool, or change whether it asks before recording."""
    mine = {"_id": grant_id, "user_id": user_id, **LIVE}
    if fields:
        grant = await mongo.mcp_grants().find_one_and_update(mine, {"$set": fields}, return_document=ReturnDocument.AFTER)
    else:
        grant = await mongo.mcp_grants().find_one(mine)
    if grant is None:
        raise NotFoundError("connection not found", error_code="connection_not_found")
    return (await _with_workspace([grant]))[0]


async def _revoke(query: dict) -> int:
    """Revoked grants are kept, for the record of what a tool did; their tokens go."""
    ids = [grant["_id"] async for grant in mongo.mcp_grants().find({**query, **LIVE}, {"_id": 1})]
    if ids:
        await mongo.mcp_grants().update_many({"_id": {"$in": ids}}, {"$set": {"revoked_at": utcnow()}})
        await mongo.mcp_tokens().delete_many({"grant_id": {"$in": ids}})
    return len(ids)


async def revoke(user_id: str, grant_id: str) -> None:
    if not await _revoke({"_id": grant_id, "user_id": user_id}):
        raise NotFoundError("connection not found", error_code="connection_not_found")


async def revoke_for_member(workspace_id: str, user_id: str) -> None:
    """Someone left or was removed: their tools stop working in that workspace."""
    await _revoke({"workspace_id": workspace_id, "user_id": user_id})


async def revoke_for_workspace(workspace_id: str) -> None:
    await _revoke({"workspace_id": workspace_id})


async def authenticate(raw: str) -> dict | None:
    """The live grant behind a token, with its member's role now; None when the token, grant or membership is gone."""
    now = utcnow()
    token = await mongo.mcp_tokens().find_one(
        {"token_hash": hash_token(raw), "kind": {"$ne": McpTokenKind.REFRESH}, "expires_at": {"$gt": now}},
    )
    grant = await mongo.mcp_grants().find_one({"_id": token["grant_id"], **LIVE}) if token else None
    if grant is None or (grant["expires_at"] and grant["expires_at"] <= now):
        return None
    # checked on every call, not only when access is taken away: a missed revoke must not leave a way in
    workspace = await workspaces.get(grant["workspace_id"])
    membership = await workspaces.membership(grant["workspace_id"], grant["user_id"]) if workspace else None
    if membership is None:
        return None
    await mongo.mcp_grants().update_one({"_id": grant["_id"]}, {"$set": {"last_used_at": now}})
    return {**grant, "role": membership["role"], "workspace": workspace}
