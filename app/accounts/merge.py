"""Moving everything one account owns onto another, then removing it.

There are no multi-document transactions (dev and tests run without a replica set), so a merge is a
series of steps that are each safe to run again: the audit record is written first, and a merge that
stops halfway is simply run again from the top until it finishes.

Every field that holds a user id is listed here. A new collection that stores user ids must be added,
or a merge leaves its rows pointing at a deleted account.
"""

from collections.abc import Callable
from datetime import timedelta

import structlog

from app.auth import repository as auth_repository
from app.auth import usernames
from app.accounts import repository
from app.core.constants import KeepProfile, Provider
from app.core.time import utcnow
from app.db import mongo
from app.workspaces.service import rank

logger = structlog.get_logger("app.accounts")

# a merge still "running" this long after its last step was interrupted, and is resumed
STALL_AFTER = timedelta(minutes=5)

# (collection, field) pairs holding a plain user id
USER_FIELDS: list[tuple[Callable, str]] = [
    (mongo.workspaces, "created_by"),
    (mongo.workspace_invites, "invited_by"),
    (mongo.workspace_invites, "accepted_by"),
    (mongo.repos, "connected_by"),
    (mongo.connections, "connected_by"),
    (mongo.work_items, "created_by"),
    (mongo.documents, "created_by"),
    (mongo.documents, "updated_by"),
    (mongo.documents, "archived_by"),
    (mongo.doc_attachments, "created_by"),
    (mongo.chat_conversations, "user_id"),
]
# (collection, embedded person) pairs: `{id, name}` copies whose name follows the merged profile
PEOPLE: list[tuple[Callable, str]] = [
    (mongo.work_items, "assignee"),
    (mongo.work_items, "reporter"),
    (mongo.work_item_comments, "author"),
    (mongo.documents, "author"),
]
# identity fields that move from the absorbed account when the survivor lacks them
LOGINS = ("github_id", "github_login")
EMAIL_LOGIN = ("email", "email_verified_at", "password_hash", "password_changed_at")
PROFILE = ("username", "name", "avatar_url")


def merged_profile(survivor: dict, absorbed: dict, keep: str) -> dict:
    """The survivor's fields after the merge: the chosen profile, and whichever sign-in methods either account had."""
    chosen, other = (absorbed, survivor) if keep == KeepProfile.OTHER else (survivor, absorbed)
    fields = {field: chosen.get(field) or other.get(field) for field in PROFILE}
    source = survivor if survivor.get("github_id") else absorbed
    fields |= {field: source.get(field) for field in LOGINS}
    source = survivor if survivor.get("email") else absorbed
    fields |= {field: source.get(field) for field in EMAIL_LOGIN}
    return fields


async def _move_memberships(survivor_id: str, absorbed_id: str) -> int:
    """Join the survivor to every workspace the absorbed account was in, keeping the higher role of the two."""
    moved = 0
    async for membership in mongo.workspace_members().find({"user_id": absorbed_id}):
        mine = await mongo.workspace_members().find_one({"workspace_id": membership["workspace_id"], "user_id": survivor_id})
        if mine is None:
            await mongo.workspace_members().update_one({"_id": membership["_id"]}, {"$set": {"user_id": survivor_id}})
        else:
            role = max(mine["role"], membership["role"], key=rank)
            joined = min(mine["joined_at"], membership["joined_at"])
            await mongo.workspace_members().update_one({"_id": mine["_id"]}, {"$set": {"role": role, "joined_at": joined}})
            await mongo.workspace_members().delete_one({"_id": membership["_id"]})
        moved += 1
    return moved


async def absorbed_workspace_name(absorbed: dict) -> str | None:
    """What the absorbed account's personal workspace is called once it's an ordinary workspace; None without one."""
    workspace = await mongo.workspaces().find_one({"_id": absorbed["_id"], "personal": True})
    if workspace is None:
        return None
    handle = usernames.handle(absorbed)
    return f"{workspace['name']} (from @{handle})" if handle else workspace["name"]


async def run(merge: dict) -> dict[str, int]:
    """Carry out (or finish) one merge; every step can run again without harm."""
    survivor_id, absorbed_id = merge["survivor_id"], merge["absorbed_id"]
    survivor, absorbed = merge["survivor_snapshot"], merge["absorbed_snapshot"]
    moved: dict[str, int] = {}

    # the absorbed account can't sign in, refresh or verify anything from here on
    await auth_repository.revoke_user_sessions(absorbed_id)
    await auth_repository.delete_challenges(absorbed_id)
    await repository.delete_proposals(absorbed_id)

    moved["memberships"] = await _move_memberships(survivor_id, absorbed_id)
    # the survivor keeps their own personal workspace; the other becomes an ordinary one, contents untouched
    if merge.get("absorbed_workspace_name"):
        await mongo.workspaces().update_one(
            {"_id": absorbed_id, "personal": True},
            {"$set": {"personal": False, "merged_from": absorbed_id, "name": merge["absorbed_workspace_name"], "updated_at": utcnow()}},
        )

    profile = merged_profile(survivor, absorbed, merge["keep_profile"])
    name = usernames.display_name(profile)
    for collection, field in USER_FIELDS:
        result = await collection().update_many({field: absorbed_id}, {"$set": {field: survivor_id}})
        moved[f"{collection.__name__}.{field}"] = result.modified_count
    for collection, person in PEOPLE:
        changes = {f"{person}.id": survivor_id, f"{person}.name": name}
        if person == "author" and collection is mongo.documents:
            changes[f"{person}.avatar_url"] = profile.get("avatar_url")
        result = await collection().update_many({f"{person}.id": absorbed_id}, {"$set": changes})
        moved[f"{collection.__name__}.{person}"] = result.modified_count

    # GitHub is the one connection held by the user (others belong to workspaces, which keep their ids)
    if await mongo.oauth_tokens().find_one({"owner_id": survivor_id, "provider": Provider.GITHUB}):
        await mongo.oauth_tokens().delete_many({"owner_id": absorbed_id, "provider": Provider.GITHUB})
    else:
        await mongo.oauth_tokens().update_many(
            {"owner_id": absorbed_id, "provider": Provider.GITHUB}, {"$set": {"owner_id": survivor_id}},
        )

    # sign-in methods and username are unique: free them on the absorbed account before the survivor takes them
    await mongo.users().update_one({"_id": absorbed_id}, {"$unset": dict.fromkeys(("github_id", "email", "username"), "")})
    present = {field: value for field, value in profile.items() if value is not None}
    missing = [field for field, value in profile.items() if value is None]
    await auth_repository.update_user(survivor_id, present, unset=tuple(missing))
    await mongo.users().delete_one({"_id": absorbed_id})
    # a refresh that raced the first revocation is ended too
    await auth_repository.revoke_user_sessions(absorbed_id)

    await repository.finish_merge(merge["_id"], moved)
    await repository.unlock_user(survivor_id, merge["_id"])
    logger.info("account_merged", merge_id=merge["_id"], survivor_id=survivor_id, absorbed_id=absorbed_id)
    return moved


async def resume_stalled() -> None:
    """Finish merges a crash or restart interrupted."""
    for merge in await repository.stalled_merges(utcnow() - STALL_AFTER):
        try:
            await run(merge)
        except Exception:
            logger.exception("account_merge_resume_failed", merge_id=merge["_id"])
