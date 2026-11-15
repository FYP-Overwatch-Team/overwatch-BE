from pydantic import BaseModel, Field, field_validator

from app.auth.schemas import EmailAddress
from app.core.constants import HeardFrom


class JoinWaitlist(EmailAddress):
    name: str = Field(min_length=1, max_length=80)
    heard_from: HeardFrom

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter your name")
        return value.strip()


class WaitlistJoined(BaseModel):
    # true when the email was on the list already: nothing changed and no email went out
    already_joined: bool
