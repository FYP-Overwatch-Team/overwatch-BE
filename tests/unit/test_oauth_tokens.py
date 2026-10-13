from datetime import timedelta

import pytest

from app.core import security
from app.core.exceptions import UnauthorizedError
from app.core.time import utcnow
from app.db import mongo, oauth_tokens

TOKENS = {"access_token": "ghu_1", "expires_in": 28800, "refresh_token": "ghr_1", "refresh_token_expires_in": 100}


async def _expire_soon():
    await mongo.oauth_tokens().update_one({}, {"$set": {"expires_at": utcnow() + timedelta(seconds=30)}})


def _refresher(response=None, error=None):
    calls = []

    async def refresh(raw):
        calls.append(raw)
        if error:
            raise error
        return response

    refresh.calls = calls
    return refresh


async def test_fresh_token_returned_without_refresh():
    await oauth_tokens.save("u1", "github", TOKENS)
    refresh = _refresher()
    assert await oauth_tokens.get_fresh_access_token("u1", "github", refresh) == "ghu_1"
    assert refresh.calls == []


async def test_near_expiry_refreshes_and_stores_rotated_tokens():
    await oauth_tokens.save("u1", "github", TOKENS)
    await _expire_soon()
    refresh = _refresher({**TOKENS, "access_token": "ghu_2", "refresh_token": "ghr_2"})

    assert await oauth_tokens.get_fresh_access_token("u1", "github", refresh) == "ghu_2"
    assert refresh.calls == ["ghr_1"]
    doc = await oauth_tokens.get("u1", "github")
    assert security.decrypt_secret(doc["refresh_token_encrypted"]) == "ghr_2"


async def test_rejected_refresh_marks_needs_reauth():
    await oauth_tokens.save("u1", "github", TOKENS)
    await _expire_soon()
    refresh = _refresher(error=UnauthorizedError("nope", error_code="github_needs_reauth"))

    with pytest.raises(UnauthorizedError):
        await oauth_tokens.get_fresh_access_token("u1", "github", refresh)
    assert (await oauth_tokens.get("u1", "github"))["status"] == "needs_reauth"
    with pytest.raises(UnauthorizedError) as exc:
        await oauth_tokens.get_fresh_access_token("u1", "github", refresh)
    assert exc.value.error_code == "github_needs_reauth"


async def test_rejected_refresh_uses_a_concurrent_rotation():
    await oauth_tokens.save("u1", "github", TOKENS)
    await _expire_soon()

    async def refresh_lost_the_race(raw):
        # another request rotated the token while this one was in flight
        await oauth_tokens.save("u1", "github", {**TOKENS, "access_token": "ghu_new", "refresh_token": "ghr_new"})
        raise UnauthorizedError("refresh token already used")

    assert await oauth_tokens.get_fresh_access_token("u1", "github", refresh_lost_the_race) == "ghu_new"
    assert (await oauth_tokens.get("u1", "github"))["status"] == "active"


async def test_token_without_expiry_never_refreshes():
    await oauth_tokens.save("u1", "jira", {"access_token": "plain"})
    assert await oauth_tokens.get_fresh_access_token("u1", "jira", _refresher()) == "plain"


async def test_unknown_connection_needs_reauth():
    with pytest.raises(UnauthorizedError) as exc:
        await oauth_tokens.get_fresh_access_token("nobody", "github", _refresher())
    assert exc.value.error_code == "github_needs_reauth"
