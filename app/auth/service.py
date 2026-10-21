"""Sessions: a short-lived access JWT plus a rotating refresh token.

Refresh tokens are grouped into families (one per sign-in). Presenting a token
that was already rotated revokes the whole family, the standard defence
against a stolen refresh token.
"""

import uuid
from datetime import timedelta
from typing import NoReturn

import structlog
from pymongo.errors import DuplicateKeyError

from app.accounts import service as accounts
from app.auth import repository, usernames
from app.core import security
from app.core.constants import AuthErrorCode, MergeProof, Provider
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.time import utcnow
from app.db import oauth_tokens
from app.workspaces import repository as workspaces

logger = structlog.get_logger("app.auth")

REFRESH_TOKEN_TTL = timedelta(days=30)
# a token rotated this recently still works once, through its unused successor: a refresh whose answer was lost
# (a closed tab, a dropped connection) or two tabs refreshing together aren't mistaken for theft
REUSE_GRACE = timedelta(seconds=20)


async def sign_in_with_github(profile: dict, github_tokens: dict) -> str:
    """Upsert the user, keep their GitHub user token, start a session. Returns the raw refresh token."""
    existing = await repository.find_user_by_github_id(profile["id"])
    if existing and existing.get("merge_id"):
        raise accounts.merging_error()
    username = None
    if existing is None and (candidate := usernames.default_from(profile["login"])) and not await usernames.is_taken(candidate):
        username = candidate
    try:
        user = await repository.upsert_github_user(profile, username)
    except DuplicateKeyError:
        # the free username was taken in between; they choose one after signing in
        user = await repository.upsert_github_user(profile, None)
    await oauth_tokens.save(user["_id"], Provider.GITHUB, github_tokens)
    await workspaces.ensure_personal(user)
    return await create_session(user["_id"])


async def create_session(user_id: str, family_id: str | None = None) -> str:
    """Start a session family; the access token comes from the first /auth/refresh. Returns the raw refresh token."""
    raw_refresh = security.generate_refresh_token()
    await repository.insert_refresh_token(
        security.hash_refresh_token(raw_refresh),
        user_id,
        family_id or uuid.uuid4().hex,
        utcnow() + REFRESH_TOKEN_TTL,
    )
    return raw_refresh


async def rotate(raw_refresh: str) -> tuple[str, str]:
    """Swap a refresh token for a new one. Returns (access_jwt, new_raw_refresh)."""
    token_hash = security.hash_refresh_token(raw_refresh)
    new_raw = security.generate_refresh_token()
    new_hash = security.hash_refresh_token(new_raw)
    claimed = await repository.claim_refresh_token(token_hash, new_hash)
    if claimed is None:
        claimed = await _claim_successor(token_hash, new_hash)
    if claimed is None:
        await _reject(token_hash)

    await repository.insert_refresh_token(
        security.hash_refresh_token(new_raw),
        claimed["user_id"],
        claimed["family_id"],
        utcnow() + REFRESH_TOKEN_TTL,
    )
    return security.issue_access_token(claimed["user_id"]), new_raw


async def _claim_successor(token_hash: str, new_hash: str) -> dict | None:
    """Within the grace period, a just-rotated token takes over its successor if nothing has used that yet."""
    doc = await repository.find_refresh_token(token_hash)
    if not doc or not doc.get("rotated_at") or doc["rotated_at"] + REUSE_GRACE < utcnow():
        return None
    return await repository.claim_refresh_token(doc["replaced_by"], new_hash)


async def _reject(token_hash: str) -> NoReturn:
    """Explain why a token couldn't be claimed, revoking its family on reuse."""
    doc = await repository.find_refresh_token(token_hash)
    if doc is None:
        raise UnauthorizedError("invalid refresh token", error_code="invalid_refresh_token")
    if doc["revoked"]:
        await repository.revoke_family(doc["family_id"])
        logger.warning("refresh_token_reuse_detected", family_id=doc["family_id"], user_id=doc["user_id"])
        raise UnauthorizedError("refresh token reuse detected", error_code="refresh_token_reused")
    raise UnauthorizedError("refresh token expired", error_code="refresh_token_expired")


async def link_github(user_id: str, profile: dict, github_tokens: dict) -> bool:
    """Attach a GitHub account to a signed-in user. When it has an Overwatch account of its own, signing in to it just
    proved that account too, so a merge is proposed instead: returns True then. Raises when they can't be merged.
    """
    in_use = ConflictError("that GitHub account belongs to another user", error_code=AuthErrorCode.GITHUB_ACCOUNT_IN_USE)
    owner = await repository.find_user_by_github_id(profile["id"])
    if owner and owner["_id"] != user_id:
        user = await repository.get_user(user_id)
        if user is None or owner.get("merge_id") or accounts.conflicts(user, owner):
            raise in_use
        # the fresh token belongs to the GitHub identity, which is the other account's until the merge
        await oauth_tokens.save(owner["_id"], Provider.GITHUB, github_tokens)
        await accounts.propose(user, owner, MergeProof.GITHUB_OAUTH, profile["id"])
        return True
    try:
        await repository.link_github(user_id, profile)
    except DuplicateKeyError as exc:
        # another user linked it in between
        raise in_use from exc
    await oauth_tokens.save(user_id, Provider.GITHUB, github_tokens)
    return False


async def logout(raw_refresh: str) -> None:
    doc = await repository.find_refresh_token(security.hash_refresh_token(raw_refresh))
    if doc:
        await repository.revoke_family(doc["family_id"])


async def logout_everywhere(user_id: str) -> None:
    await repository.revoke_user_sessions(user_id)
