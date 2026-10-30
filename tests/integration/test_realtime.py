"""The realtime socket end to end: who may connect and subscribe, and what they hear."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import security
from app.core.config import get_settings
from app.realtime import router as realtime
from tests.integration.test_decision_comments import workspace_of_three
from tests.integration.test_workspaces import bearer, user


def connect(tc: TestClient, origin: str | None = None):
    return tc.websocket_connect("/realtime", headers={"origin": origin or get_settings().frontend_origin})


def token_of(headers: dict) -> str:
    return bearer(headers)["Authorization"].removeprefix("Bearer ")


def signed_in(ws, headers: dict) -> dict:
    ws.send_json({"type": "auth", "token": token_of(headers)})
    ready = ws.receive_json()
    assert ready["type"] == "ready"
    return ready


def subscribe(ws, topic: str) -> dict:
    ws.send_json({"type": "subscribe", "ref": "r1", "topic": topic})
    return ws.receive_json()


def closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_json()
    return closed.value.code


async def test_only_our_frontend_with_a_valid_token_connects(client, app):
    owner, *_ = await workspace_of_three(client)
    with TestClient(app) as tc:
        # refused before the handshake completes
        with pytest.raises(WebSocketDisconnect) as refused, connect(tc, origin="https://evil.test"):
            pass
        assert refused.value.code == 4403
        with connect(tc) as ws:
            ws.send_json({"type": "auth", "token": "nope"})
            assert closed_with(ws) == 4401
        with connect(tc) as ws:
            ws.send_json({"type": "subscribe", "ref": "r", "topic": "decision:x"})
            assert closed_with(ws) == 4401
        with connect(tc) as ws:
            assert signed_in(ws, owner)["protocol"] == 1


async def test_a_member_hears_messages_as_they_are_sent_and_an_outsider_cant_listen(client, app):
    owner, member, _, decision_id = await workspace_of_three(client)
    outsider = await user(client, "zed")
    topic = f"decision:{decision_id}"
    with TestClient(app) as tc, connect(tc) as listening, connect(tc) as prying:
        signed_in(listening, member)
        signed_in(prying, outsider)
        assert subscribe(listening, topic) == {"type": "subscribed", "ref": "r1", "topic": topic}
        assert subscribe(prying, topic)["code"] == "topic_not_found"

        sent = tc.post(
            f"/decisions/{decision_id}/comments", json={"client_id": uuid.uuid4().hex, "body": "Hello"}, headers=owner,
        ).json()["data"]
        tc.patch(f"/decisions/{decision_id}/comments/{sent['id']}", json={"body": "Hello all"}, headers=owner)
        tc.delete(f"/decisions/{decision_id}/comments/{sent['id']}", headers=owner)

        created, updated, deleted = (listening.receive_json() for _ in range(3))
        assert (created["event"], created["data"]["message"]["body"]) == ("decision.comment.created", "Hello")
        assert created["data"]["message"]["created_at"] == sent["created_at"]
        assert (updated["event"], updated["data"]["message"]["version"]) == ("decision.comment.updated", 2)
        assert deleted["event"] == "decision.comment.deleted" and deleted["data"] == {"id": sent["id"]}


async def test_typing_reaches_the_others_but_not_the_typist(client, app):
    owner, member, _, decision_id = await workspace_of_three(client)
    topic = f"decision:{decision_id}"
    with TestClient(app) as tc, connect(tc) as typing, connect(tc) as reading:
        for ws, headers in ((typing, member), (reading, owner)):
            signed_in(ws, headers)
            subscribe(ws, topic)

        typing.send_json({"type": "signal", "topic": topic, "name": "typing", "state": "start"})
        # a second start straight after isn't passed on; the stop is
        typing.send_json({"type": "signal", "topic": topic, "name": "typing", "state": "start"})
        typing.send_json({"type": "signal", "topic": topic, "name": "typing", "state": "stop"})
        typing.send_json({"type": "signal", "topic": topic, "name": "shouting", "state": "start"})

        started, stopped = reading.receive_json(), reading.receive_json()
        assert (started["event"], started["data"]["user"]["name"]) == ("decision.typing.started", "sara")
        assert stopped["event"] == "decision.typing.stopped"
        assert typing.receive_json()["code"] == "unknown_signal"


async def test_a_removed_member_stops_hearing_at_once(client, app):
    owner, member, _, decision_id = await workspace_of_three(client)
    topic = f"decision:{decision_id}"
    member_id = "id-sara"  # as tests.integration.test_workspaces.user names them
    with TestClient(app) as tc, connect(tc) as ws:
        signed_in(ws, member)
        subscribe(ws, topic)

        assert tc.delete(f"/workspaces/{owner['X-Workspace-Id']}/members/{member_id}", headers=owner).status_code == 200

        assert ws.receive_json() == {"type": "unsubscribed", "topic": topic, "reason": "forbidden"}


async def test_a_token_left_to_expire_closes_the_socket(client, app):
    owner, *_ = await workspace_of_three(client)
    short = security.issue_access_token(security.verify_access_token(token_of(owner)), ttl=timedelta(seconds=1))
    with TestClient(app) as tc, connect(tc) as ws:
        ws.send_json({"type": "auth", "token": short})
        assert ws.receive_json()["type"] == "ready"
        assert closed_with(ws) == 4401


async def test_a_flood_of_frames_closes_the_socket(client, app):
    owner, *_ = await workspace_of_three(client)
    with TestClient(app) as tc, connect(tc) as ws:
        signed_in(ws, owner)
        for n in range(realtime.FRAME_BURST + 5):
            ws.send_json({"type": "ping", "ref": str(n)})
        replies = []
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                replies.append(ws.receive_json())
        assert closed.value.code == 4429
        assert len(replies) <= realtime.FRAME_BURST
