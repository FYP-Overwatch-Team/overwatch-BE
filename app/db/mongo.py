from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import get_settings
from app.core.constants import NATIVE_SOURCE, SprintState

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def typed(value: object, bson_type: str) -> dict:
    """An equality on a field whose unique index is partial on `$type`: Mongo only uses that index when the query
    repeats the type, so a plain `{field: value}` would scan the collection."""
    return {"$eq": value, "$type": bson_type}


def connect() -> AsyncIOMotorDatabase:
    global _client, _db
    settings = get_settings()
    # tz_aware: datetimes come back UTC-aware, so comparisons with utcnow() just work
    _client = AsyncIOMotorClient(settings.mongo_uri, tz_aware=True)
    _db = _client[settings.mongo_db_name]
    return _db


def close() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Mongo is not connected; call connect() on startup")
    return _db


def use_db(db: AsyncIOMotorDatabase) -> None:
    """Inject a database instance (tests use mongomock-motor here)."""
    global _db
    _db = db


# Collection accessors — single place to change names/indexes later.
def users():
    return get_db()["users"]


def auth_challenges():
    """Emailed one-time codes (email verification, password reset); dropped once `expires_at` passes."""
    return get_db()["auth_challenges"]


def merge_proposals():
    """A proven, not yet confirmed merge of another account into the signed-in one; short-lived."""
    return get_db()["merge_proposals"]


def account_merges():
    """Every merge of one account into another, kept as the audit trail; also how an interrupted merge resumes."""
    return get_db()["account_merges"]


def oauth_tokens():
    return get_db()["oauth_tokens"]


def refresh_tokens():
    return get_db()["refresh_tokens"]


def repos():
    return get_db()["repos"]


def index_locks():
    """One doc per repo: the indexing lock and any work queued behind it."""
    return get_db()["index_locks"]


def connections():
    """One per workspace and provider (Jira, ...): how it's authorised and by whom."""
    return get_db()["connections"]


def sync_targets():
    """The projects (lists, channels) a connection syncs."""
    return get_db()["sync_targets"]


def work_item_types():
    """A workspace's kinds of work item (Task, Bug, ...), with the icon and color each shows with."""
    return get_db()["work_item_types"]


def work_items():
    """Board items: mirrored from a tracker (`source` = provider) or created here (`native`)."""
    return get_db()["work_items"]


def boards():
    """One per tracker target (`source` = provider) plus one native board per workspace: columns, mode, prompt."""
    return get_db()["boards"]


def sprints():
    """A board's sprints: native ones, and mirrors of the tracker's (`external_id` set)."""
    return get_db()["sprints"]


def work_item_comments():
    """Comments on native items, and who in Overwatch wrote the tracker comments posted from here."""
    return get_db()["work_item_comments"]


def jira_projects():
    """Legacy: replaced by sync_targets; read only by migration scripts."""
    return get_db()["jira_projects"]


def tickets():
    """Legacy: replaced by work_items; read only by migration scripts."""
    return get_db()["tickets"]


def documents():
    """Docs tree nodes (folders and pages): written here (`native`) or mirrored from a docs provider."""
    return get_db()["documents"]


def doc_attachments():
    """Files (images) uploaded into docs pages: where each is stored, and whether its upload finished."""
    return get_db()["doc_attachments"]


def workspaces():
    return get_db()["workspaces"]


def workspace_members():
    return get_db()["workspace_members"]


def workspace_invites():
    return get_db()["workspace_invites"]


def webhook_deliveries():
    return get_db()["webhook_deliveries"]


def chat_conversations():
    """AI chat conversations, private to the member who started them; messages embedded."""
    return get_db()["chat_conversations"]


def usage_counters():
    """Fixed-window request counters for rate limits; expire with their window."""
    return get_db()["usage_counters"]


def link_previews():
    """What a link shows as a card, by its address; dropped once `expires_at` passes."""
    return get_db()["link_previews"]


def file_facts():
    """Knowledge-graph extraction facts, one document per file."""
    return get_db()["file_facts"]


