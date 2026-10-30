from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.core.constants import ConnectionStatus, ConnectorCategory

# keys go into provider queries (JQL), so only plain key shapes get through
TargetKey = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class AuthorizeRequest(BaseModel):
    return_to: Literal["onboarding", "settings"] = "onboarding"


class AuthorizationUrl(BaseModel):
    authorization_url: str


class Target(BaseModel):
    key: str
    name: str | None
    id: str | None


class AddTargetRequest(BaseModel):
    key: str = TargetKey
    name: str | None = Field(default=None, max_length=200)


class SyncTarget(BaseModel):
    key: str
    name: str | None = None
    sync_status: str
    sync_error: str | None = None
    last_synced_at: datetime | None = None


class ExternalAccount(BaseModel):
    id: str
    name: str | None = None
    url: str | None = None


class Connection(BaseModel):
    provider: str
    name: str
    category: ConnectorCategory
    status: ConnectionStatus
    # false for grants made before write-back existed: reconnect to edit from the board
    can_write: bool
    # true when the grant lacks a scope the connector now asks for (a board's columns): reconnect
    outdated: bool = False
    external_account: ExternalAccount | None = None
    connected_by: str | None = None
    connected_at: datetime | None = None
    targets: list[SyncTarget]


class Assignee(BaseModel):
    id: str
    name: str | None
    avatar_url: str | None = None
