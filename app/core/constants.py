"""Values stored in Mongo and returned by the API, named once.

StrEnum members are plain strings, so they compare equal to, and are stored as, their values.
"""

from enum import IntEnum, StrEnum


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


class Product(StrEnum):
    """The brand a deployment presents itself as; `PRODUCT_NAMES` holds how each is written."""

    OVERWATCH = "overwatch"
    BEARINGS = "bearings"


PRODUCT_NAMES = {Product.OVERWATCH: "Overwatch", Product.BEARINGS: "Bearings"}


class HeardFrom(StrEnum):
    """Where someone on the waitlist heard about the product."""

    FRIEND = "friend"
    LINKEDIN = "linkedin"
    X = "x"
    SEARCH = "search"
    UNIVERSITY = "university"
    OTHER = "other"


class UsageFeature(StrEnum):
    """What a model call was made for, as AI usage is split by."""

    CHAT = "chat"
    DECISION_SUGGEST = "decision_suggest"
    DECISION_CHECK = "decision_check"
    #: turning items, pages and decisions into vectors, so related ones can be found by meaning
    EMBEDDINGS = "embeddings"


class DecisionStatus(StrEnum):
    """Where a decision stands. Only people move it; a suggestion from the AI starts as `proposed`."""

    OPEN = "open"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class InboxTab(StrEnum):
    """Where a decision sits in the Decisions inbox, for the member looking at it."""

    #: a proposal to settle, an open one they own or whose review date has come, or an accepted
    #: one whose evidence broke or was contradicted since it was settled
    NEEDS_YOU = "needs_you"
    OPEN = "open"
    DECIDED = "decided"


class DecisionOrigin(StrEnum):
    MANUAL = "manual"
    SUGGESTED = "suggested"


class EvidenceKind(StrEnum):
    """What a piece of evidence quotes: a board item or a docs page."""

    WORK_ITEM = "work_item"
    DOCUMENT = "document"


class EvidenceStance(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class EvidenceState(StrEnum):
    """How a quote compares with its source now: worked out each time a decision is read."""

    #: the source hasn't changed since it was quoted
    CURRENT = "current"
    #: the source changed, but still says what was quoted
    CHANGED = "changed"
    #: the source no longer says what was quoted
    QUOTE_MISSING = "quote_missing"
    #: the source was deleted or archived
    SOURCE_GONE = "source_gone"


class SearchResultKind(StrEnum):
    """What a ⌘K search result opens: a board item, or a docs page."""

    WORK_ITEM = "work_item"
    DOCUMENT = "document"


class CheckMatch(StrEnum):
    """Why a decision check read a source against a decision, strongest first."""

    #: a decision quotes it
    QUOTED = "quoted"
    #: it's the parent, a subtask or an issue link of an item a decision quotes
    LINKED = "linked"
    #: it names a quoted item's key or links a quoted page, or a quoted source names it
    MENTIONED = "mentioned"
    #: it reads close in meaning to the decision
    SIMILAR = "similar"


class EmbeddingKind(StrEnum):
    """What an embedding was made from."""

    WORK_ITEM = "work_item"
    DOCUMENT = "document"
    DECISION = "decision"


class EmbedPurpose(StrEnum):
    """How a text is embedded: as something to find, or as what's searched with."""

    DOCUMENT = "document"
    QUERY = "query"


class VectorSearch(StrEnum):
    """Where nearest embeddings are found: Atlas Vector Search, or compared here (dev and tests)."""

    ATLAS = "atlas"
    LOCAL = "local"


class DecisionCheckInterval(IntEnum):
    """How often a workspace's accepted decisions are checked against what changed, in minutes."""

    HALF_HOUR = 30
    HOUR = 60
    HALF_DAY = 720
    DAY = 1440
