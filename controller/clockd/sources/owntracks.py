"""OwnTracks over MQTT.

OwnTracks publishes to ``owntracks/<user>/<device>``.  The payload we care about
is ``_type: "location"``::

    {"_type":"location","lat":44.9109,"lon":-93.5017,"acc":12,
     "tst":1762204800,"batt":82,"tid":"be","vel":0,"alt":280}

Everything else on that topic tree -- ``lwt`` (last will), ``transition``
(geofence crossings), ``waypoints``, ``card`` (the user's avatar), ``beacon`` --
is ignored silently.  ``transition`` in particular carries lat/lon and would be
tempting to use, but it fires on geofence entry/exit rather than as a position
update, so taking it would make a hand jump on a stale coordinate.

``tst`` is unix seconds and is the phone's own idea of when the fix was taken,
which is what the staleness and out-of-order filters need; the time the message
reached us is kept separately.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from ..bus import Bus
from ..config import ClockConfig
from ..state import Fix
from .common import ParseResult, decode_json, number

log = logging.getLogger(__name__)

#: The only ``_type`` we act on.
LOCATION_TYPE = "location"

#: Payload types that are expected and deliberately ignored.
IGNORED_TYPES = (
    "lwt",
    "transition",
    "waypoint",
    "waypoints",
    "card",
    "beacon",
    "cmd",
    "steps",
    "configuration",
    "encrypted",
    "request",
)

#: Called with ``(person_slug, fix)`` for every accepted parse.
FixCallback = Callable[[str, Fix], Awaitable[None] | None]


def split_topic(topic: str, prefix: str) -> tuple[str, str] | None:
    """Pull ``(user, device)`` out of ``<prefix>/<user>/<device>``.

    Returns ``None`` when the topic does not have that shape, which includes the
    deeper topics OwnTracks uses for commands and waypoints.
    """
    parts = topic.split("/")
    prefix_parts = prefix.strip("/").split("/") if prefix.strip("/") else []
    if len(parts) != len(prefix_parts) + 2:
        return None
    if parts[: len(prefix_parts)] != prefix_parts:
        return None
    user, device = parts[-2], parts[-1]
    if not user or not device:
        return None
    return user, device


def parse_owntracks(
    topic: str, payload: bytes | str, config: ClockConfig, *, now: float | None = None
) -> ParseResult:
    """Parse one OwnTracks message into a fix.

    Args:
        topic: The full MQTT topic it arrived on.
        payload: The raw payload bytes.
        config: Used to map user/device to a person and to read the prefix.
        now: Current unix time, used when the payload has no ``tst``.

    Returns:
        A :class:`ParseResult`.  Non-location payloads come back with
        ``ignored=True`` and no error.
    """
    now = now if now is not None else time.time()
    prefix = config.sources.owntracks.topic_prefix

    identity = split_topic(topic, prefix)
    if identity is None:
        return ParseResult(ignored=True)
    user, device = identity

    data, error = decode_json(payload)
    if error is not None:
        return ParseResult(error=f"{error} on {topic}")
    if not isinstance(data, dict):
        return ParseResult(error=f"payload on {topic} is not a JSON object")

    kind = data.get("_type")
    if kind != LOCATION_TYPE:
        if kind in IGNORED_TYPES or kind is None:
            return ParseResult(ignored=True)
        return ParseResult(ignored=True)

    person = config.person_for_owntracks(user, device)
    if person is None:
        return ParseResult(
            error=f"no person configured for owntracks user {user!r} device {device!r}"
        )

    lat = number(data.get("lat"))
    lon = number(data.get("lon"))
    if lat is None or lon is None:
        return ParseResult(person=person, error=f"location payload on {topic} has no lat/lon")

    timestamp = number(data.get("tst"))
    fix = Fix(
        lat=lat,
        lon=lon,
        source="owntracks",
        ts=timestamp if timestamp is not None else now,
        received=now,
        accuracy_m=number(data.get("acc")),
        battery=number(data.get("batt")),
        topic=topic,
    )
    return ParseResult(person=person, fix=fix)


class OwnTracksSource:
    """Subscribes to the OwnTracks topic tree and feeds fixes to the engine.

    Args:
        config: The clock configuration.
        on_fix: Called with ``(person_slug, fix)`` for each accepted parse.
        on_reject: Called with ``(topic, reason)`` when a payload is bad, so the
            UI's Logs page can show why a phone is not getting through.
    """

    name = "owntracks"

    def __init__(
        self,
        config: ClockConfig,
        on_fix: FixCallback,
        on_reject: Callable[[str, str], None] | None = None,
    ):
        self.config = config
        self.on_fix = on_fix
        self.on_reject = on_reject
        self.messages = 0
        self.fixes = 0
        self.ignored = 0
        self.errors = 0

    @property
    def enabled(self) -> bool:
        """Whether this source is switched on in config."""
        return self.config.sources.owntracks.enabled

    @property
    def topic_filter(self) -> str:
        """The MQTT filter this source subscribes to."""
        return "{}/+/+".format(self.config.sources.owntracks.topic_prefix.strip("/"))

    async def start(self, bus: Bus) -> None:
        """Subscribe.  Subscribing even when disabled would waste no bandwidth
        but would hide the fact that it is off, so it really does not subscribe.
        """
        if not self.enabled:
            log.info("owntracks source disabled")
            return
        await bus.subscribe(self.topic_filter, self.handle)
        log.info("owntracks source subscribed to %s", self.topic_filter)

    async def handle(self, topic: str, payload: bytes) -> None:
        """MQTT handler: parse and forward."""
        self.messages += 1
        if not self.enabled:
            return
        result = parse_owntracks(topic, payload, self.config)
        if result.ignored:
            self.ignored += 1
            return
        if result.error is not None:
            self.errors += 1
            if self.on_reject is not None:
                self.on_reject(topic, result.error)
            return
        self.fixes += 1
        assert result.person is not None and result.fix is not None
        outcome = self.on_fix(result.person.slug, result.fix)
        if outcome is not None:
            await outcome

    def describe(self) -> dict:
        """Counters for the UI."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "topic": self.topic_filter,
            "messages": self.messages,
            "fixes": self.fixes,
            "ignored": self.ignored,
            "errors": self.errors,
        }
