from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.core.constants import InviteKind, Role

InviteRole = Literal[Role.ADMIN, Role.MEMBER]


class WorkspaceName(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class Workspace(BaseModel):
    id: str
    name: str
    personal: bool
    role: Role


class Member(BaseModel):
    user_id: str
    username: str | None
    github_login: str | None
    email: str | None
    name: str | None
    avatar_url: str | None
    role: Role
    joined_at: datetime


class UpdateMemberRequest(BaseModel):
    role: Role


class CreateInviteRequest(BaseModel):
    role: InviteRole = Role.MEMBER
    # only this GitHub account may accept, when set
    github_login: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,39}$")
    # only this email's account may accept; the link is mailed there and never shown
    email: EmailStr | None = Field(default=None, max_length=254)

    @field_validator("email")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return value and value.strip().lower()

    @model_validator(mode="after")
    def _one_restriction(self) -> "CreateInviteRequest":
        if self.github_login and self.email:
            raise PydanticCustomError("one_restriction", "Restrict an invite to a GitHub account or an email, not both")
        return self


class Invite(BaseModel):
    id: str
    kind: InviteKind
    role: InviteRole
    github_login: str | None
    email: str | None
    created_at: datetime
    expires_at: datetime
    last_sent_at: datetime | None


class InviteCreated(Invite):
    url: str | None  # carries the raw token, shown once; never for an email invite


class InviteToken(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class InvitePreview(BaseModel):
    workspace_name: str
    invited_by: str
    role: InviteRole


class PublicInvitePreview(InvitePreview):
    kind: InviteKind
    email: str | None
    account_exists: bool | None
