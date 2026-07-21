import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

ACCESS_TOKEN_TTL = timedelta(minutes=15)
JWT_ALGORITHM = "HS256"


class TokenError(Exception):
    """Raised when a JWT is invalid or expired."""


def issue_access_token(user_id: str, *, ttl: timedelta = ACCESS_TOKEN_TTL) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "iat": now, "exp": now + ttl, "type": "access"}
    return jwt.encode(payload, get_settings().jwt_secret_key, algorithm=JWT_ALGORITHM)


def verify_access_token(token: str) -> str:
    """Return the user id from a valid access JWT, raise TokenError otherwise."""
    try:
        payload = jwt.decode(token, get_settings().jwt_secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("type") != "access" or "sub" not in payload:
        raise TokenError("wrong token type")
    return payload["sub"]


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    """Refresh tokens are stored only as SHA-256 hashes; the raw value never touches Mongo."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY is not configured")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenError("cannot decrypt stored secret") from exc
