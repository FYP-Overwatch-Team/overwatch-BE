"""Emailed one-time codes: six digits, short-lived, stored only as a keyed hash.

The hash is an HMAC under a server key, so a leaked database can't be brute-forced offline the
way a plain hash of a 6-digit code could.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from app.core.config import get_settings

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
# how long the challenge itself (its resends) lives
CHALLENGE_TTL = timedelta(hours=1)
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_RESENDS = 5


def new_code() -> str:
    return str(secrets.randbelow(10**CODE_LENGTH)).zfill(CODE_LENGTH)


def hash_code(code: str, purpose: str) -> str:
    key = hashlib.sha256(f"otp:{get_settings().jwt_secret_key}".encode()).digest()
    return hmac.new(key, f"{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def unmatchable_hash(purpose: str) -> str:
    """A decoy challenge's hash: no six-digit code ever matches it."""
    return hash_code(new_code() + "x", purpose)


def matches(code: str, purpose: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_code(code, purpose), code_hash)


def email_key(email: str) -> str:
    """An email as it appears in keys and lookups that shouldn't hold the address itself."""
    return hashlib.sha256(email.encode()).hexdigest()
