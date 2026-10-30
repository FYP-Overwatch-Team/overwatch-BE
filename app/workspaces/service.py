"""Workspace rules: who may do what, and the invariants that keep a workspace usable.

- a workspace always keeps at least one owner
- a personal workspace can't be deleted, and its creator can't leave it
- nobody grants or removes a role above their own
"""

import secrets
import uuid
from datetime import timedelta

from fastapi import BackgroundTasks
from pymongo.errors import DuplicateKeyError

from app.auth import usernames
from app.auth.repository import find_user_by_email, get_user
from app.core import security
from app.core.config import get_settings
from app.core.constants import InviteKind, Role
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.rate_limit import Limit, cooldown_error, retry_after, take
from app.core.time import utcnow
from app.mail import templates
from app.mail.sender import deliver
from app.realtime.hub import get_hub
from app.workspaces import repository

INVITE_TTL = timedelta(days=7)
INVITE_RESEND_COOLDOWN = timedelta(seconds=60)
DAY = timedelta(days=1)
RESENDS_PER_INVITE = Limit("invite_resend", 5, DAY)
RESEND_WAIT = "Wait a moment before resending."
FORMER_MEMBER = "a former member"


def rank(role: str) -> int:
    return list(Role).index(role)


def to_workspace(workspace: dict, membership: dict) -> dict:
    return {"id": workspace["_id"], "name": workspace["name"], "personal": workspace["personal"], "role": membership["role"]}


async def require_member(workspace_id: str, user_id: str, min_role: str = Role.MEMBER) -> dict:
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
    return to_workspace(workspace, {"role": Role.OWNER})


async def rename(member: dict, name: str) -> dict:
    await repository.rename(member["workspace_id"], name)
    return to_workspace({**member["workspace"], "name": name}, member)


async def delete(member: dict) -> None:
    """Soft delete: hidden from everyone, but repos, tickets and graphs are kept."""
    if member["workspace"]["personal"]:
        raise ConflictError("a personal workspace can't be deleted", error_code="personal_workspace")
    await repository.soft_delete(member["workspace_id"])
    get_hub().revoke(member["workspace_id"])


async def list_members(workspace_id: str) -> list[dict]:
    members = []
    for membership in await repository.list_members(workspace_id):
        user = await get_user(membership["user_id"])
        if user:
            members.append({
                "user_id": user["_id"], "username": user.get("username"), "github_login": user.get("github_login"),
                "email": user.get("email"), "name": user.get("name"), "avatar_url": user.get("avatar_url"),
                "role": membership["role"], "joined_at": membership["joined_at"],
            })
    return members


async def _target(workspace_id: str, user_id: str) -> dict:
    target = await repository.membership(workspace_id, user_id)
    if target is None:
        raise NotFoundError("member not found", error_code="member_not_found")
    return target


async def _keep_an_owner(workspace_id: str, target: dict) -> None:
    if target["role"] == Role.OWNER and await repository.count_owners(workspace_id) <= 1:
        raise ConflictError("a workspace needs at least one owner", error_code="last_owner")


async def set_role(actor: dict, user_id: str, role: str) -> None:
    workspace_id = actor["workspace_id"]
    target = await _target(workspace_id, user_id)
    if max(rank(role), rank(target["role"])) > rank(actor["role"]):
        raise ForbiddenError("can't change a role above your own", error_code="insufficient_role")
    if role != Role.OWNER:
        await _keep_an_owner(workspace_id, target)
    await repository.set_role(workspace_id, user_id, role)


async def remove_member(actor: dict, user_id: str) -> None:
    """Admins remove others; anyone may leave."""
    workspace_id = actor["workspace_id"]
    target = await _target(workspace_id, user_id)
    leaving = user_id == actor["user_id"]
    if not leaving and (rank(actor["role"]) < rank(Role.ADMIN) or rank(target["role"]) > rank(actor["role"])):
        raise ForbiddenError("can't remove this member", error_code="insufficient_role")
    if actor["workspace"]["personal"] and user_id == actor["workspace"]["created_by"]:
        raise ConflictError("you can't leave your personal workspace", error_code="personal_workspace")
    await _keep_an_owner(workspace_id, target)
    await repository.remove_member(workspace_id, user_id)
    # their open sockets stop hearing about this workspace now, not at their next re-auth
    get_hub().revoke(workspace_id, user_id)


def _kind(doc: dict) -> str:
    # invites from before email invites carry no kind
    return doc.get("kind") or (InviteKind.GITHUB if doc.get("github_login") else InviteKind.LINK)


def _invite(doc: dict) -> dict:
    return {key: doc.get(key) for key in ("role", "github_login", "email", "created_at", "expires_at", "last_sent_at")} | {
        "id": doc["_id"], "kind": _kind(doc),
    }


def invite_url(token: str) -> str:
    # the token rides in the fragment, which browsers never send to a server or in a Referer
    return f"{get_settings().frontend_origin}/invite#{token}"


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


async def _count_invite_email(actor: dict) -> None:
    settings = get_settings()
    await take(Limit("invite_email:workspace", settings.invite_email_daily_limit_workspace, DAY), actor["workspace_id"])
    await take(Limit("invite_email:user", settings.invite_email_daily_limit_user, DAY), actor["user_id"])


async def _send_invite(background: BackgroundTasks, actor: dict, invite: dict, token: str) -> None:
    inviter = await get_user(actor["user_id"])
    email = templates.workspace_invite_email(
        invite["email"], usernames.handle(inviter, FORMER_MEMBER), actor["workspace"]["name"], invite["role"], invite_url(token), invite["expires_at"],
    )
    background.add_task(deliver, email)


