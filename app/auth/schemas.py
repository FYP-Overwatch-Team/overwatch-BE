from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.auth.passwords import PASSWORD_MAX_LENGTH
from app.auth.usernames import USERNAME_MAX_LENGTH
from app.core.constants import Role

# generous outer bounds; the real password and username rules give friendlier messages in the service
Password = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH * 4)
Code = Field(pattern=r"^\d{6}$")
ChallengeId = Field(min_length=1, max_length=64)


class AccessToken(BaseModel):
    access_token: str
    token_type: str = "bearer"


class Me(BaseModel):
    id: str
    username: str | None
    email: str | None
    github_login: str | None
    name: str | None
    avatar_url: str | None


class _Email(BaseModel):
    email: EmailStr = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class _NewAccount(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH * 2)
    password: str = Password

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter your name")
        return value.strip()


class SignupRequest(_NewAccount, _Email):
    pass


class InviteSignupRequest(_NewAccount):
    token: str = Field(min_length=1, max_length=128)


class LoginRequest(_Email):
    password: str = Password


class ForgotPasswordRequest(_Email):
    pass


class VerifyEmailRequest(BaseModel):
    challenge_id: str = ChallengeId
    code: str = Code


class ResendRequest(BaseModel):
    challenge_id: str = ChallengeId


class ResetPasswordRequest(VerifyEmailRequest):
    password: str = Password


class ChallengeIssued(BaseModel):
    challenge_id: str
    email: str
    resend_available_in: int


class ResendAccepted(BaseModel):
    resend_available_in: int


class JoinedWorkspace(BaseModel):
    id: str
    name: str
    personal: bool
    role: Role


class InviteSession(AccessToken):
    workspace: JoinedWorkspace


class UsernameAvailability(BaseModel):
    username: str
    available: bool
    reason: Literal["invalid", "reserved", "taken"] | None


class SetUsername(BaseModel):
    username: str = Field(min_length=1, max_length=USERNAME_MAX_LENGTH * 2)


class LinkGithubRequest(BaseModel):
    return_to: Literal["onboarding", "settings"] = "settings"


class AuthorizationUrl(BaseModel):
    authorization_url: str
