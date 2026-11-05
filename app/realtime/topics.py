"""The topics a client can subscribe to, and who may.

A topic is `<kind>:<id>`. Each kind says how to check a user may see it (returning the workspace
it belongs to, so access can be taken back when they leave it) and which signals it carries.
Adding a kind is one entry in `KINDS`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.exceptions import AppError
from app.db import mongo
from app.realtime.events import ErrorCode, EventName
from app.workspaces.service import require_member


class TopicError(Exception):
    def __init__(self, code: ErrorCode):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Kind:
    #: the workspace the topic's resource belongs to, when the user is a member there; None otherwise
    workspace_of: Callable[[str, str], Awaitable[str | None]]
    #: signals members may send to the topic, as (name, state) -> the event the others get
    signals: dict[tuple[str, str], EventName] = field(default_factory=dict)


async def _member_of(workspace_id: str, user_id: str) -> bool:
    # as the REST API decides it: a member of a workspace that still exists
    try:
        await require_member(workspace_id, user_id)
    except AppError:
        return False
    return True


async def _decision_workspace(user_id: str, decision_id: str) -> str | None:
    decision = await mongo.decisions().find_one({"_id": decision_id}, {"workspace_id": 1})
    if decision is None or not await _member_of(decision["workspace_id"], user_id):
        return None
    return decision["workspace_id"]


KINDS: dict[str, Kind] = {
    "decision": Kind(_decision_workspace, {
        ("typing", "start"): EventName.DECISION_TYPING_STARTED,
        ("typing", "stop"): EventName.DECISION_TYPING_STOPPED,
    }),
}


def name(kind: str, resource_id: str) -> str:
    """A topic's name, e.g. `decision:9f2c…`."""
    return f"{kind}:{resource_id}"


def parse(topic: str) -> tuple[Kind, str]:
    """The topic's kind and resource id; TopicError(`bad_topic`) when it isn't one."""
    kind, _, resource_id = topic.partition(":")
    if kind not in KINDS or not resource_id:
        raise TopicError(ErrorCode.BAD_TOPIC)
    return KINDS[kind], resource_id


async def authorize(user_id: str, topic: str) -> str:
    """The workspace the topic belongs to; TopicError(`topic_not_found`) when it doesn't exist or the
    user can't see it (the same answer, so a topic's existence never leaks)."""
    kind, resource_id = parse(topic)
    workspace_id = await kind.workspace_of(user_id, resource_id)
    if workspace_id is None:
        raise TopicError(ErrorCode.TOPIC_NOT_FOUND)
    return workspace_id
