"""Merging two accounts one person owns, once both are proven: proposing, previewing, confirming.

The signed-in account always survives and the proven one is absorbed, so a stolen session can only pull
accounts its holder already controls into it, never pull someone else's data out. Nothing is ever merged
because two emails look alike.
"""

import uuid
from datetime import timedelta

from fastapi import BackgroundTasks

from app.accounts import merge as engine
from app.accounts import repository
from app.auth import repository as auth_repository
from app.core.constants import AuthErrorCode, MergeProof, MergeStatus
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.rate_limit import Limit, take
from app.core.time import utcnow
from app.db import mongo
from app.mail import templates
from app.mail.sender import deliver

PROPOSAL_TTL = timedelta(minutes=15)
MERGES_PER_USER = Limit("merge:user", 5, timedelta(hours=1))
MERGING_MESSAGE = "This account is being merged with another. Try again in a minute."
CONFLICT_MESSAGE = "These accounts sign in differently in a way one account can't hold. Remove a sign-in method from one first."


def merging_error() -> AppError:
    """For signing in to an account a merge holds: it's about to change, or to be gone."""
    return AppError(MERGING_MESSAGE, error_code=AuthErrorCode.ACCOUNT_MERGING, status_code=409)


def conflicts(survivor: dict, absorbed: dict) -> list[str]:
    """Sign-in methods both accounts have, but differently: an account holds one GitHub and one email."""
    found = []
    if survivor.get("github_id") and absorbed.get("github_id") and survivor["github_id"] != absorbed["github_id"]:
        found.append("github")
    if survivor.get("email_verified_at") and absorbed.get("email_verified_at") and survivor.get("email") != absorbed.get("email"):
        found.append("email")
    return found


def _profile(user: dict) -> dict:
    return {
        "username": user.get("username"), "name": user.get("name"), "avatar_url": user.get("avatar_url"),
        "email": user.get("email") if user.get("email_verified_at") else None, "github_login": user.get("github_login"),
        "created_at": user.get("created_at"),
    }


async def _moves(absorbed_id: str) -> dict[str, int]:
    return {
        "workspaces": await mongo.workspace_members().count_documents({"user_id": absorbed_id}),
        "repos": await mongo.repos().count_documents({"connected_by": absorbed_id}),
        "documents": await mongo.documents().count_documents({"created_by": absorbed_id}),
        "work_items": await mongo.work_items().count_documents({"created_by": absorbed_id}),
        "comments": await mongo.work_item_comments().count_documents({"author.id": absorbed_id}),
        "chats": await mongo.chat_conversations().count_documents({"user_id": absorbed_id}),
    }


async def _preview(survivor: dict, absorbed: dict, proof: str) -> dict:
    return {
        "current": _profile(survivor), "other": _profile(absorbed), "proof": proof, "moves": await _moves(absorbed["_id"]),
        "personal_workspace_name": await engine.absorbed_workspace_name(absorbed), "conflicts": conflicts(survivor, absorbed),
    }


async def propose(survivor: dict, absorbed: dict, proof: str, identity: str | int) -> dict:
    """Remember that the signed-in account proved the other one (`identity`: its email or GitHub id), and preview the merge."""
    now = utcnow()
    await repository.save_proposal({
        "_id": uuid.uuid4().hex, "survivor_id": survivor["_id"], "absorbed_id": absorbed["_id"], "proof": proof,
        "identity": identity, "created_at": now, "expires_at": now + PROPOSAL_TTL,
    })
    return await _preview(survivor, absorbed, proof)


def _still_proven(absorbed: dict, proposal: dict) -> bool:
    if proposal["proof"] == MergeProof.GITHUB_OAUTH:
        return absorbed.get("github_id") == proposal["identity"]
    return absorbed.get("email") == proposal["identity"] and bool(absorbed.get("email_verified_at"))


async def _pending(user: dict) -> tuple[dict, dict]:
    proposal = await repository.find_proposal(user["_id"])
    absorbed = proposal and await auth_repository.get_user(proposal["absorbed_id"])
    if not proposal or not absorbed or not _still_proven(absorbed, proposal):
        raise NotFoundError("there's no merge to confirm; prove the other account again", error_code=AuthErrorCode.MERGE_NOT_FOUND)
    return proposal, absorbed


async def pending_preview(user: dict) -> dict:
    proposal, absorbed = await _pending(user)
    return await _preview(user, absorbed, proposal["proof"])


async def cancel(user: dict) -> None:
    await repository.delete_proposals(user["_id"])


async def confirm(background: BackgroundTasks, user: dict, keep_profile: str, ip: str) -> dict:
    """Merge the proven account into the signed-in one; returns the merged user."""
    await take(MERGES_PER_USER, user["_id"])
    proposal, absorbed = await _pending(user)
    if found := conflicts(user, absorbed):
        raise ConflictError(CONFLICT_MESSAGE, error_code=AuthErrorCode.MERGE_CONFLICT, meta={"conflicts": found})

    merge_id = uuid.uuid4().hex
    # both accounts, in a fixed order so two merges can't each hold one and wait for the other
    locked: list[str] = []
    for user_id in sorted((user["_id"], absorbed["_id"])):
        if not await repository.lock_user(user_id, merge_id):
            for held in locked:
                await repository.unlock_user(held, merge_id)
            raise AppError("Another merge of this account is in progress.", error_code=AuthErrorCode.MERGE_IN_PROGRESS, status_code=409)
        locked.append(user_id)

    survivor = await auth_repository.get_user(user["_id"]) or user
    absorbed = await auth_repository.get_user(absorbed["_id"]) or absorbed
    now = utcnow()
    record = {
        "_id": merge_id, "status": MergeStatus.RUNNING, "survivor_id": survivor["_id"], "absorbed_id": absorbed["_id"],
        "proof": proposal["proof"], "keep_profile": keep_profile, "ip": ip,
        "survivor_snapshot": survivor, "absorbed_snapshot": absorbed,
        "absorbed_workspace_name": await engine.absorbed_workspace_name(absorbed), "created_at": now, "updated_at": now,
    }
    # the audit record first: if anything below stops, it's what the merge resumes from
    await repository.insert_merge(record)
    await repository.delete_proposals(survivor["_id"])
    await engine.run(record)

    merged = await auth_repository.get_user(survivor["_id"]) or survivor
    for address in {merged.get("email"), absorbed.get("email")} - {None}:
        background.add_task(deliver, templates.accounts_merged_email(address, merged.get("username")))
    return merged
