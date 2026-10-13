from pydantic import BaseModel


class AccessToken(BaseModel):
    access_token: str
    token_type: str = "bearer"


class Me(BaseModel):
    id: str
    github_login: str
    name: str | None
    avatar_url: str | None
