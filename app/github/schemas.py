from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.repo_identity import REPO_FULL_NAME_PATTERN


class ConnectRepoRequest(BaseModel):
    # validated at the edge too: it ends up in a filesystem path and a GitHub URL
    repo_full_name: str = Field(pattern=REPO_FULL_NAME_PATTERN, max_length=140)


class Installation(BaseModel):
    id: int
    account_login: str
    account_type: str
    repository_selection: str | None


class Installations(BaseModel):
    install_url: str
    installations: list[Installation]


class AvailableRepo(BaseModel):
    """A repo the user can see through one of their installations."""

    id: int
    full_name: str
    private: bool
    default_branch: str | None
    installation_id: int
    connected: bool


class ConnectedRepo(BaseModel):
    repo_full_name: str
    default_branch: str
    access_status: str = "active"  # active | revoked | needs_install
    parse_status: str  # pending | in_progress | done | failed
    parse_error: str | None = None
    indexed_at: datetime | None = None
    graph_stats: dict[str, Any] | None = None


class WebhookResult(BaseModel):
    outcome: str  # accepted | ignored | duplicate_ignored
    repos: int | None = None
    event: str | None = None
