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
    CONFLUENCE = "confluence"


class CredentialGroup(StrEnum):
    """Providers that share one OAuth app and one token: one consent covers every product in the group."""

    ATLASSIAN = "atlassian"


class ConnectorCategory(StrEnum):
    """What a connector mirrors: tracker tickets into `work_items`, or pages into `documents`."""

    WORK_ITEMS = "work_items"
    DOCUMENTS = "documents"


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


class DocPushState(StrEnum):
    """Where a mirrored page's local edits are on their way back to the source."""

    NONE = "none"
    PENDING = "pending"
    PUSHING = "pushing"
    # the source changed too: its version is shown, the local edit kept for the user to resolve
    CONFLICT = "conflict"


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


class WorkItemIcon(StrEnum):
    """The icons a work item type can have (Phosphor icon names, drawn by the frontend)."""

    CHECK_SQUARE = "CheckSquare"
    BUG = "Bug"
    FLASK = "Flask"
    BOOKMARK = "BookmarkSimple"
    LIGHTNING = "Lightning"
    TREE = "TreeStructure"
    STAR = "Star"
    FLAG = "Flag"
    WRENCH = "Wrench"
    ROCKET = "Rocket"
    LIGHTBULB = "Lightbulb"
    SHIELD = "ShieldCheck"
    GEAR = "Gear"
    PAINT_BRUSH = "PaintBrush"
    FILE = "FileText"
    MEGAPHONE = "Megaphone"
    QUESTION = "Question"
    TARGET = "Target"
    PUZZLE = "PuzzlePiece"
    CODE = "Code"
    CIRCLE = "Circle"


class ChallengePurpose(StrEnum):
    """What an emailed one-time code proves: a new account's address, the right to reset a password, or an address
    added to a signed-in account."""

    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"
    ADD_EMAIL = "add_email"


class InviteKind(StrEnum):
    """Who may accept an invite: anyone with the link, one GitHub account, or one email address."""

    LINK = "link"
    GITHUB = "github"
    EMAIL = "email"


class AuthErrorCode(StrEnum):
    """Error codes of the email and password flows the frontend reacts to."""

    INVALID_CREDENTIALS = "invalid_credentials"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    INVALID_CODE = "invalid_code"
    OTP_ATTEMPTS_EXCEEDED = "otp_attempts_exceeded"
    OTP_RESEND_COOLDOWN = "otp_resend_cooldown"
    OTP_RESEND_LIMIT = "otp_resend_limit"
    USERNAME_ALREADY_SET = "username_already_set"
    GITHUB_ACCOUNT_IN_USE = "github_account_in_use"
    ACCOUNT_EXISTS = "account_exists"
    EMAIL_ALREADY_SET = "email_already_set"
    MERGE_NOT_FOUND = "merge_not_found"
    MERGE_CONFLICT = "merge_conflict"
    MERGE_IN_PROGRESS = "merge_in_progress"
    ACCOUNT_MERGING = "account_merging"
    LAST_LOGIN_METHOD = "last_login_method"


class MergeStatus(StrEnum):
    """A merge in the audit trail: still running (and resumed if interrupted), or finished."""

    RUNNING = "running"
    DONE = "done"


class MergeProof(StrEnum):
    """How the other account was proven: its emailed code, or signing in to its GitHub."""

    EMAIL_OTP = "email_otp"
    GITHUB_OAUTH = "github_oauth"


class KeepProfile(StrEnum):
    """Whose name, username and picture a merged account keeps."""

    CURRENT = "current"
    OTHER = "other"
