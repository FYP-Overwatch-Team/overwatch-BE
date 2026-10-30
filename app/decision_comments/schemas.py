from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.comments.schemas import BODY, CommentAuthor

#: 32 hex characters, as `uuid.uuid4().hex` makes them
CLIENT_ID = r"^[0-9a-f]{32}$"
#: an emoji as the picker gives it (a character, with any skin tone or joiners): short, no spaces or markup
EMOJI = r"^[^\s\x00-\x1f<>&\"'\\/]{1,32}$"
#: different emoji on one message
MAX_REACTIONS = 20
#: files on one message
MAX_ATTACHMENTS = 10


class MessageAttachment(BaseModel):
    """A file sent with a message, as it was when sent; downloaded through its id."""

    id: str
    filename: str
    content_type: str
    #: image, pdf, document, spreadsheet, presentation or text
    kind: str
    size: int


class Reaction(BaseModel):
    emoji: str
    #: who reacted with it, first first
    user_ids: list[str]


class DecisionComment(BaseModel):
    """A message in a decision's discussion, the same for every viewer: who may change it is worked out
    from `author` (its author edits it; its author or an admin deletes it)."""

    id: str
    decision_id: str
    #: as the author was named when they wrote it, so a former member still shows
    author: CommentAuthor
    #: Markdown
    body: str
    #: the id the sender's client gave it, to match it to the message it shows as sending
    client_id: str | None = None
    #: in the order each emoji was first used
    reactions: list[Reaction] = []
    #: files sent with it, in the order they were added
    attachments: list[MessageAttachment] = []
    #: goes up with every change, so a client keeps the newest copy whichever arrives last
    version: int
    #: the body was changed after it was sent
    edited: bool
    created_at: datetime
    updated_at: datetime


class NewDecisionComment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    #: made by the client: a send retried with the same id isn't posted twice
    client_id: str = Field(pattern=CLIENT_ID)
    #: may be empty when files are sent
    body: str = Field(default="", max_length=BODY["max_length"])
    #: uploads finished on this decision, by their ids
    attachment_ids: list[str] = Field(default=[], max_length=MAX_ATTACHMENTS)

    @model_validator(mode="after")
    def _says_something(self) -> "NewDecisionComment":
        if not self.body and not self.attachment_ids:
            raise ValueError("Write a message or add a file")
        return self


class EditDecisionComment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(**BODY)
