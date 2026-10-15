"""Workspaces, their members and invite links.

A soft-deleted workspace keeps all its data but is invisible: every lookup here skips it.
"""

import uuid
from datetime import datetime

from pymongo import ReturnDocument

from app.auth import usernames
from app.core.constants import InviteKind, Role
from app.core.time import utcnow
from app.db import mongo

LIVE = {"deleted_at": None}


async def ensure_personal(user: dict) -> None:
    """Create the user's personal workspace once; it shares the user's id, so reruns are no-ops."""
    now = utcnow()
    await mongo.workspaces().update_one(
        {"_id": user["_id"]},
        {"$setOnInsert": {
            "name": usernames.display_name(user, "Personal"),
            "personal": True,
            "created_by": user["_id"],
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }},
        upsert=True,
    )
    await add_member(user["_id"], user["_id"], Role.OWNER)


async def create(name: str, created_by: str) -> dict:
    now = utcnow()
    doc = {
        "_id": uuid.uuid4().hex, "name": name, "personal": False, "created_by": created_by,
        "created_at": now, "updated_at": now, "deleted_at": None,
    }
    await mongo.workspaces().insert_one(doc)
    await add_member(doc["_id"], created_by, Role.OWNER)
    return doc


async def get(workspace_id: str) -> dict | None:
    return await mongo.workspaces().find_one({"_id": workspace_id, **LIVE})


async def rename(workspace_id: str, name: str) -> None:
    await mongo.workspaces().update_one({"_id": workspace_id}, {"$set": {"name": name, "updated_at": utcnow()}})


async def soft_delete(workspace_id: str) -> None:
    now = utcnow()
    await mongo.workspaces().update_one({"_id": workspace_id}, {"$set": {"deleted_at": now, "updated_at": now}})


async def add_member(workspace_id: str, user_id: str, role: str) -> None:
    """Add a member; an existing membership keeps its role."""
    await mongo.workspace_members().update_one(
        {"workspace_id": workspace_id, "user_id": user_id},
        {"$setOnInsert": {"role": role, "joined_at": utcnow()}},
        upsert=True,
    )


async def membership(workspace_id: str, user_id: str) -> dict | None:
    return await mongo.workspace_members().find_one({"workspace_id": workspace_id, "user_id": user_id})


async def list_for_user(user_id: str) -> list[tuple[dict, dict]]:
    """(workspace, membership) pairs, oldest membership first."""
    memberships = [doc async for doc in mongo.workspace_members().find({"user_id": user_id}).sort("joined_at", 1)]
    workspaces = {
        doc["_id"]: doc
        async for doc in mongo.workspaces().find({"_id": {"$in": [m["workspace_id"] for m in memberships]}, **LIVE})
    }
    return [(workspaces[m["workspace_id"]], m) for m in memberships if m["workspace_id"] in workspaces]


async def list_members(workspace_id: str) -> list[dict]:
    return [doc async for doc in mongo.workspace_members().find({"workspace_id": workspace_id}).sort("joined_at", 1)]


async def count_owners(workspace_id: str) -> int:
    return await mongo.workspace_members().count_documents({"workspace_id": workspace_id, "role": Role.OWNER})


async def set_role(workspace_id: str, user_id: str, role: str) -> None:
    await mongo.workspace_members().update_one({"workspace_id": workspace_id, "user_id": user_id}, {"$set": {"role": role}})


async def remove_member(workspace_id: str, user_id: str) -> None:
    await mongo.workspace_members().delete_one({"workspace_id": workspace_id, "user_id": user_id})


async def insert_invite(doc: dict) -> None:
    await mongo.workspace_invites().insert_one(doc)


async def list_invites(workspace_id: str) -> list[dict]:
    query = {"workspace_id": workspace_id, "accepted_by": None, "revoked": False, "expires_at": {"$gt": utcnow()}}
    return [doc async for doc in mongo.workspace_invites().find(query).sort("created_at", -1)]


async def revoke_invite(workspace_id: str, invite_id: str) -> bool:
    result = await mongo.workspace_invites().update_one(
        {"_id": invite_id, "workspace_id": workspace_id}, {"$set": {"revoked": True}, "$unset": {"pending_email": ""}},
    )
    return result.matched_count > 0


async def release_expired_email_invites(workspace_id: str) -> None:
    """Expired invites stop holding their email, so it can be invited again."""
    await mongo.workspace_invites().update_many(
        {"workspace_id": workspace_id, "pending_email": {"$type": "string"}, "expires_at": {"$lte": utcnow()}},
        {"$unset": {"pending_email": ""}},
    )


async def find_open_email_invite(workspace_id: str, invite_id: str) -> dict | None:
    return await mongo.workspace_invites().find_one({
        "_id": invite_id, "workspace_id": workspace_id, "kind": InviteKind.EMAIL, "accepted_by": None, "revoked": False,
        "expires_at": {"$gt": utcnow()},
    })


async def renew_invite(invite_id: str, last_sent_at: datetime, fields: dict) -> dict | None:
    """A fresh token and expiry, unless another resend got there first."""
    return await mongo.workspace_invites().find_one_and_update(
        {"_id": invite_id, "last_sent_at": last_sent_at},
        {"$set": fields, "$inc": {"send_count": 1}},
        return_document=ReturnDocument.AFTER,
    )


async def find_open_invite(token_hash: str) -> dict | None:
    return await mongo.workspace_invites().find_one(
        {"token_hash": token_hash, "accepted_by": None, "revoked": False, "expires_at": {"$gt": utcnow()}},
    )


async def claim_invite(token_hash: str, user_id: str, now: datetime) -> dict | None:
    """Use up an open invite in one write, so a single-use link can't be accepted twice."""
    return await mongo.workspace_invites().find_one_and_update(
        {"token_hash": token_hash, "accepted_by": None, "revoked": False, "expires_at": {"$gt": now}},
        {"$set": {"accepted_by": user_id, "accepted_at": now}, "$unset": {"pending_email": ""}},
        return_document=ReturnDocument.AFTER,
    )
