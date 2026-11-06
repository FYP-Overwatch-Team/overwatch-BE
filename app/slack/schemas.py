from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

#: a 24-hour time of day, as the digest is scheduled
DIGEST_TIME = r"^([01]\d|2[0-3]):[0-5]\d$"


class DigestSettings(BaseModel):
    enabled: bool
    #: when the digest is posted each day, in `time_zone`
    time: str
    #: IANA zone, e.g. Asia/Karachi; null until an admin saves one, and the digest runs in UTC meanwhile
    time_zone: str | None


class UpdateDigest(BaseModel):
    enabled: bool | None = None
    time: str | None = Field(default=None, pattern=DIGEST_TIME)
    time_zone: str | None = Field(default=None, max_length=64)

    @field_validator("time_zone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return value
