from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response

from app.core import security
from app.core.exceptions import UnauthorizedError
from app.db import mongo
from app.services import jira_service

TOKEN_URL = "https://auth.atlassian.com/oauth/token"


async def seed_tokens(expires_in_seconds: int) -> None:
    await jira_service.store_tokens(
        "user-1",
        {"access_token": "old-access", "refresh_token": "old-refresh", "expires_in": expires_in_seconds},
        cloud_id="cloud-1",
    )


async def test_fresh_token_used_without_refresh():
    await seed_tokens(expires_in_seconds=3600)
    with respx.mock:  # strict: any outbound call would fail the test
        access, cloud_id = await jira_service.get_access_token("user-1")
    assert access == "old-access"
    assert cloud_id == "cloud-1"


async def test_token_inside_buffer_refreshes_proactively():
    await seed_tokens(expires_in_seconds=60)  # < 2-min buffer, still technically valid
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(200, json={
            "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
        }))
        access, _ = await jira_service.get_access_token("user-1")
    assert access == "new-access"


async def test_rotated_refresh_token_overwrites_stored_one():
    await seed_tokens(expires_in_seconds=0)
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(200, json={
            "access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 3600,
        }))
        await jira_service.get_access_token("user-1")

    doc = await mongo.oauth_tokens().find_one({"user_id": "user-1", "provider": "jira"})
    assert security.decrypt_secret(doc["refresh_token_encrypted"]) == "rotated-refresh"
    assert security.decrypt_secret(doc["access_token_encrypted"]) == "new-access"


async def test_rejected_refresh_marks_needs_reauth():
    await seed_tokens(expires_in_seconds=0)
    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(403, json={"error": "invalid_grant"}))
        with pytest.raises(UnauthorizedError) as exc:
            await jira_service.get_access_token("user-1")
    assert exc.value.error_code == "jira_needs_reauth"

    doc = await mongo.oauth_tokens().find_one({"user_id": "user-1", "provider": "jira"})
    assert doc["status"] == "needs_reauth"

    # subsequent calls fail fast without hitting Atlassian
    with respx.mock:
        with pytest.raises(UnauthorizedError):
            await jira_service.get_access_token("user-1")


async def test_no_connection_raises_needs_reauth():
    with pytest.raises(UnauthorizedError) as exc:
        await jira_service.get_access_token("user-1")
    assert exc.value.error_code == "jira_needs_reauth"


async def test_tokens_stored_encrypted():
    await seed_tokens(expires_in_seconds=3600)
    doc = await mongo.oauth_tokens().find_one({"user_id": "user-1", "provider": "jira"})
    assert "old-access" not in str(doc)
    assert "old-refresh" not in str(doc)
