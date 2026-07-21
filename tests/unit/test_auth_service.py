import pytest

from app.core import security
from app.core.exceptions import UnauthorizedError
from app.db import mongo
from app.services import auth_service

PROFILE = {"id": 42, "login": "octocat", "name": "Octo Cat", "avatar_url": "https://img"}


async def test_upsert_creates_then_updates_user():
    user = await auth_service.upsert_user_from_github(PROFILE, "gho_token")
    assert user["github_login"] == "octocat"

    updated_profile = {**PROFILE, "name": "New Name"}
    user2 = await auth_service.upsert_user_from_github(updated_profile, "gho_token2")
    assert user2["_id"] == user["_id"]
    assert user2["name"] == "New Name"
    assert await mongo.users().count_documents({}) == 1


async def test_github_token_stored_encrypted():
    user = await auth_service.upsert_user_from_github(PROFILE, "gho_supersecret")
    doc = await mongo.oauth_tokens().find_one({"user_id": user["_id"], "provider": "github"})
    assert doc is not None
    assert "gho_supersecret" not in str(doc)
    assert security.decrypt_secret(doc["access_token_encrypted"]) == "gho_supersecret"


async def test_session_and_rotation():
    access, raw1 = await auth_service.create_session("user-1")
    assert security.verify_access_token(access) == "user-1"

    access2, raw2 = await auth_service.rotate_refresh_token(raw1)
    assert security.verify_access_token(access2) == "user-1"
    assert raw2 != raw1

    # raw refresh tokens never stored in plaintext
    docs = [d async for d in mongo.refresh_tokens().find({})]
    assert all(raw1 != d["token_hash"] and raw2 != d["token_hash"] for d in docs)


async def test_reuse_of_rotated_token_revokes_family():
    _, raw1 = await auth_service.create_session("user-1")
    _, raw2 = await auth_service.rotate_refresh_token(raw1)

    with pytest.raises(UnauthorizedError) as exc:
        await auth_service.rotate_refresh_token(raw1)
    assert exc.value.error_code == "refresh_token_reused"

    # the newest token in the family must now be dead too
    with pytest.raises(UnauthorizedError):
        await auth_service.rotate_refresh_token(raw2)


async def test_unknown_refresh_token_rejected():
    with pytest.raises(UnauthorizedError) as exc:
        await auth_service.rotate_refresh_token("no-such-token")
    assert exc.value.error_code == "invalid_refresh_token"


async def test_logout_revokes_family():
    _, raw = await auth_service.create_session("user-1")
    await auth_service.logout(raw)
    with pytest.raises(UnauthorizedError):
        await auth_service.rotate_refresh_token(raw)