async def create_invite(
    actor: dict, role: str, github_login: str | None, email: str | None, background: BackgroundTasks,
) -> dict:
    if rank(role) > rank(actor["role"]):
        raise ForbiddenError("can't invite above your own role", error_code="insufficient_role")
    workspace_id = actor["workspace_id"]
    if email:
        invitee = await find_user_by_email(email)
        if invitee and await repository.membership(workspace_id, invitee["_id"]):
            raise ConflictError("this person is already a member", error_code="already_member")
        await repository.release_expired_email_invites(workspace_id)
    token = secrets.token_urlsafe(32)
    now = utcnow()
    kind = InviteKind.EMAIL if email else InviteKind.GITHUB if github_login else InviteKind.LINK
    doc = {
        "_id": uuid.uuid4().hex,
        "workspace_id": workspace_id,
        "token_hash": security.hash_refresh_token(token),
        "kind": kind,
        "role": role,
        "github_login": github_login,
        "email": email,
        "invited_by": actor["user_id"],
        "created_at": now,
        "expires_at": now + INVITE_TTL,
        "accepted_by": None,
        "revoked": False,
    }
    if email:
        doc |= {"pending_email": email, "last_sent_at": now, "send_count": 1}
        await _count_invite_email(actor)
    try:
        await repository.insert_invite(doc)
    except DuplicateKeyError as exc:
        raise ConflictError("this email already has a pending invite; resend it instead", error_code="invite_exists") from exc
    if email:
        await _send_invite(background, actor, doc, token)
    # an email invite's link only ever goes to that inbox
    return {**_invite(doc), "url": None if email else invite_url(token)}


async def resend_invite(actor: dict, invite_id: str, background: BackgroundTasks) -> dict:
    invite = await repository.find_open_email_invite(actor["workspace_id"], invite_id)
    if invite is None:
        raise NotFoundError("invite not found", error_code="invite_not_found")
    if wait := retry_after(invite["last_sent_at"], INVITE_RESEND_COOLDOWN):
        raise cooldown_error(RESEND_WAIT, "invite_resend_cooldown", wait)
    await take(RESENDS_PER_INVITE, invite_id)
    await _count_invite_email(actor)
    token = secrets.token_urlsafe(32)
    now = utcnow()
    # a new link: the one in the earlier email stops working
    fields = {"token_hash": security.hash_refresh_token(token), "last_sent_at": now, "expires_at": now + INVITE_TTL}
    renewed = await repository.renew_invite(invite_id, invite["last_sent_at"], fields)
    if renewed is None:
        raise cooldown_error(RESEND_WAIT, "invite_resend_cooldown", int(INVITE_RESEND_COOLDOWN.total_seconds()))
    await _send_invite(background, actor, renewed, token)
    return _invite(renewed)


async def list_invites(workspace_id: str) -> list[dict]:
    return [_invite(doc) for doc in await repository.list_invites(workspace_id)]


async def revoke_invite(workspace_id: str, invite_id: str) -> None:
    if not await repository.revoke_invite(workspace_id, invite_id):
        raise NotFoundError("invite not found", error_code="invite_not_found")


def _invalid_invite() -> NotFoundError:
    return NotFoundError("this invite is invalid or has expired", error_code="invite_invalid")


async def _find_open(token: str) -> tuple[dict, dict]:
    invite = await repository.find_open_invite(security.hash_refresh_token(token))
    workspace = invite and await repository.get(invite["workspace_id"])
    if not workspace:
        raise _invalid_invite()
    return invite, workspace


async def open_email_invite(token: str) -> tuple[dict, dict]:
    invite, workspace = await _find_open(token)
    if _kind(invite) != InviteKind.EMAIL:
        raise _invalid_invite()
    return invite, workspace


async def _open_invite(token: str, user: dict) -> tuple[dict, dict]:
    invite, workspace = await _find_open(token)
    kind = _kind(invite)
    if kind == InviteKind.GITHUB and invite["github_login"].lower() != (user.get("github_login") or "").lower():
        raise ForbiddenError("this invite is for another GitHub account", error_code="invite_wrong_account")
    if kind == InviteKind.EMAIL and not (user.get("email") == invite["email"] and user.get("email_verified_at")):
        invited = mask_email(invite["email"])
        raise ForbiddenError(f"this invite is for {invited}", error_code="invite_wrong_account", meta={"invited": invited})
    return invite, workspace


async def _preview(invite: dict, workspace: dict) -> dict:
    inviter = await get_user(invite["invited_by"])
    return {"workspace_name": workspace["name"], "invited_by": usernames.handle(inviter, FORMER_MEMBER), "role": invite["role"]}


async def public_preview(token: str) -> dict:
    """What a signed-out visitor holding the link may see: holding it shows they can read that inbox."""
    invite, workspace = await _find_open(token)
    kind = _kind(invite)
    preview = await _preview(invite, workspace) | {"kind": kind, "email": None, "account_exists": None}
    if kind == InviteKind.EMAIL:
        invitee = await find_user_by_email(invite["email"])
        preview |= {"email": invite["email"], "account_exists": bool(invitee and invitee.get("email_verified_at"))}
    return preview


async def preview_invite(token: str, user: dict) -> dict:
    invite, workspace = await _open_invite(token, user)
    return await _preview(invite, workspace)


async def accept_invite(token: str, user: dict) -> dict:
    invite, workspace = await _open_invite(token, user)
    # an existing member keeps their role and leaves the link unused
    if existing := await repository.membership(workspace["_id"], user["_id"]):
        return to_workspace(workspace, existing)
    if await repository.claim_invite(invite["token_hash"], user["_id"], utcnow()) is None:
        raise NotFoundError("this invite is invalid or has expired", error_code="invite_invalid")
    await repository.add_member(workspace["_id"], user["_id"], invite["role"])
    return to_workspace(workspace, {"role": invite["role"]})
