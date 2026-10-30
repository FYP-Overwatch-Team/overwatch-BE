"""Everything that goes over the realtime socket (`/realtime`), in one place.

A client sends `ClientFrame`s; the server sends control frames (`ready`, `subscribed`...) and
`event` frames. Events are named `<resource>.<part>.<past_tense_verb>` and go to a topic,
`<resource>:<id>` (e.g. `decision:9f2c…`); everyone subscribed to the topic gets them.

The socket only carries what happened: every change is made through the REST API first.
"""

import json
import uuid
from enum import IntEnum, StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from app.core.time import utcnow

#: bumped when frames change in a way old clients can't read
PROTOCOL = 1


class EventName(StrEnum):
    """Every event the server sends."""

    DECISION_COMMENT_CREATED = "decision.comment.created"
    DECISION_COMMENT_UPDATED = "decision.comment.updated"
    DECISION_COMMENT_DELETED = "decision.comment.deleted"
    DECISION_TYPING_STARTED = "decision.typing.started"
    DECISION_TYPING_STOPPED = "decision.typing.stopped"
    DECISION_DELETED = "decision.deleted"


class CloseCode(IntEnum):
    """Why the server closed the socket; the client reconnects after all but `BAD_ORIGIN`."""

    BAD_MESSAGE = 4400
    UNAUTHORIZED = 4401
    BAD_ORIGIN = 4403
    AUTH_TIMEOUT = 4408
    TOO_MANY = 4429
    #: it couldn't keep up, so some events were dropped: the client reconnects and refetches
    SLOW_CONSUMER = 1013


class ErrorCode(StrEnum):
    """Why a frame was refused; the socket stays open."""

    BAD_FRAME = "bad_frame"
    BAD_TOPIC = "bad_topic"
    TOPIC_NOT_FOUND = "topic_not_found"
    NOT_SUBSCRIBED = "not_subscribed"
    TOO_MANY_SUBSCRIPTIONS = "too_many_subscriptions"
    UNKNOWN_SIGNAL = "unknown_signal"


class UnsubscribeReason(StrEnum):
    CLIENT = "client"
    #: the user lost access (left the workspace, or was removed)
    FORBIDDEN = "forbidden"
    #: what the topic is about was deleted
    GONE = "gone"


class Auth(BaseModel):
    """First frame on a new socket, and again whenever the client's access token is refreshed."""

    type: Literal["auth"]
    token: str


class Subscribe(BaseModel):
    type: Literal["subscribe"]
    #: the client's id for the request, echoed in the answer
    ref: str = Field(max_length=64)
    topic: str = Field(max_length=200)


class Unsubscribe(BaseModel):
    type: Literal["unsubscribe"]
    ref: str = Field(max_length=64)
    topic: str = Field(max_length=200)


class Signal(BaseModel):
    """Something passing that isn't stored, like typing, sent to a subscribed topic's other members."""

    type: Literal["signal"]
    topic: str = Field(max_length=200)
    name: str = Field(max_length=32)
    state: Literal["start", "stop"]


class Ping(BaseModel):
    type: Literal["ping"]
    ref: str = Field(max_length=64)


ClientFrame = TypeAdapter(Annotated[Auth | Subscribe | Unsubscribe | Signal | Ping, Field(discriminator="type")])


def frame(type_: str, **fields) -> str:
    """A control frame, as sent."""
    return json.dumps({"type": type_, **fields}, default=str)


def event(topic: str, name: EventName | str, data: dict) -> str:
    """An event frame, as sent: serialised once, then sent as is to every subscriber."""
    return frame("event", id=uuid.uuid7().hex, topic=topic, event=str(name), ts=utcnow().isoformat(), data=data)
