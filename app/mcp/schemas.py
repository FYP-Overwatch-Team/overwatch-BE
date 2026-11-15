from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import McpGrantKind, McpScope


class AiTool(BaseModel):
    """An AI tool the member connected over MCP, and the one workspace it reads and records in."""

    id: str
    kind: McpGrantKind
    label: str
    workspace_id: str
    workspace_name: str
    scopes: list[McpScope]
    #: it asks the person before recording anything, unless they asked for it
    ask_first: bool = True
    #: the start of its token, to tell it from the others; the rest is never stored
    token_prefix: str | None
    created_at: datetime
    last_used_at: datetime | None
    #: decisions and questions it has recorded
    records_count: int
    expires_at: datetime | None


class AiToolCreated(AiTool):
    token: str  # the raw token, shown once
    #: the address to give the tool
    url: str


class CreateToken(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    read_only: bool = False
    ask_first: bool = True
    #: the default life when not sent; the server's longest is refused above
    expires_in_days: int | None = Field(default=None, ge=1)


class ChangeAiTool(BaseModel):
    """Only the fields sent are changed."""

    label: str | None = Field(default=None, min_length=1, max_length=60)
    ask_first: bool | None = None


class AiToolSetup(BaseModel):
    """What the setup instructions need: where a tool connects."""

    url: str
