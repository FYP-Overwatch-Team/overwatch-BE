"""Workspace rules: who may do what, and the invariants that keep a workspace usable.

- a workspace always keeps at least one owner
- a personal workspace can't be deleted, and its creator can't leave it
- nobody grants or removes a role above their own
"""

import secrets
import uuid
from datetime import timedelta

from app.auth.repository import get_user
from app.core import security
from app.core.config import get_settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.time import utcnow
from app.workspaces import repository

ROLES = ("member", "admin", "owner")
INVITE_TTL = timedelta(days=7)


def rank(role: str) -> int:
    return ROLES.index(role)


def to_workspace(workspace: dict, membership: dict) -> dict:
    return {"id": workspace["_id"], "name": workspace["name"], "personal": workspace["personal"], "role": membership["role"]}


async def require_member(workspace_id: str, user_id: str, min_role: str = "member") -> dict:
    """The caller's membership; 404 to non-members so a workspace's existence never leaks."""
    workspace = await repository.get(workspace_id)
    membership = await repository.membership(workspace_id, user_id) if workspace else None
    if membership is None:
        raise NotFoundError("workspace not found", error_code="workspace_not_found")
    if rank(membership["role"]) < rank(min_role):
        raise ForbiddenError(f"requires the {min_role} role", error_code="insufficient_role")
    return {**membership, "workspace": workspace}


async def list_workspaces(user_id: str) -> list[dict]:
    return [to_workspace(workspace, membership) for workspace, membership in await repository.list_for_user(user_id)]


async def create(user_id: str, name: str) -> dict:
    workspace = await repository.create(name, user_id)
    return to_workspace(workspace, {"role": "owner"})


async def rename(member: dict, name: str) -> dict:
    await repository.rename(member["workspace_id"], name)
    return to_workspace({**member["workspace"], "name": name}, member)


async def delete(member: dict) -> None:
    """Soft delete: hidden from everyone, but repos, tickets and graphs are kept."""
    if member["workspace"]["personal"]:
        raise ConflictError("a personal workspace can't be deleted", error_code="personal_workspace")
    await repository.soft_delete(member["workspace_id"])


async def list_members(workspace_id: str) -> list[dict]:
    members = []
    for membership in await repository.list_members(workspace_id):
        user = await get_user(membership["user_id"])
        if user:
            members.append({
                "user_id": user["_id"], "github_login": user["github_login"], "name": user.get("name"),
                "avatar_url": user.get("avatar_url"), "role": membership["role"], "joined_at": membership["joined_at"],
            })
    return members


async def _target(workspace_id: str, user_id: str) -> dict:
    target = await repository.membership(workspace_id, user_id)
    if target is None:
        raise NotFoundError("member not found", error_code="member_not_found")
    return target


async def _keep_an_owner(workspace_id: str, target: dict) -> None:
    if target["role"] == "owner" and await repository.count_owners(workspace_id) <= 1:
        raise ConflictError("a workspace needs at least one owner", error_code="last_owner")


async def set_role(actor: dict, user_id: str, role: str) -> None:
    workspace_id = actor["workspace_id"]
    target = await _target(workspace_id, user_id)
    if max(rank(role), rank(target["role"])) > rank(actor["role"]):
        raise ForbiddenError("can't change a role above your own", error_code="insufficient_role")
    if role != "owner":
        await _keep_an_owner(workspace_id, target)
    await repository.set_role(workspace_id, user_id, role)


async def remove_member(actor: dict, user_id: str) -> None:
    """Admins remove others; anyone may leave."""
    workspace_id = actor["workspace_id"]
    target = await _target(workspace_id, user_id)
    leaving = user_id == actor["user_id"]
    if not leaving and (rank(actor["role"]) < rank("admin") or rank(target["role"]) > rank(actor["role"])):
        raise ForbiddenError("can't remove this member", error_code="insufficient_role")
    if actor["workspace"]["personal"] and user_id == actor["workspace"]["created_by"]:
        raise ConflictError("you can't leave your personal workspace", error_code="personal_workspace")
    await _keep_an_owner(workspace_id, target)
    await repository.remove_member(workspace_id, user_id)


def _invite(doc: dict) -> dict:
    return {key: doc[key] for key in ("role", "github_login", "created_at", "expires_at")} | {"id": doc["_id"]}


async def create_invite(actor: dict, role: str, github_login: str | None) -> dict:
    if rank(role) > rank(actor["role"]):
        raise ForbiddenError("can't invite above your own role", error_code="insufficient_role")
    token = secrets.token_urlsafe(32)
    now = utcnow()
    doc = {
        "_id": uuid.uuid4().hex,
        "workspace_id": actor["workspace_id"],
        "token_hash": security.hash_refresh_token(token),
        "role": role,
        "github_login": github_login,
        "invited_by": actor["user_id"],
        "created_at": now,
        "expires_at": now + INVITE_TTL,
        "accepted_by": None,
        "revoked": False,
    }
    await repository.insert_invite(doc)
    return {**_invite(doc), "url": f"{get_settings().frontend_origin}/invite/{token}"}


async def list_invites(workspace_id: str) -> list[dict]:
    return [_invite(doc) for doc in await repository.list_invites(workspace_id)]


async def revoke_invite(workspace_id: str, invite_id: str) -> None:
    if not await repository.revoke_invite(workspace_id, invite_id):
        raise NotFoundError("invite not found", error_code="invite_not_found")


async def _open_invite(token: str, user: dict) -> tuple[dict, dict]:
    invite = await repository.find_open_invite(security.hash_refresh_token(token))
    workspace = invite and await repository.get(invite["workspace_id"])
    if not workspace:
        raise NotFoundError("this invite is invalid or has expired", error_code="invite_invalid")
    if invite["github_login"] and invite["github_login"].lower() != user["github_login"].lower():
        raise ForbiddenError("this invite is for another GitHub account", error_code="invite_wrong_account")
    return invite, workspace


async def preview_invite(token: str, user: dict) -> dict:
    invite, workspace = await _open_invite(token, user)
    inviter = await get_user(invite["invited_by"])
    return {
        "workspace_name": workspace["name"],
        "invited_by": inviter["github_login"] if inviter else "a former member",
        "role": invite["role"],
    }


async def accept_invite(token: str, user: dict) -> dict:
    invite, workspace = await _open_invite(token, user)
    # an existing member keeps their role and leaves the link unused
    if existing := await repository.membership(workspace["_id"], user["_id"]):
        return to_workspace(workspace, existing)
    if await repository.claim_invite(invite["token_hash"], user["_id"], utcnow()) is None:
        raise NotFoundError("this invite is invalid or has expired", error_code="invite_invalid")
    await repository.add_member(workspace["_id"], user["_id"], invite["role"])
    return to_workspace(workspace, {"role": invite["role"]})
