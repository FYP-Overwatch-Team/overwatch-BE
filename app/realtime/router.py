"""The realtime socket, `/realtime`: one per browser tab, carrying events for the topics it subscribes to.

1. The browser connects (only from our frontend's origin) and sends `{"type": "auth", "token": …}`
   within `AUTH_TIMEOUT`; the server answers `ready`.
2. It subscribes to topics; each is checked (`topics.authorize`) before it's joined.
3. It re-sends `auth` with each refreshed access token; one left to expire closes the socket.

Frames and codes are in `events.py`; this module only reads, checks and dispatches them.
"""

import asyncio
import time

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.auth import usernames
from app.auth.repository import get_user
from app.core.config import get_settings
from app.core.security import TokenError, read_access_token
from app.core.time import utcnow
from app.realtime import topics
from app.realtime.events import (
    PROTOCOL,
    Auth,
    ClientFrame,
    CloseCode,
    ErrorCode,
    Ping,
    Signal,
    Subscribe,
    Unsubscribe,
    UnsubscribeReason,
    frame,
)
from app.realtime.hub import CloseRequest, Connection, get_hub

logger = structlog.get_logger("app.realtime")
router = APIRouter()

AUTH_TIMEOUT = 10
#: how often the client should ping, so a dead connection is noticed
HEARTBEAT_MS = 25_000
MAX_CONNECTIONS_PER_USER = 20
MAX_TOPICS = 50
#: frames a client may send: a steady rate, with room for a burst
FRAMES_PER_SECOND = 10
FRAME_BURST = 20
#: a signal started again inside this window (typing, while typing) isn't passed on
SIGNAL_EVERY_SECONDS = 2


class Session:
    """One connected socket: who it is, until when its token holds, and how fast it's sending."""

    def __init__(self, connection: Connection, expires_at: float) -> None:
        self.connection = connection
        self.expires_at = expires_at
        self._tokens = float(FRAME_BURST)
        self._refilled = time.monotonic()
        self._started: dict[tuple[str, str], float] = {}

    def allow_frame(self) -> bool:
        """A token bucket: FRAMES_PER_SECOND on average, FRAME_BURST at once."""
        now = time.monotonic()
        self._tokens = min(FRAME_BURST, self._tokens + (now - self._refilled) * FRAMES_PER_SECOND)
        self._refilled = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def allow_start(self, topic: str, signal: str) -> bool:
        now = time.monotonic()
        if now - self._started.get((topic, signal), 0) < SIGNAL_EVERY_SECONDS:
            return False
        self._started[(topic, signal)] = now
        return True


async def _user_from(token: str) -> tuple[dict, float] | None:
    """`{id, name}` and the token's expiry (a timestamp) for a valid access token of a known user."""
    try:
        user_id, expires_at = read_access_token(token)
    except TokenError:
        return None
    user = await get_user(user_id)
    if user is None:
        return None
    return {"id": user_id, "name": usernames.display_name(user)}, expires_at.timestamp()


async def _receive(websocket: WebSocket, timeout: float) -> str | None:
    """The next text frame; None for a binary one. Raises TimeoutError, or WebSocketDisconnect when the client goes."""
    message = await asyncio.wait_for(websocket.receive(), timeout)
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    return message.get("text")


async def _write(connection: Connection) -> None:
    """Send what's queued, in order, until asked to close."""
    while True:
        item = await connection.outbox.get()
        if isinstance(item, CloseRequest):
            await connection.websocket.close(code=item.code)
            return
        await connection.websocket.send_text(item)


