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


class DocumentKind(StrEnum):
    """A docs tree node: folders hold other nodes, pages hold content (and may hold child pages)."""

    FOLDER = "folder"
    PAGE = "page"


class BoardKind(StrEnum):
    """`scrum` boards plan work in sprints; `kanban` boards are a continuous flow."""

    SCRUM = "scrum"
    KANBAN = "kanban"


class ColumnMode(StrEnum):
    """A board's columns: the three status categories, or the tracker's own columns."""

    DEFAULT = "default"
    TRACKER = "tracker"


class ColumnPrompt(StrEnum):
    """Whether the board asks an admin to import the tracker's columns."""

    NONE = "none"
    PENDING = "pending"
    DISMISSED = "dismissed"


class SprintState(StrEnum):
    FUTURE = "future"
    ACTIVE = "active"
    CLOSED = "closed"


class ColumnColor(StrEnum):
    """A board column's color, chosen on the board; none means the default for its name and category."""

    GRAY = "gray"
    BLUE = "blue"
    GREEN = "green"
    RED = "red"
    AMBER = "amber"
    PURPLE = "purple"
    ORANGE = "orange"
    SLATE = "slate"
