"""MQTT, with an in-memory stand-in so ``--sim`` needs no broker.

The controller only ever uses :class:`Bus`.  On the Pi that is :class:`MqttBus`
talking to the Mosquitto already running on ``localhost:1883``; on a laptop in
``--sim`` mode it is :class:`MemoryBus`, which implements the same publish,
subscribe, retain and wildcard semantics in a dictionary.  The integration tests
run against both, which is what keeps the stand-in honest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

#: A handler takes ``(topic, payload_bytes)`` and may be sync or async.
Handler = Callable[[str, bytes], Awaitable[None] | None]


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT topic filter matching, including ``+`` and ``#``.

    Args:
        filter_: A subscription filter such as ``owntracks/+/+``.
        topic: A concrete topic such as ``owntracks/becky/iphone``.

    Returns:
        True when the topic matches.
    """
    if filter_ == "#":
        return not topic.startswith("$")
    filter_parts = filter_.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            # '#' must be last and matches the rest, but not a leading $topic.
            if index == 0 and topic_parts and topic_parts[0].startswith("$"):
                return False
            return index <= len(topic_parts)
        if index >= len(topic_parts):
            return False
        if part == "+":
            if index == 0 and topic_parts[0].startswith("$"):
                return False
            continue
        if part != topic_parts[index]:
            return False
    return len(filter_parts) == len(topic_parts)


class Message:
    """One published message, as the UI log and the tests see it."""

    __slots__ = ("topic", "payload", "retain", "qos", "at")

    def __init__(self, topic: str, payload: bytes, retain: bool = False, qos: int = 0):
        self.topic = topic
        self.payload = payload
        self.retain = retain
        self.qos = qos
        self.at = time.time()

    def text(self) -> str:
        """Payload decoded as UTF-8, with replacement for bad bytes."""
        return self.payload.decode("utf-8", "replace")

    def json(self) -> Any:
        """Payload parsed as JSON, or ``None`` if it is not JSON."""
        try:
            return json.loads(self.text())
        except ValueError:
            return None

    def __repr__(self) -> str:
        return "Message(%r, %r, retain=%r)" % (self.topic, self.text()[:60], self.retain)


class Bus:
    """Interface shared by the real broker client and the in-memory one."""

    #: True once the transport is usable.
    connected: bool = False

    async def start(self) -> None:
        """Connect (or set up) the bus."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Disconnect and release resources."""
        raise NotImplementedError

    async def publish(
        self, topic: str, payload: Any, *, retain: bool = False, qos: int = 0
    ) -> None:
        """Publish a message.

        ``payload`` may be bytes, a string, or any JSON-serialisable object, in
        which case it is encoded as compact JSON.
        """
        raise NotImplementedError

    async def subscribe(self, filter_: str, handler: Handler) -> None:
        """Subscribe to a topic filter and route matches to ``handler``."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        """A short dict for the UI's system banner."""
        raise NotImplementedError


