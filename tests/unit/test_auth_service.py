import pytest

from app.auth import repository, service
from app.core import security
from app.core.exceptions import UnauthorizedError
from app.db import mongo

PROFILE = {"id": 42, "login": "octocat", "name": "Octo Cat", "avatar_url": "https://img"}


async def _sign_in(profile=PROFILE, token="ghu_token") -> dict:
    await service.sign_in_with_github(profile, {"access_token": token, "expires_in": 28800, "refresh_token": "ghr_token"})
    return await mongo.users().find_one({"github_id": profile["id"]})


async def test_sign_in_creates_then_updates_user():
    user = await _sign_in()
    assert user["github_login"] == "octocat"

    user2 = await _sign_in({**PROFILE, "name": "New Name"})
    assert user2["_id"] == user["_id"]
    assert user2["name"] == "New Name"
    assert user2["created_at"] == user["created_at"]
    assert await mongo.users().count_documents({}) == 1


async def test_github_token_stored_encrypted():
    user = await _sign_in(token="ghu_supersecret")
    doc = await mongo.oauth_tokens().find_one({"user_id": user["_id"], "provider": "github"})
    assert "ghu_supersecret" not in str(doc) and "ghr_token" not in str(doc)
    assert security.decrypt_secret(doc["access_token_encrypted"]) == "ghu_supersecret"
    assert doc["expires_at"] is not None


async def test_session_and_rotation():
    access, raw1 = await service.create_session("user-1")
    assert security.verify_access_token(access) == "user-1"

    access2, raw2 = await service.rotate(raw1)
    assert security.verify_access_token(access2) == "user-1"
    assert raw2 != raw1

    # raw refresh tokens never stored in plaintext
    docs = [d async for d in mongo.refresh_tokens().find({})]
    assert all(raw1 != d["token_hash"] and raw2 != d["token_hash"] for d in docs)


async def test_a_token_can_be_claimed_only_once():
    # the single conditional write is what makes rotation atomic; the race
    # itself is exercised against real Mongo in test_auth_sessions_mongo.py
    _, raw = await service.create_session("user-1")
    token_hash = security.hash_refresh_token(raw)
    assert await repository.claim_refresh_token(token_hash, "next-1") is not None
    assert await repository.claim_refresh_token(token_hash, "next-2") is None


async def test_reuse_of_rotated_token_revokes_family():
    _, raw1 = await service.create_session("user-1")
    _, raw2 = await service.rotate(raw1)

    with pytest.raises(UnauthorizedError) as exc:
        await service.rotate(raw1)
    assert exc.value.error_code == "refresh_token_reused"

    # the newest token in the family must now be dead too
    with pytest.raises(UnauthorizedError):
        await service.rotate(raw2)


async def test_unknown_refresh_token_rejected():
    with pytest.raises(UnauthorizedError) as exc:
        await service.rotate("no-such-token")
    assert exc.value.error_code == "invalid_refresh_token"


async def test_logout_revokes_family():
    _, raw = await service.create_session("user-1")
    await service.logout(raw)
    with pytest.raises(UnauthorizedError):
        await service.rotate(raw)


async def test_logout_everywhere_revokes_every_family():
    _, laptop = await service.create_session("user-1")
    _, phone = await service.create_session("user-1")
    _, other_user = await service.create_session("user-2")

    await service.logout_everywhere("user-1")

    for raw in (laptop, phone):
        with pytest.raises(UnauthorizedError):
            await service.rotate(raw)
    await service.rotate(other_user)  # untouched
