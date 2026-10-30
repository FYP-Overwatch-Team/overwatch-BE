"""The realtime hub: who gets which event, and what happens to a client that can't keep up."""

import json

from app.realtime.events import CloseCode, EventName, UnsubscribeReason
from app.realtime.hub import OUTBOX_SIZE, CloseRequest, Connection, Hub


def connect(hub: Hub, user_id: str, *topics: tuple[str, str]) -> Connection:
    connection = Connection(websocket=None, user={"id": user_id, "name": user_id})
    hub.add(connection)
    for topic, workspace_id in topics:
        hub.subscribe(connection, topic, workspace_id)
    return connection


def sent(connection: Connection) -> list:
    items = []
    while not connection.outbox.empty():
        item = connection.outbox.get_nowait()
        items.append(item if isinstance(item, CloseRequest) else json.loads(item))
    return items


def test_an_event_reaches_the_topics_subscribers_but_not_the_excluded_user():
    hub = Hub()
    sara, ali, other = connect(hub, "sara", ("decision:d1", "w1")), connect(hub, "ali", ("decision:d1", "w1")), connect(hub, "zed")

    hub.publish("decision:d1", EventName.DECISION_TYPING_STARTED, {"user": {"id": "sara"}}, exclude_user="sara")

    [received] = sent(ali)
    assert (received["type"], received["event"], received["topic"]) == ("event", "decision.typing.started", "decision:d1")
    assert sent(sara) == [] and sent(other) == []


def test_a_client_that_cant_keep_up_is_closed_rather_than_skipped():
    hub = Hub()
    slow = connect(hub, "sara", ("decision:d1", "w1"))

    for _ in range(OUTBOX_SIZE + 1):
        hub.publish("decision:d1", EventName.DECISION_COMMENT_DELETED, {"id": "m"})

    # what was queued is dropped: it refetches on reconnect
    assert sent(slow) == [CloseRequest(CloseCode.SLOW_CONSUMER)]


def test_losing_a_workspace_ends_only_its_topics():
    hub = Hub()
    sara = connect(hub, "sara", ("decision:d1", "w1"), ("decision:d2", "w2"))

    hub.revoke("w1", "sara")
    hub.publish("decision:d1", EventName.DECISION_COMMENT_DELETED, {"id": "m"})

    assert sent(sara) == [{"type": "unsubscribed", "topic": "decision:d1", "reason": UnsubscribeReason.FORBIDDEN}]
    assert list(sara.topics) == ["decision:d2"]


def test_a_deleted_resource_unsubscribes_everyone_and_a_gone_connection_leaves_no_trace():
    hub = Hub()
    sara, ali = connect(hub, "sara", ("decision:d1", "w1")), connect(hub, "ali", ("decision:d1", "w1"))

    hub.close_topic("decision:d1", UnsubscribeReason.GONE)
    hub.remove(sara)

    assert sent(ali) == [{"type": "unsubscribed", "topic": "decision:d1", "reason": "gone"}]
    assert (hub.connections_of("sara"), hub.connections_of("ali")) == (0, 1)