async def ensure_indexes() -> None:
    """Create the indexes the hot paths depend on. Idempotent — safe on every boot.

    The two TTL indexes are what keep the session store from growing without
    bound: Mongo drops refresh-token rows once they pass `expires_at`, and
    webhook delivery ids 30 days after receipt (GitHub never retries that late,
    so anything older can no longer cause a duplicate re-parse).
    """
    # Session lookup on every /auth/refresh, plus family-wide revocation.
    await refresh_tokens().create_index("token_hash", unique=True)
    await refresh_tokens().create_index("family_id")
    await refresh_tokens().create_index("user_id")  # logout-all
    await refresh_tokens().create_index("expires_at", expireAfterSeconds=0)

    # partial: email-only users have no GitHub id, and a plain unique index would count each missing one as a duplicate null
    if "github_id_1" in await users().index_information():
        await users().drop_index("github_id_1")
    await users().create_index(
        "github_id", unique=True, name="github_id_unique", partialFilterExpression={"github_id": {"$type": "number"}},
    )
    # emails and usernames are stored lowercase, so exact-match uniqueness is uniqueness ignoring case
    await users().create_index("email", unique=True, partialFilterExpression={"email": {"$type": "string"}})
    await users().create_index("username", unique=True, partialFilterExpression={"username": {"$type": "string"}})
    # an account whose email was never verified is deleted 7 days after its last sign-up attempt
    await users().create_index("unverified_expires_at", expireAfterSeconds=0)
    await auth_challenges().create_index([("user_id", 1), ("purpose", 1)])
    await auth_challenges().create_index("expires_at", expireAfterSeconds=0)
    # one pending merge per account; a proof unconfirmed for a while lapses
    await merge_proposals().create_index("survivor_id", unique=True)
    await merge_proposals().create_index("expires_at", expireAfterSeconds=0)
    await account_merges().create_index([("status", 1), ("updated_at", 1)])
    await account_merges().create_index("absorbed_id")
    await account_merges().create_index("survivor_id")
    await workspace_members().create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await workspace_members().create_index("user_id")
    await workspace_invites().create_index("token_hash", unique=True)
    await workspace_invites().create_index("workspace_id")
    # one pending invite per email in a workspace; `pending_email` is unset once it's accepted, revoked or expired
    await workspace_invites().create_index(
        [("workspace_id", 1), ("pending_email", 1)], unique=True,
        partialFilterExpression={"pending_email": {"$type": "string"}},
    )
    await oauth_tokens().create_index([("owner_id", 1), ("provider", 1)], unique=True)
    await repos().create_index([("workspace_id", 1), ("repo_full_name", 1)], unique=True)
    await repos().create_index("repo_full_name")
    # webhook routing: push events carry the repo id, installation events the installation id
    await repos().create_index("github_repo_id")
    await repos().create_index("installation_id")
    await connections().create_index([("workspace_id", 1), ("provider", 1)], unique=True)
    await sync_targets().create_index([("workspace_id", 1), ("provider", 1), ("key", 1)], unique=True)
    await sync_targets().create_index("status")
    # Must match the sync upsert filter: keyed on the provider's stable id, so an issue
    # moved between projects (which changes its key) updates rather than duplicating.
    await work_items().create_index([("workspace_id", 1), ("source", 1), ("external_id", 1)], unique=True)
    await work_items().create_index([("workspace_id", 1), ("status_category", 1), ("rank", 1)])
    await work_items().create_index([("workspace_id", 1), ("source", 1), ("target_key", 1), ("column_id", 1), ("rank", 1)])
    await boards().create_index([("workspace_id", 1), ("source", 1), ("target_key", 1)], unique=True)
    await sprints().create_index(
        [("workspace_id", 1), ("source", 1), ("external_id", 1)], unique=True,
        partialFilterExpression={"external_id": {"$type": "string"}},
    )
    await sprints().create_index([("board_id", 1), ("state", 1)])
    # one active native sprint per board, even when two admins start sprints at once (Jira allows parallel ones)
    await sprints().create_index(
        "board_id", unique=True, name="one_active_native_sprint",
        partialFilterExpression={"source": NATIVE_SOURCE, "state": SprintState.ACTIVE.value},
    )
    await work_item_comments().create_index([("workspace_id", 1), ("item_id", 1), ("created_at", 1)])
    # a folder's children in order: the docs tree loads one level at a time
    await documents().create_index([("workspace_id", 1), ("parent_id", 1), ("archived_at", 1), ("rank", 1)])
    # a node's whole subtree, through the ancestors each node keeps (archive, move)
    await documents().create_index([("workspace_id", 1), ("ancestor_ids", 1)])
    # each archive's group, restored together
    await documents().create_index([("workspace_id", 1), ("archive_root_id", 1)])
    await doc_attachments().create_index([("workspace_id", 1), ("node_id", 1)])
    # images copied from a docs source, found again when a page syncs
    await doc_attachments().create_index(
        [("workspace_id", 1), ("source", 1), ("external_id", 1)], unique=True,
        partialFilterExpression={"external_id": {"$type": "string"}},
    )
    # a synced target's mirror, read whole on each sync
    await documents().create_index([("workspace_id", 1), ("source", 1), ("target_key", 1)])
    # archives past their retention, across workspaces (the hourly clean-up)
    await documents().create_index([("archived_at", 1)], partialFilterExpression={"archived_root": True})
    # mirrored pages waiting to go back to their source (only mirrored pages have a push state)
    await documents().create_index([("push_state", 1), ("push_due_at", 1)], partialFilterExpression={"push_state": {"$type": "string"}})
    # the Archive list: what was archived, newest first
    await documents().create_index(
        [("workspace_id", 1), ("archived_at", -1)], partialFilterExpression={"archived_root": True},
    )
    await documents().create_index(
        [("workspace_id", 1), ("source", 1), ("external_id", 1)], unique=True,
        partialFilterExpression={"external_id": {"$type": "string"}},
    )
    await work_items().create_index([("workspace_id", 1), ("source", 1), ("target_key", 1), ("sprint_id", 1)])
    # a type's items, found again when it's deleted
    await work_items().create_index([("workspace_id", 1), ("type_id", 1)])
    # a board item by its key, as a link to it on the board looks it up; keys repeat across trackers, so not unique
    await work_items().create_index([("workspace_id", 1), ("source", 1), ("target_key", 1), ("key", 1)])
    # one type per name in a workspace, whatever its case
    await work_item_types().create_index([("workspace_id", 1), ("name_key", 1)], unique=True)

    # Extraction cache: read on every sync to decide what changed, and always
    # scoped to one repository.
    await file_facts().create_index([("repo_full_name", 1), ("path", 1)], unique=True)

    # Webhook dedup: looked up on every delivery, expired 30 days after receipt.
    await chat_conversations().create_index([("workspace_id", 1), ("user_id", 1), ("repo_full_name", 1), ("updated_at", -1)])
    await chat_conversations().create_index("expires_at", expireAfterSeconds=0)
    await usage_counters().create_index("expires_at", expireAfterSeconds=0)
    await link_previews().create_index("expires_at", expireAfterSeconds=0)
    await webhook_deliveries().create_index("delivery_id", unique=True)
    await webhook_deliveries().create_index("received_at", expireAfterSeconds=30 * 24 * 3600)
