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
    connected_at: datetime
    updated_at: datetime
