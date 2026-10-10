from pydantic import BaseModel, Field


class ConnectProjectRequest(BaseModel):
    # project keys go into JQL, so only real key shapes get through
    project_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,49}$")


class AuthorizationUrl(BaseModel):
    authorization_url: str


class JiraProject(BaseModel):
    key: str
    name: str | None
    id: str | None


class ConnectedProject(BaseModel):
    project_key: str
    cloud_id: str
    sync_status: str
