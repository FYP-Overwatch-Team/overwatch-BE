"""Password policy (NIST SP 800-63B) and storage (argon2id, OWASP parameters).

Length is the rule: 8 to 128 characters, anything allowed, nothing truncated, no composition rules.
New passwords are checked against Have I Been Pwned's breach corpus, sending only a hash prefix;
if HIBP can't be reached the password is allowed, so an outage never blocks sign-up.
"""

import asyncio
import hashlib
import unicodedata

import httpx
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings

logger = structlog.get_logger("app.auth")

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/"
HIBP_TIMEOUT = httpx.Timeout(2.0)

# OWASP's minimum argon2id configuration: 19 MiB, two passes, one lane
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
# compared against when no account matches, so an unknown email takes as long as a wrong password
DUMMY_HASH = _hasher.hash("overwatch-no-such-account")


def normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def problem(password: str, email: str | None) -> str | None:
    """Why a new password isn't acceptable, worded for the user; None when it is."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Use at most {PASSWORD_MAX_LENGTH} characters"
    local = (email or "").partition("@")[0].lower()
    if len(local) >= 3 and local in password.lower():
        return "Don't include your email address in your password"
    return None


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, normalize(password))


async def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, password_hash or DUMMY_HASH, normalize(password)) and bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


async def is_breached(password: str) -> bool:
    if not get_settings().hibp_enabled:
        return False
    digest = hashlib.sha1(normalize(password).encode()).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        async with httpx.AsyncClient(timeout=HIBP_TIMEOUT) as client:
            response = await client.get(HIBP_RANGE_URL + prefix, headers={"Add-Padding": "true"})
            response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("hibp_unavailable")
        return False
    for line in response.text.splitlines():
        candidate, _, count = line.partition(":")
        if candidate == suffix and count.strip() not in ("", "0"):
            return True
    return False
