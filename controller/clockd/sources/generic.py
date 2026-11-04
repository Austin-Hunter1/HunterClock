"""The generic MQTT source: ``clock/location/<person>``.

The simple way in for anything that is not OwnTracks -- a Home Assistant
automation, a cron script, ``mosquitto_pub`` while setting the clock up::

    mosquitto_pub -h localhost -u clock -P "$MQTT_PASSWORD" \\
        -t clock/location/austin \\
        -m '{"lat":40.0150,"lon":-105.2705,"acc":30,"ts":1762204800}'

Fields: ``lat`` and ``lon`` are required; ``ts`` (unix seconds) defaults to now;
``acc`` is metres; ``batt`` is a percentage.  ``distance_km`` is also accepted
instead of a position, for a sender that knows only a distance -- which is how
the UI's "fake distance" override reaches the same code path.
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

FixCallback = Callable[[str, Fix], Awaitable[None] | None]


def parse_generic(
    topic: str, payload: bytes | str, config: ClockConfig, *, now: float | None = None
) -> ParseResult:
    """Parse one ``clock/location/<person>`` message.

    Args:
        topic: The full topic; the last segment names the person.
        payload: Raw payload bytes.
        config: Used to look the person up and to read the topic prefix.
        now: Current unix time, used when ``ts`` is absent.

    Returns:
        A :class:`ParseResult`.
    """
    now = now if now is not None else time.time()
    prefix = config.sources.generic_mqtt.topic_prefix.strip("/")
    prefix_parts = prefix.split("/") if prefix else []
    parts = topic.split("/")
    if len(parts) != len(prefix_parts) + 1 or parts[:-1] != prefix_parts:
        return ParseResult(ignored=True)

    slug = parts[-1]
    try:
        person = config.person(slug)
    except KeyError:
        return ParseResult(error="no person named %r (topic %s)" % (slug, topic))

    data, error = decode_json(payload)
    if error is not None:
        return ParseResult(person=person, error="%s on %s" % (error, topic))
    if not isinstance(data, dict):
        return ParseResult(person=person, error="payload on %s is not a JSON object" % topic)

    timestamp = number(data.get("ts")) or number(data.get("tst"))
    accuracy = number(data.get("acc"))
    if accuracy is None:
        accuracy = number(data.get("accuracy_m"))
    battery = number(data.get("batt"))
    if battery is None:
        battery = number(data.get("battery"))

    lat = number(data.get("lat"))
    lon = number(data.get("lon"))
    distance = number(data.get("distance_km"))

    if lat is None or lon is None:
        if distance is None:
            return ParseResult(
                person=person, error="payload on %s has neither lat/lon nor distance_km" % topic
            )
        # A distance-only report: keep home's coordinates so the fix is still a
        # well-formed position, and carry the distance explicitly.
        return ParseResult(
            person=person,
            fix=Fix(
                lat=config.home.lat,
                lon=config.home.lon,
                source="generic_mqtt",
                ts=timestamp if timestamp is not None else now,
                received=now,
                accuracy_m=accuracy,
                battery=battery,
                topic=topic,
                distance_km=max(0.0, distance),
            ),
        )

    return ParseResult(
        person=person,
        fix=Fix(
            lat=lat,
            lon=lon,
            source="generic_mqtt",
            ts=timestamp if timestamp is not None else now,
            received=now,
            accuracy_m=accuracy,
            battery=battery,
            topic=topic,
        ),
    )


class GenericMqttSource:
    """Subscribes to ``<prefix>/+`` and feeds fixes to the engine."""

    name = "generic_mqtt"

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
        return self.config.sources.generic_mqtt.enabled

    @property
    def topic_filter(self) -> str:
        """The MQTT filter this source subscribes to."""
        return "%s/+" % self.config.sources.generic_mqtt.topic_prefix.strip("/")

    async def start(self, bus: Bus) -> None:
        """Subscribe, unless disabled."""
        if not self.enabled:
            log.info("generic_mqtt source disabled")
            return
        await bus.subscribe(self.topic_filter, self.handle)
        log.info("generic_mqtt source subscribed to %s", self.topic_filter)

    async def handle(self, topic: str, payload: bytes) -> None:
        """MQTT handler: parse and forward."""
        self.messages += 1
        if not self.enabled:
            return
        result = parse_generic(topic, payload, self.config)
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
