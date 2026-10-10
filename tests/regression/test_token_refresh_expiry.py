"""Regression pins for the auth-session edge cases most likely to break silently."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core import security
from app.db import mongo
from app.auth import service

pytestmark = pytest.mark.regression


async def test_expired_refresh_token_rejected_with_stable_error_code():
    raw = await service.create_session("user-1")
    await mongo.refresh_tokens().update_one(
        {"token_hash": security.hash_refresh_token(raw)},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=60)}},
    )
    with pytest.raises(Exception) as exc:
        await service.rotate(raw)
    assert getattr(exc.value, "error_code", None) == "refresh_token_expired"


async def test_token_still_valid_just_before_expiry():
    raw = await service.create_session("user-1")
    await mongo.refresh_tokens().update_one(
        {"token_hash": security.hash_refresh_token(raw)},
        {"$set": {"expires_at": datetime.now(timezone.utc) + timedelta(minutes=1)}},
    )
    access, _ = await service.rotate(raw)
    assert security.verify_access_token(access) == "user-1"


async def test_family_revocation_marks_every_generation():
    raw1 = await service.create_session("user-1")
    _, raw2 = await service.rotate(raw1)
    _, raw3 = await service.rotate(raw2)

    with pytest.raises(Exception):
        await service.rotate(raw1)  # reuse -> family revoked

    docs = [d async for d in mongo.refresh_tokens().find({})]
    assert len(docs) == 3
    assert all(d["revoked"] for d in docs)
