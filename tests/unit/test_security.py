from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from app.core import security
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _configured_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_access_token_round_trip():
    token = security.issue_access_token("user-123")
    assert security.verify_access_token(token) == "user-123"


def test_expired_access_token_rejected():
    token = security.issue_access_token("user-123", ttl=timedelta(seconds=-1))
    with pytest.raises(security.TokenError):
        security.verify_access_token(token)


def test_tampered_access_token_rejected():
    token = security.issue_access_token("user-123")
    with pytest.raises(security.TokenError):
        security.verify_access_token(token + "x")


def test_refresh_token_hashing_is_stable_and_opaque():
    raw = security.generate_refresh_token()
    hashed = security.hash_refresh_token(raw)
    assert hashed == security.hash_refresh_token(raw)
    assert raw not in hashed
    assert len(hashed) == 64  # sha256 hex


def test_fernet_round_trip():
    secret = "gho_verysecretgithubtoken"
    encrypted = security.encrypt_secret(secret)
    assert secret not in encrypted
    assert security.decrypt_secret(encrypted) == secret


def test_decrypt_with_bad_ciphertext_raises():
    with pytest.raises(security.TokenError):
        security.decrypt_secret("not-a-valid-token")


def test_missing_fernet_key_raises(monkeypatch):
    monkeypatch.setenv("FERNET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        security.encrypt_secret("x")
