"""The in-memory event log behind the UI's Logs page, plus file logging.

Everything that happens gets one line: serial traffic in both directions, MQTT
in and out, accepted and rejected fixes, and every action that moves hardware.
The spec asks for that last one specifically, and it is the thing that makes a
remote clock debuggable -- when a hand is 40 degrees out, the first question is
always "what was the last thing that moved it, and who asked".

The log is a ring buffer so it cannot grow without bound on a Pi that runs for
months; a rotating file on disk keeps the history that outlives the buffer.
"""

from __future__ import annotations

import itertools
import logging
import logging.handlers
import time
from collections import deque
from pathlib import Path
from typing import Any

#: Categories, used as the Logs page's filter chips.
CAT_SERIAL_TX = "serial-tx"
CAT_SERIAL_RX = "serial-rx"
CAT_MQTT_IN = "mqtt-in"
CAT_MQTT_OUT = "mqtt-out"
CAT_FIX = "fix"
CAT_ACTION = "action"
CAT_SYSTEM = "system"
CAT_ERROR = "error"

CATEGORIES = (
    CAT_SERIAL_TX,
    CAT_SERIAL_RX,
    CAT_MQTT_IN,
    CAT_MQTT_OUT,
    CAT_FIX,
    CAT_ACTION,
    CAT_SYSTEM,
    CAT_ERROR,
)


class Event:
    """One logged line."""

    __slots__ = ("id", "at", "category", "message", "data")

    def __init__(self, id_: int, category: str, message: str, data: dict | None = None):
        self.id = id_
        self.at = time.time()
        self.category = category
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict for the UI and the diagnostic bundle."""
        return {
            "id": self.id,
            "at": self.at,
            "category": self.category,
            "message": self.message,
            "data": self.data,
        }


class EventLog:
    """A bounded, filterable log with optional file output.

    Args:
        capacity: How many events to keep in memory.
        path: Optional file to append to, rotated at ``max_bytes``.
        max_bytes: Rotation threshold.
        backups: How many rotated files to keep.
    """

    def __init__(
        self,
        capacity: int = 2000,
        path: Path | None = None,
        *,
        max_bytes: int = 2_000_000,
        backups: int = 3,
    ):
        self.events: deque[Event] = deque(maxlen=capacity)
        self._ids = itertools.count(1)
        self._logger: logging.Logger | None = None
        self.path = path
        self.dropped_by_rotation = 0
        if path is not None:
            self._logger = logging.getLogger("clockd.events")
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False
            if not self._logger.handlers:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    str(path), maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S")
                )
                self._logger.addHandler(handler)

    def add(self, category: str, message: str, **data: Any) -> Event:
        """Record one event.

        Args:
            category: One of :data:`CATEGORIES`.
            message: The human-readable line.
            **data: Structured extras, shown when a row is expanded.

        Returns:
            The :class:`Event` that was stored.
        """
        event = Event(next(self._ids), category, message, data)
        self.events.append(event)
        if self._logger is not None:
            self._logger.info("[%s] %s", category, message)
        return event

    # Convenience wrappers, so call sites read as what they are.

    def action(self, message: str, **data: Any) -> Event:
        """Record something a person asked for that moves hardware."""
        return self.add(CAT_ACTION, message, **data)

    def system(self, message: str, **data: Any) -> Event:
        """Record a controller lifecycle event."""
        return self.add(CAT_SYSTEM, message, **data)

    def error(self, message: str, **data: Any) -> Event:
        """Record something that went wrong."""
        return self.add(CAT_ERROR, message, **data)

    def fix(self, message: str, **data: Any) -> Event:
        """Record an accepted or rejected location fix."""
        return self.add(CAT_FIX, message, **data)

    def serial_tx(self, message: str, **data: Any) -> Event:
        """Record a line sent to the Pico."""
        return self.add(CAT_SERIAL_TX, message, **data)

    def serial_rx(self, message: str, **data: Any) -> Event:
        """Record a line received from the Pico."""
        return self.add(CAT_SERIAL_RX, message, **data)

    def mqtt_in(self, message: str, **data: Any) -> Event:
        """Record an inbound MQTT message."""
        return self.add(CAT_MQTT_IN, message, **data)

    def mqtt_out(self, message: str, **data: Any) -> Event:
        """Record an outbound MQTT message."""
        return self.add(CAT_MQTT_OUT, message, **data)

    def tail(
        self,
        limit: int = 200,
        categories: list[str] | None = None,
        since_id: int | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most recent matching events, oldest first.

        Args:
            limit: Maximum number to return.
            categories: Keep only these categories; ``None`` means all.
            since_id: Only events with a greater id, for incremental polling.
            search: Case-insensitive substring of the message.
        """
        wanted = set(categories) if categories else None
        needle = search.lower() if search else None
        out: list[dict[str, Any]] = []
        for event in reversed(self.events):
            if since_id is not None and event.id <= since_id:
                break
            if wanted is not None and event.category not in wanted:
                continue
            if needle is not None and needle not in event.message.lower():
                continue
            out.append(event.to_dict())
            if len(out) >= limit:
                break
        out.reverse()
        return out

    def last_id(self) -> int:
        """The id of the newest event, or 0 when empty."""
        return self.events[-1].id if self.events else 0

    def counts(self) -> dict[str, int]:
        """How many events of each category are in the buffer."""
        totals = dict.fromkeys(CATEGORIES, 0)
        for event in self.events:
            totals[event.category] = totals.get(event.category, 0) + 1
        return totals

    def as_text(self) -> str:
        """The whole buffer as plain text, for the diagnostic bundle."""
        lines = []
        for event in self.events:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(event.at))
            lines.append("%s [%s] %s" % (stamp, event.category, event.message))
        return "\n".join(lines)
