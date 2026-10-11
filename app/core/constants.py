"""Values stored in Mongo and returned by the API, named once.

StrEnum members are plain strings, so they compare equal to, and are stored as, their values.
"""

from enum import StrEnum


class Role(StrEnum):
    """A workspace member's role, lowest first: the order is the rank."""

    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


class Provider(StrEnum):
    GITHUB = "github"
    JIRA = "jira"


# `work_items.source` for items created in Overwatch; tracker items carry their provider
NATIVE_SOURCE = "native"


class AuthBackendName(StrEnum):
    """How a connection's requests are authenticated."""

    NATIVE_OAUTH = "native_oauth"


class ConnectionStatus(StrEnum):
    """An OAuth token's or a tracker connection's state."""

    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    DISCONNECTED = "disconnected"


class AccessStatus(StrEnum):
    """Whether the GitHub App can still read a connected repo."""

    ACTIVE = "active"
    REVOKED = "revoked"
    NEEDS_INSTALL = "needs_install"


class JobStatus(StrEnum):
    """A background job's state: repo indexing (`parse_status`) or a tracker sync (`sync_status`)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class StatusCategory(StrEnum):
    """A board column."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
