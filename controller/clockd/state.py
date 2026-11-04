"""Location fixes, per-person state and the state machine.

A :class:`Fix` is one position report from one source.  A :class:`PersonState`
is everything the controller knows about one person and their hand, and is also
exactly what the UI renders and what goes out on MQTT -- there is no second
representation to keep in step.

The status values are the ones in the spec: ``ok``, ``stale``, ``override``,
``unverified``, ``fault``, ``disabled``.  Several can be true at once (a hand can
be unverified *and* stale), so the booleans are all kept and :meth:`PersonState.status`
reduces them to one headline value by a fixed precedence:

1. ``disabled`` -- switched off in config; nothing else matters.
2. ``fault`` -- the hand cannot be driven (no Pico link, or the firmware
   reported a fault). Shown first because nothing the clock says is being acted
   on.
3. ``unverified`` -- nobody has confirmed where this hand physically points, so
   the angle is a guess. Ranked above ``override`` and ``stale`` because it is
   the one condition that needs a person at the clock to clear.
4. ``override`` -- a manual fake location or distance is in force.
5. ``stale`` -- no fix for longer than ``filtering.stale_after_s``; the hand
   holds its last position.
6. ``ok``.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Source names, in the order of the spec.  ``startup`` is the config fallback
#: position, used before any real fix has ever arrived.
SOURCES = ("manual", "owntracks", "generic_mqtt", "hass", "startup")

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_OVERRIDE = "override"
STATUS_UNVERIFIED = "unverified"
STATUS_FAULT = "fault"
STATUS_DISABLED = "disabled"

#: Every status the state machine can report.
STATUSES = (
    STATUS_OK,
    STATUS_STALE,
    STATUS_OVERRIDE,
    STATUS_UNVERIFIED,
    STATUS_FAULT,
    STATUS_DISABLED,
)

#: What the display nodes understand; a deliberately smaller vocabulary than the
#: controller's, because a 240x240 round screen has room for one word.
DISPLAY_HOME = "home"
DISPLAY_AWAY = "away"
DISPLAY_STALE = "stale"
DISPLAY_UNKNOWN = "unknown"

#: Distance under which a person counts as "home" on the displays, in km.
HOME_RADIUS_KM = 1.0


def iso(timestamp: float | None) -> str | None:
    """Format a unix timestamp as a UTC ISO-8601 string, or ``None``."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


