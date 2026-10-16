"""Usernames: public handles, unique ignoring case (they're stored lowercase).

Availability is answered from the unique index alone; the index is also the real check when one is
saved, so two people racing for the same name can't both get it.
"""

import re
from enum import StrEnum

from app.db import mongo

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 30
# letters and digits, single "-" or "_" between them
USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|[-_](?=[a-z0-9])){2,29}$")
RESERVED = frozenset({
    "about", "account", "accounts", "admin", "administrator", "api", "app", "auth", "bearings", "billing", "blog",
    "board", "boards", "chat", "choose-username", "confluence", "dashboard", "docs", "forgot-password", "github", "help",
    "invite", "invites", "jira", "login", "logout", "mail", "me", "noreply", "no-reply", "null", "onboarding",
    "overwatch", "owner", "postmaster", "privacy", "reset-password", "root", "security", "settings", "sign-in",
    "sign-up", "signin", "signup", "staff", "status", "support", "system", "team", "terms", "undefined",
    "user", "users", "verify-email", "webmaster", "workspace", "workspaces", "www",
})


class Reason(StrEnum):
    INVALID = "invalid"
    RESERVED = "reserved"
    TAKEN = "taken"


def normalize(username: str) -> str:
    return username.strip().lower()


def problem(username: str) -> str | None:
    """Why a username can't be used, before asking the database; None when it might."""
    if not USERNAME_RE.match(username):
        return Reason.INVALID
    if username in RESERVED:
        return Reason.RESERVED
    return None


MESSAGES = {
    Reason.INVALID: f"Use {USERNAME_MIN_LENGTH}–{USERNAME_MAX_LENGTH} lowercase letters, numbers, - or _",
    Reason.RESERVED: "That username isn't available",
    Reason.TAKEN: "That username is taken",
}


async def is_taken(username: str) -> bool:
    # a covered query: answered from the username index without reading the user
    return await mongo.users().find_one({"username": mongo.typed(username, "string")}, {"_id": 0, "username": 1}) is not None


async def availability(username: str) -> str | None:
    """The reason a normalized username can't be taken right now; None when it's free."""
    if reason := problem(username):
        return reason
    return Reason.TAKEN if await is_taken(username) else None


def default_from(login: str | None) -> str | None:
    """A GitHub login as a username, when it fits the rules."""
    candidate = normalize(login or "")
    return candidate if candidate and problem(candidate) is None else None


def display_name(user: dict | None, fallback: str | None = None) -> str | None:
    """How a person is shown: their name, else their username or GitHub login."""
    user = user or {}
    return user.get("name") or user.get("username") or user.get("github_login") or fallback


def handle(user: dict | None, fallback: str | None = None) -> str | None:
    """How a person is referred to: their username first, else how they're shown."""
    return (user or {}).get("username") or display_name(user, fallback)
