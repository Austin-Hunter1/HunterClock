"""The MQTT abstraction: topic matching, retain, wildcards and the Last Will.

The in-memory bus stands in for Mosquitto in ``--sim``, so it has to behave
like a broker, not merely like a dictionary.
"""

from __future__ import annotations

import json

import pytest

from clockd.bus import MemoryBus, encode_payload, topic_matches


@pytest.mark.parametrize(
    ("filter_", "topic", "expected"),
    [
        ("a/b", "a/b", True),
        ("a/b", "a/c", False),
        ("a/b", "a/b/c", False),
        ("a/+", "a/b", True),
        ("a/+", "a/b/c", False),
        ("+/b", "a/b", True),
        ("a/+/c", "a/b/c", True),
        ("a/#", "a", True),
        ("a/#", "a/b", True),
        ("a/#", "a/b/c/d", True),
        ("#", "a/b", True),
        ("#", "a", True),
        ("owntracks/+/+", "owntracks/becky/iphone", True),
        ("owntracks/+/+", "owntracks/becky", False),
        ("owntracks/+/+", "owntracks/becky/iphone/cmd", False),
        ("clock/location/+", "clock/location/austin", True),
        ("clock/state/+", "clock/display/austin", False),
        # $-prefixed system topics are not matched by bare wildcards.
        ("#", "$SYS/broker/uptime", False),
        ("+/broker", "$SYS/broker", False),
    ],
)
def test_topic_matching(filter_, topic, expected):
    assert topic_matches(filter_, topic) is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"raw", b"raw"),
        ("text", b"text"),
        ({"a": 1}, b'{"a":1}'),
        ([1, 2], b"[1,2]"),
        (bytearray(b"ba"), b"ba"),
    ],
)
def test_payload_encoding(payload, expected):
    assert encode_payload(payload) == expected


async def test_publish_reaches_matching_subscribers_only():
    bus = MemoryBus()
    await bus.start()
    hits, misses = [], []
    await bus.subscribe("clock/state/+", lambda t, p: hits.append(t))
    await bus.subscribe("other/#", lambda t, p: misses.append(t))
    await bus.publish("clock/state/evan", {"a": 1})
    assert hits == ["clock/state/evan"]
    assert misses == []


async def test_a_retained_message_is_replayed_to_a_new_subscriber():
    """This is how a display node that boots late learns its person's state."""
    bus = MemoryBus()
    await bus.start()
    await bus.publish("clock/display/evan", {"name": "Evan"}, retain=True)
    received = []
    await bus.subscribe("clock/display/+", lambda t, p: received.append((t, p)))
    assert received == [("clock/display/evan", b'{"name":"Evan"}')]


async def test_a_non_retained_message_is_not_replayed():
    bus = MemoryBus()
    await bus.start()
    await bus.publish("clock/system/heartbeat", {"at": 1})
    received = []
    await bus.subscribe("clock/#", lambda t, p: received.append(t))
    assert received == []


async def test_an_empty_retained_payload_clears_the_retained_message():
    bus = MemoryBus()
    await bus.start()
    await bus.publish("clock/state/evan", {"a": 1}, retain=True)
    await bus.publish("clock/state/evan", b"", retain=True)
    assert "clock/state/evan" not in bus.retained


async def test_the_last_will_is_delivered_on_stop():
    bus = MemoryBus()
    await bus.start()
    bus.set_will("clock/system/status", {"online": False}, retain=True)
    await bus.stop()
    assert json.loads(bus.retained["clock/system/status"].text()) == {"online": False}
    assert not bus.connected


async def test_a_failing_handler_does_not_break_the_bus():
    """One bad subscriber must not stop the rest of the clock working."""
    bus = MemoryBus()
    await bus.start()
    good = []

    def explodes(topic, payload):
        raise RuntimeError("boom")

    await bus.subscribe("#", explodes)
    await bus.subscribe("#", lambda t, p: good.append(t))
    await bus.publish("clock/x", b"1")
    assert good == ["clock/x"]


async def test_async_handlers_are_awaited():
    bus = MemoryBus()
    await bus.start()
    seen = []

    async def handler(topic, payload):
        seen.append(topic)

    await bus.subscribe("#", handler)
    await bus.publish("a/b", b"1")
    assert seen == ["a/b"]


async def test_history_is_bounded():
    bus = MemoryBus(history_limit=10)
    await bus.start()
    for index in range(50):
        await bus.publish("clock/x", str(index))
    assert len(bus.history) == 10
    assert bus.history[-1].text() == "49"
    assert bus.publish_count == 50


async def test_message_json_returns_none_for_non_json():
    bus = MemoryBus()
    await bus.start()
    await bus.publish("a", b"not json")
    assert bus.history[-1].json() is None


async def test_describe_reports_what_the_ui_shows():
    bus = MemoryBus()
    await bus.start()
    await bus.subscribe("clock/#", lambda t, p: None)
    await bus.publish("clock/a", b"1", retain=True)
    described = bus.describe()
    assert described["kind"] == "memory"
    assert described["connected"] is True
    assert described["retained"] == 1
    assert "clock/#" in described["subscriptions"]
