"""AI tools a member connected over MCP, and the tokens they sign in with.

A grant ties one tool to one member and one workspace, so a tool never chooses the workspace
it reads or records in. Tokens are opaque and looked up on every call, so revoking a grant,
leaving the workspace or deleting it takes effect on the tool's next request.
"""

import secrets
import uuid
from datetime import timedelta

from app.core.config import get_settings
from app.core.constants import MCP_TOKEN_PREFIX, McpGrantKind, McpScope
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import hash_token
from app.core.time import utcnow
from app.db import mongo
from app.workspaces import repository as workspaces

#: how much of a token is kept readable, to tell one from another in the list
SHOWN_PREFIX = len(MCP_TOKEN_PREFIX) + 4
LIVE = {"revoked_at": None}


async def create_token(member: dict, label: str, *, read_only: bool, days: int | None) -> tuple[dict, str]:
    """A personal token for the member's current workspace; the raw token is returned once and never stored."""
    settings = get_settings()
    user_id = member["user_id"]
    if await mongo.mcp_grants().count_documents({"user_id": user_id, **LIVE}) >= settings.mcp_max_connections_per_user:
        raise ConflictError("Remove a connected AI tool before adding another.", error_code="too_many_connections")
    now = utcnow()
    raw = MCP_TOKEN_PREFIX + secrets.token_urlsafe(32)
    grant = {
        "_id": uuid.uuid4().hex, "user_id": user_id, "workspace_id": member["workspace_id"],
        "kind": McpGrantKind.TOKEN, "label": label,
        "scopes": [McpScope.READ] if read_only else [McpScope.READ, McpScope.RECORD],
        "token_prefix": raw[:SHOWN_PREFIX], "created_at": now, "last_used_at": None, "records_count": 0,
        "expires_at": now + timedelta(days=days or settings.mcp_token_default_days), "revoked_at": None,
    }
    await mongo.mcp_grants().insert_one(grant)
    await mongo.mcp_tokens().insert_one({
        "_id": uuid.uuid4().hex, "token_hash": hash_token(raw), "grant_id": grant["_id"], "expires_at": grant["expires_at"],
    })
    return (await _with_workspace([grant]))[0], raw


async def _with_workspace(grants: list[dict]) -> list[dict]:
    ids = list({grant["workspace_id"] for grant in grants})
    names = {w["_id"]: w["name"] async for w in mongo.workspaces().find({"_id": {"$in": ids}}, {"name": 1})}
    return [{**grant, "id": grant["_id"], "workspace_name": names.get(grant["workspace_id"], "")} for grant in grants]


async def list_for_user(user_id: str) -> list[dict]:
    """The member's connected AI tools in every workspace, newest first; revoked ones are left out."""
    cursor = mongo.mcp_grants().find({"user_id": user_id, **LIVE}).sort([("created_at", -1), ("_id", 1)])
    return await _with_workspace([grant async for grant in cursor])


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
    token = await mongo.mcp_tokens().find_one({"token_hash": hash_token(raw)})
    now = utcnow()
    if token is None:
        return None
    grant = await mongo.mcp_grants().find_one({"_id": token["grant_id"], **LIVE, "expires_at": {"$gt": now}})
    if grant is None:
        return None
    # checked on every call, not only when access is taken away: a missed revoke must not leave a way in
    workspace = await workspaces.get(grant["workspace_id"])
    membership = await workspaces.membership(grant["workspace_id"], grant["user_id"]) if workspace else None
    if membership is None:
        return None
    await mongo.mcp_grants().update_one({"_id": grant["_id"]}, {"$set": {"last_used_at": now}})
    return {**grant, "role": membership["role"], "workspace": workspace}
