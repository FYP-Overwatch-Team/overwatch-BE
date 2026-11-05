"""Who is connected and to which topics, and sending events to them.

Each connection has its own outbox, written to the socket by its own task, so one slow client
never holds up the others. An outbox that fills up closes its connection (SLOW_CONSUMER): the
client reconnects and refetches, so it never silently misses an event.

This hub lives in the one API process. Run more processes and they need a shared broker (Redis
pub/sub): `publish` would send there, and each process would hand what arrives to `deliver`.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import WebSocket

from app.realtime.events import CloseCode, EventName, UnsubscribeReason, event, frame

#: frames waiting for a slow client before its connection is closed
OUTBOX_SIZE = 256


@dataclass(frozen=True)
class CloseRequest:
    """Put in an outbox instead of a frame: the writer closes the socket with `code`."""

    code: int


@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    #: `{id, name}`: who is connected, as events about them name them
    user: dict
    #: topic -> the workspace it belongs to
    topics: dict[str, str] = field(default_factory=dict)
    outbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(OUTBOX_SIZE))
    closing: bool = False

    def send(self, data: str) -> None:
        """Queue a frame; a full outbox closes the connection instead."""
        if self.closing:
            return
        try:
            self.outbox.put_nowait(data)
        except asyncio.QueueFull:
            self.close(CloseCode.SLOW_CONSUMER)

    def close(self, code: int) -> None:
        """Close once what's queued is dropped: the client refetches on reconnect anyway."""
        if self.closing:
            return
        self.closing = True
        while not self.outbox.empty():
            self.outbox.get_nowait()
        self.outbox.put_nowait(CloseRequest(code))


class Hub:
    def __init__(self) -> None:
        self._by_topic: dict[str, set[Connection]] = defaultdict(set)
        self._by_user: dict[str, set[Connection]] = defaultdict(set)

    def connections_of(self, user_id: str) -> int:
        return len(self._by_user.get(user_id, ()))

    def add(self, connection: Connection) -> None:
        self._by_user[connection.user["id"]].add(connection)

    def remove(self, connection: Connection) -> None:
        for topic in list(connection.topics):
            self.unsubscribe(connection, topic)
        self._discard(self._by_user, connection.user["id"], connection)

    def subscribe(self, connection: Connection, topic: str, workspace_id: str) -> None:
        connection.topics[topic] = workspace_id
        self._by_topic[topic].add(connection)

    def unsubscribe(self, connection: Connection, topic: str) -> None:
        connection.topics.pop(topic, None)
        self._discard(self._by_topic, topic, connection)

    def publish(self, topic: str, name: EventName, data: dict, *, exclude_user: str | None = None) -> None:
        """Send an event to everyone subscribed to `topic`; `exclude_user`'s own connections are left out."""
        self.deliver(topic, event(topic, name, data), exclude_user)

    def deliver(self, topic: str, data: str, exclude_user: str | None = None) -> None:
        for connection in list(self._by_topic.get(topic, ())):
            if connection.user["id"] != exclude_user:
                connection.send(data)

    def revoke(self, workspace_id: str, user_id: str | None = None) -> None:
        """The user (everyone, when None) lost access to the workspace: their subscriptions to its topics end."""
        users = [user_id] if user_id else list(self._by_user)
        for connection in [c for user in users for c in list(self._by_user.get(user, ()))]:
            for topic, topic_workspace in list(connection.topics.items()):
                if topic_workspace == workspace_id:
                    self.unsubscribe(connection, topic)
                    connection.send(frame("unsubscribed", topic=topic, reason=UnsubscribeReason.FORBIDDEN))

    def close_topic(self, topic: str, reason: UnsubscribeReason) -> None:
        """What the topic is about is gone: everyone on it is unsubscribed."""
        for connection in list(self._by_topic.get(topic, ())):
            self.unsubscribe(connection, topic)
            connection.send(frame("unsubscribed", topic=topic, reason=reason))

    @staticmethod
    def _discard(index: dict[str, set[Connection]], key: str, connection: Connection) -> None:
        connections = index.get(key)
        if connections is not None:
            connections.discard(connection)
            if not connections:
                del index[key]


_hub = Hub()


def get_hub() -> Hub:
    return _hub


def use_hub(hub: Hub | None) -> None:
    """Swap the hub (tests); None puts back a fresh one."""
    global _hub
    _hub = hub or Hub()
