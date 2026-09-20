from datetime import datetime

from pydantic import BaseModel


class Repo(BaseModel):
    """A connected GitHub repository.

    webhook_status and parse_status are deliberately independent — webhook
    registration can succeed while clone/parse fails and vice versa, and the
    onboarding UI needs to show which part failed.
    """

    user_id: str
    repo_full_name: str
    default_branch: str
    webhook_id: int | None = None
    webhook_secret_encrypted: str | None = None
    webhook_status: str = "pending"  # pending | created | failed
    webhook_error: str | None = None
    parse_status: str = "pending"  # pending | in_progress | done | failed
    parse_error: str | None = None
    # Set while the indexing lock is held. A lock older than
    # `parse_lock_stale_minutes` is treated as abandoned by a crashed run.
    parse_started_at: datetime | None = None
    # Work that arrived while an index was running, drained before the lock is
    # released so a push is never silently dropped.
    sync_requested_at: datetime | None = None
    pending_full: bool = False
    pending_changed: list[str] = []
    pending_removed: list[str] = []
    connected_at: datetime
    updated_at: datetime
