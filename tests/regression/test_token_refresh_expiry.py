"""Regression pins for the auth-session edge cases most likely to break silently."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core import security
from app.db import mongo
from app.services import auth_service

pytestmark = pytest.mark.regression


async def test_expired_refresh_token_rejected_with_stable_error_code():
    _, raw = await auth_service.create_session("user-1")
    await mongo.refresh_tokens().update_one(
        {"token_hash": security.hash_refresh_token(raw)},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=60)}},
    )
    with pytest.raises(Exception) as exc:
        await auth_service.rotate_refresh_token(raw)
    assert getattr(exc.value, "error_code", None) == "refresh_token_expired"


async def test_token_still_valid_just_before_expiry():
    _, raw = await auth_service.create_session("user-1")
    await mongo.refresh_tokens().update_one(
        {"token_hash": security.hash_refresh_token(raw)},
        {"$set": {"expires_at": datetime.now(timezone.utc) + timedelta(minutes=1)}},
    )
    access, _ = await auth_service.rotate_refresh_token(raw)
    assert security.verify_access_token(access) == "user-1"


async def test_family_revocation_marks_every_generation():
    _, raw1 = await auth_service.create_session("user-1")
    _, raw2 = await auth_service.rotate_refresh_token(raw1)
    _, raw3 = await auth_service.rotate_refresh_token(raw2)

    with pytest.raises(Exception):
        await auth_service.rotate_refresh_token(raw1)  # reuse -> family revoked

    docs = [d async for d in mongo.refresh_tokens().find({})]
    assert len(docs) == 3
    assert all(d["revoked"] for d in docs)