def encode_payload(payload: Any) -> bytes:
    """Turn a payload of any accepted type into bytes."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")


class MemoryBus(Bus):
    """A broker in a dictionary: same semantics, no network.

    Supports retained messages (a new subscriber immediately receives the
    retained message on any matching topic, exactly as MQTT does), wildcards,
    and a Last Will that :meth:`stop` delivers.  Published messages are also
    kept in :attr:`history` so the UI's Logs page and the tests can read them.
    """

    def __init__(self, *, history_limit: int = 2000):
        self.retained: dict[str, Message] = {}
        self.subscriptions: list[tuple[str, Handler]] = []
        self.history: list[Message] = []
        self.history_limit = history_limit
        self.will: tuple[str, bytes, bool] | None = None
        self.connected = False
        self.publish_count = 0

    async def start(self) -> None:
        """Mark the bus usable.  Nothing to connect to."""
        self.connected = True

    async def stop(self) -> None:
        """Deliver the Last Will, then stop."""
        if self.will is not None:
            topic, payload, retain = self.will
            self.connected = True  # the will is delivered by the "broker"
            await self.publish(topic, payload, retain=retain)
        self.connected = False

    def set_will(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        """Register the Last Will, delivered when the bus stops."""
        self.will = (topic, encode_payload(payload), retain)

    async def publish(
        self, topic: str, payload: Any, *, retain: bool = False, qos: int = 0
    ) -> None:
        """Publish to every matching subscriber, and retain if asked."""
        message = Message(topic, encode_payload(payload), retain, qos)
        self.publish_count += 1
        self.history.append(message)
        if len(self.history) > self.history_limit:
            del self.history[: len(self.history) - self.history_limit]
        if retain:
            if message.payload:
                self.retained[topic] = message
            else:
                # An empty retained payload clears the retained message.
                self.retained.pop(topic, None)
        for filter_, handler in list(self.subscriptions):
            if topic_matches(filter_, topic):
                await _call(handler, topic, message.payload)

    async def subscribe(self, filter_: str, handler: Handler) -> None:
        """Subscribe, then replay matching retained messages like a broker does."""
        self.subscriptions.append((filter_, handler))
        for topic, message in list(self.retained.items()):
            if topic_matches(filter_, topic):
                await _call(handler, topic, message.payload)

    def describe(self) -> dict[str, Any]:
        """Describe the bus for the UI."""
        return {
            "kind": "memory",
            "connected": self.connected,
            "host": "in-process",
            "published": self.publish_count,
            "retained": len(self.retained),
            "subscriptions": [f for f, _ in self.subscriptions],
        }


async def _call(handler: Handler, topic: str, payload: bytes) -> None:
    """Invoke a handler that may be sync or async, never letting it escape."""
    try:
        result = handler(topic, payload)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        log.exception("MQTT handler for %s failed", topic)


class MqttBus(Bus):
    """A real broker, via paho-mqtt running its own network thread.

    paho's callbacks arrive on that thread, so everything is marshalled back
    onto the asyncio loop with ``call_soon_threadsafe`` before any controller
    code runs.  Reconnection is paho's automatic one; :attr:`connected` tracks
    it so the UI can show the broker as down without the controller stopping.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        *,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "clockd",
        keepalive_s: int = 30,
        tls: bool = False,
        history_limit: int = 2000,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.keepalive_s = keepalive_s
        self.tls = tls
        self.history: list[Message] = []
        self.history_limit = history_limit
        self.subscriptions: list[tuple[str, Handler]] = []
        self.connected = False
        self.publish_count = 0
        self.last_error: str | None = None
        self.connect_count = 0
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._will: tuple[str, bytes, bool] | None = None

    def set_will(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        """Register a Last Will.  Must be called before :meth:`start`."""
        self._will = (topic, encode_payload(payload), retain)

    async def start(self) -> None:
        """Connect to the broker and start paho's network loop."""
        import paho.mqtt.client as mqtt

        self._loop = asyncio.get_running_loop()
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=True
            )
        except AttributeError:  # pragma: no cover - paho 1.x
            client = mqtt.Client(client_id=self.client_id, clean_session=True)
        if self.username:
            client.username_pw_set(self.username, self.password or None)
        if self.tls:
            client.tls_set()
        if self._will is not None:
            topic, payload, retain = self._will
            client.will_set(topic, payload, qos=1, retain=retain)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client = client
        try:
            client.connect_async(self.host, self.port, keepalive=self.keepalive_s)
            client.loop_start()
        except Exception as exc:
            self.last_error = str(exc)
            raise

    async def stop(self) -> None:
        """Disconnect cleanly, which suppresses the Last Will."""
        client = self._client
        self._client = None
        self.connected = False
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:  # pragma: no cover
            pass

    async def publish(
        self, topic: str, payload: Any, *, retain: bool = False, qos: int = 0
    ) -> None:
        """Publish through paho.  Silently queues when the broker is down."""
        raw = encode_payload(payload)
        message = Message(topic, raw, retain, qos)
        self.history.append(message)
        if len(self.history) > self.history_limit:
            del self.history[: len(self.history) - self.history_limit]
        self.publish_count += 1
        if self._client is None:
            return
        self._client.publish(topic, raw, qos=qos, retain=retain)

    async def subscribe(self, filter_: str, handler: Handler) -> None:
        """Subscribe now, and again automatically after every reconnect."""
        self.subscriptions.append((filter_, handler))
        if self._client is not None and self.connected:
            self._client.subscribe(filter_, qos=1)

    # -- paho callbacks (network thread) ---------------------------------

    def _on_connect(self, _client, _userdata, _flags, reason_code, *_args) -> None:
        code = getattr(reason_code, "value", reason_code)
        if code not in (0, None):
            self.last_error = "connect refused: %s" % reason_code
            log.warning("MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        self.connect_count += 1
        self.last_error = None
        log.info("MQTT connected to %s:%s", self.host, self.port)
        # Re-subscribe: the broker forgets subscriptions on a clean session.
        for filter_, _handler in self.subscriptions:
            self._client.subscribe(filter_, qos=1)

    def _on_disconnect(self, _client, _userdata, *args) -> None:
        self.connected = False
        reason = args[-1] if args else "unknown"
        log.warning("MQTT disconnected (%s); paho will retry", reason)

    def _on_message(self, _client, _userdata, message) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        topic = message.topic
        payload = bytes(message.payload)
        loop.call_soon_threadsafe(self._dispatch, topic, payload)

    def _dispatch(self, topic: str, payload: bytes) -> None:
        for filter_, handler in list(self.subscriptions):
            if topic_matches(filter_, topic):
                asyncio.ensure_future(_call(handler, topic, payload))

    def describe(self) -> dict[str, Any]:
        """Describe the broker connection for the UI."""
        return {
            "kind": "mqtt",
            "connected": self.connected,
            "host": "%s:%d" % (self.host, self.port),
            "published": self.publish_count,
            "subscriptions": [f for f, _ in self.subscriptions],
            "connects": self.connect_count,
            "last_error": self.last_error,
        }