@dataclass
class Fix:
    """One position report.

    Attributes:
        lat: Latitude in degrees.
        lon: Longitude in degrees.
        source: Which source produced it; one of :data:`SOURCES`.
        ts: The time the *phone* says the fix was taken, unix seconds.
        received: When the controller received it, unix seconds.
        accuracy_m: Reported horizontal accuracy in metres, if known.
        battery: Battery percentage, if the source reports it.
        topic: The MQTT topic or URL it arrived on, for the logs.
        distance_km: Pre-computed distance, used by a fake-distance override
            where there is no real position at all.
    """

    lat: float
    lon: float
    source: str = "generic_mqtt"
    ts: float = field(default_factory=time.time)
    received: float = field(default_factory=time.time)
    accuracy_m: float | None = None
    battery: float | None = None
    topic: str | None = None
    distance_km: float | None = None

    def age_s(self, now: float | None = None) -> float:
        """How old the fix's own timestamp is, in seconds."""
        return max(0.0, (now if now is not None else time.time()) - self.ts)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict, as stored and as shown in the UI."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fix:
        """Rebuild a fix from :meth:`to_dict` output, ignoring unknown keys."""
        known = {
            "lat",
            "lon",
            "source",
            "ts",
            "received",
            "accuracy_m",
            "battery",
            "topic",
            "distance_km",
        }
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Override:
    """A manual fake position or distance set from the UI.

    Exactly one of ``lat``/``lon`` or ``distance_km`` is meaningful; the UI's
    Simulate page offers both because "put Evan 500 km away" is a quicker way to
    check a hand than looking up coordinates.
    """

    expires_at: float
    lat: float | None = None
    lon: float | None = None
    distance_km: float | None = None
    note: str = ""
    set_at: float = field(default_factory=time.time)

    def active(self, now: float | None = None) -> bool:
        """Is the override still in force?"""
        return (now if now is not None else time.time()) < self.expires_at

    def seconds_left(self, now: float | None = None) -> float:
        """Seconds until it expires, floored at zero."""
        return max(0.0, self.expires_at - (now if now is not None else time.time()))

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict for the UI."""
        data = asdict(self)
        data["seconds_left"] = round(self.seconds_left(), 1)
        return data


@dataclass
class PersonState:
    """Everything known about one person and their hand.

    ``target_deg`` is where the hand should point, ``actual_deg`` where the
    firmware says it is; they differ while a hand is travelling, which is what
    the UI draws as a ghost hand.  Both are wrapped to ``[0, 360)`` for display,
    while the step positions behind them are unwrapped.
    """

    name: str
    slug: str
    level: int
    channel: int
    color: str = "#888888"
    enabled: bool = True

    distance_km: float | None = None
    target_deg: float | None = None
    actual_deg: float | None = None
    target_steps: int | None = None
    actual_steps: int | None = None

    source: str | None = None
    last_fix: Fix | None = None
    last_seen: float | None = None
    accuracy_m: float | None = None
    battery: float | None = None

    verified: bool = False
    verified_at: float | None = None
    zero_offset_deg: float = 0.0
    override: Override | None = None

    moving: bool = False
    fault: str | None = None
    link_down: bool = False
    last_move_at: float | None = None
    rejected: int = 0
    last_reject_reason: str | None = None
    accepted: int = 0

    # -- derived ---------------------------------------------------------

    def is_stale(self, stale_after_s: float, now: float | None = None) -> bool:
        """True when the last fix is older than the stale timeout.

        A person with no fix at all is *not* stale -- they are unknown, which
        the status machine reports through ``last_seen`` being ``None``.
        """
        if self.last_seen is None:
            return False
        now = now if now is not None else time.time()
        return (now - self.last_seen) > stale_after_s

    def status(self, stale_after_s: float, now: float | None = None) -> str:
        """The single headline status, by the precedence in the module docstring."""
        if not self.enabled:
            return STATUS_DISABLED
        if self.fault or self.link_down:
            return STATUS_FAULT
        if not self.verified:
            return STATUS_UNVERIFIED
        if self.override is not None and self.override.active(now):
            return STATUS_OVERRIDE
        if self.is_stale(stale_after_s, now):
            return STATUS_STALE
        return STATUS_OK

    def display_status(self, stale_after_s: float, now: float | None = None) -> str:
        """The coarse status the ESP32 screens show.

        Deliberately not the same vocabulary: a screen beside a hand should say
        HOME, a distance, or that it does not know -- "unverified" is the
        owner's problem, not something to put on a portrait.
        """
        if not self.enabled or self.last_seen is None or self.distance_km is None:
            return DISPLAY_UNKNOWN
        if self.is_stale(stale_after_s, now):
            return DISPLAY_STALE
        if self.distance_km <= HOME_RADIUS_KM:
            return DISPLAY_HOME
        return DISPLAY_AWAY

    def display_text(self, stale_after_s: float, now: float | None = None) -> str:
        """The one line of text under the name on a display node."""
        state = self.display_status(stale_after_s, now)
        if state == DISPLAY_UNKNOWN:
            return "no location yet"
        if state == DISPLAY_HOME:
            return "HOME"
        distance = self.distance_km or 0.0
        if distance < 10:
            return f"{distance:.1f} km away"
        return f"{round(distance)} km away"

    def seconds_since_seen(self, now: float | None = None) -> float | None:
        """Age of the last fix in seconds, or ``None`` if never seen."""
        if self.last_seen is None:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.last_seen)

    # -- serialisation ---------------------------------------------------

    def to_state_payload(self, stale_after_s: float, now: float | None = None) -> dict[str, Any]:
        """The retained ``clock/state/<person>`` payload."""
        return {
            "name": self.name,
            "distance_km": round(self.distance_km, 3) if self.distance_km is not None else None,
            "target_deg": round(self.target_deg, 2) if self.target_deg is not None else None,
            "actual_deg": round(self.actual_deg, 2) if self.actual_deg is not None else None,
            "status": self.status(stale_after_s, now),
            "last_seen": iso(self.last_seen),
            "source": self.source,
            "accuracy_m": self.accuracy_m,
            "verified": self.verified,
            "moving": self.moving,
            "level": self.level,
        }

    def to_display_payload(self, stale_after_s: float, now: float | None = None) -> dict[str, Any]:
        """The retained ``clock/display/<person>`` payload for the ESP32 nodes."""
        from . import geo

        distance = self.distance_km
        return {
            "name": self.name,
            "status": self.display_status(stale_after_s, now),
            "distance_km": round(distance, 1) if distance is not None else None,
            "fraction": round(geo.distance_fraction(distance), 4)
            if distance is not None
            else None,
            "updated_iso": iso(self.last_seen),
            "text": self.display_text(stale_after_s, now),
            "color": self.color,
        }

    def to_ui(self, stale_after_s: float, now: float | None = None) -> dict[str, Any]:
        """Everything the web UI needs about this person, in one dict."""
        now = now if now is not None else time.time()
        return {
            "name": self.name,
            "slug": self.slug,
            "level": self.level,
            "channel": self.channel,
            "color": self.color,
            "enabled": self.enabled,
            "status": self.status(stale_after_s, now),
            "display_status": self.display_status(stale_after_s, now),
            "distance_km": self.distance_km,
            "target_deg": self.target_deg,
            "actual_deg": self.actual_deg,
            "target_steps": self.target_steps,
            "actual_steps": self.actual_steps,
            "source": self.source,
            "last_seen": self.last_seen,
            "last_seen_iso": iso(self.last_seen),
            "age_s": self.seconds_since_seen(now),
            "accuracy_m": self.accuracy_m,
            "battery": self.battery,
            "verified": self.verified,
            "verified_at_iso": iso(self.verified_at),
            "zero_offset_deg": self.zero_offset_deg,
            "override": self.override.to_dict()
            if self.override is not None and self.override.active(now)
            else None,
            "moving": self.moving,
            "fault": self.fault,
            "stale": self.is_stale(stale_after_s, now),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "last_reject_reason": self.last_reject_reason,
        }
