from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

BODY = {"min_length": 1, "max_length": 20_000}


class CommentAuthor(BaseModel):
    id: str | None
    name: str | None


class Comment(BaseModel):
    id: str
    author: CommentAuthor
    #: Markdown
    body: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: the caller wrote it (here), so may change it
    editable: bool
    #: the caller may remove it: its author, or an admin for comments written here
    deletable: bool


class CommentBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(**BODY)
