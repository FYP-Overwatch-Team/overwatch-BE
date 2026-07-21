from datetime import datetime

from pydantic import BaseModel, Field


class User(BaseModel):
    id: str = Field(alias="_id")
    github_id: int
    github_login: str
    name: str | None = None
    avatar_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}
