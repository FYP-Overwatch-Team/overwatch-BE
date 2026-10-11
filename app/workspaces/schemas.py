from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["owner", "admin", "member"]
InviteRole = Literal["admin", "member"]


class WorkspaceName(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class Workspace(BaseModel):
    id: str
    name: str
    personal: bool
    role: Role


class Member(BaseModel):
    user_id: str
    github_login: str
    name: str | None
    avatar_url: str | None
    role: Role
    joined_at: datetime


class UpdateMemberRequest(BaseModel):
    role: Role


class CreateInviteRequest(BaseModel):
    role: InviteRole = "member"
    # only this GitHub account may accept, when set
    github_login: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,39}$")


class Invite(BaseModel):
    id: str
    role: InviteRole
    github_login: str | None
    created_at: datetime
    expires_at: datetime


class InviteCreated(Invite):
    url: str  # carries the raw token, shown once


class InvitePreview(BaseModel):
    workspace_name: str
    invited_by: str
    role: InviteRole