@router.websocket("/realtime")
async def realtime(websocket: WebSocket) -> None:
    # browsers send their origin and CORS doesn't cover sockets: without this any site could connect as the user
    if websocket.headers.get("origin") != get_settings().frontend_origin:
        await websocket.close(code=CloseCode.BAD_ORIGIN)
        return
    await websocket.accept()
    try:
        first = ClientFrame.validate_json(await _receive(websocket, AUTH_TIMEOUT) or "")
    except TimeoutError:
        await websocket.close(code=CloseCode.AUTH_TIMEOUT)
        return
    except (ValidationError, WebSocketDisconnect):
        await websocket.close(code=CloseCode.BAD_MESSAGE)
        return
    found = await _user_from(first.token) if isinstance(first, Auth) else None
    if found is None:
        await websocket.close(code=CloseCode.UNAUTHORIZED)
        return
    user, expires_at = found
    hub = get_hub()
    if hub.connections_of(user["id"]) >= MAX_CONNECTIONS_PER_USER:
        await websocket.close(code=CloseCode.TOO_MANY)
        return

    connection = Connection(websocket, user)
    session = Session(connection, expires_at)
    hub.add(connection)
    writer = asyncio.create_task(_write(connection))
    connection.send(frame("ready", protocol=PROTOCOL, user_id=user["id"], heartbeat_ms=HEARTBEAT_MS))
    logger.info("realtime_connected", user_id=user["id"])
    try:
        await _read(session)
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("realtime_disconnected", user_id=user["id"], topics=len(connection.topics))
        hub.remove(connection)
        # a close the server asked for is still queued: let the writer send it; otherwise stop writing
        if not connection.closing:
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def _read(session: Session) -> None:
    """Handle frames until the client goes, its token runs out, or it misbehaves."""
    connection = session.connection
    while not connection.closing:
        left = session.expires_at - utcnow().timestamp()
        try:
            raw = await _receive(connection.websocket, max(left, 0))
        except TimeoutError:
            connection.close(CloseCode.UNAUTHORIZED)
            return
        if not session.allow_frame():
            connection.close(CloseCode.TOO_MANY)
            return
        try:
            received = ClientFrame.validate_json(raw or "")
        except ValidationError:
            connection.send(frame("error", code=ErrorCode.BAD_FRAME, message="That frame isn't one this socket reads."))
            continue
        await _handle(session, received)


async def _handle(session: Session, received) -> None:
    connection, hub = session.connection, get_hub()
    match received:
        case Ping(ref=ref):
            connection.send(frame("pong", ref=ref))
        case Auth(token=token):
            found = await _user_from(token)
            if found is None or found[0]["id"] != connection.user["id"]:
                connection.close(CloseCode.UNAUTHORIZED)
                return
            session.expires_at = found[1]
            await _recheck(connection)
        case Subscribe(ref=ref, topic=topic):
            if topic not in connection.topics and len(connection.topics) >= MAX_TOPICS:
                connection.send(frame("error", ref=ref, code=ErrorCode.TOO_MANY_SUBSCRIPTIONS, message=f"At most {MAX_TOPICS} topics."))
                return
            try:
                workspace_id = await topics.authorize(connection.user["id"], topic)
            except topics.TopicError as error:
                connection.send(frame("error", ref=ref, code=error.code, message="That topic can't be subscribed to."))
                return
            hub.subscribe(connection, topic, workspace_id)
            connection.send(frame("subscribed", ref=ref, topic=topic))
        case Unsubscribe(ref=ref, topic=topic):
            hub.unsubscribe(connection, topic)
            connection.send(frame("unsubscribed", ref=ref, topic=topic, reason=UnsubscribeReason.CLIENT))
        case Signal(topic=topic, name=name, state=state):
            if topic not in connection.topics:
                connection.send(frame("error", code=ErrorCode.NOT_SUBSCRIBED, message="Subscribe to the topic first."))
                return
            kind, _ = topics.parse(topic)
            event_name = kind.signals.get((name, state))
            if event_name is None:
                connection.send(frame("error", code=ErrorCode.UNKNOWN_SIGNAL, message=f"No '{name}' signal here."))
            elif state == "stop" or session.allow_start(topic, name):
                hub.publish(topic, event_name, {"user": connection.user}, exclude_user=connection.user["id"])


async def _recheck(connection: Connection) -> None:
    """On re-auth, each topic is checked again, in case access was lost meanwhile."""
    hub = get_hub()
    for topic in list(connection.topics):
        try:
            await topics.authorize(connection.user["id"], topic)
        except topics.TopicError:
            hub.unsubscribe(connection, topic)
            connection.send(frame("unsubscribed", topic=topic, reason=UnsubscribeReason.FORBIDDEN))
