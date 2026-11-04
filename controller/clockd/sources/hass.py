"""Home Assistant REST source (optional, P2, lowest priority).

Polls ``GET /api/states/<entity_id>`` with a long-lived access token and reads
the ``latitude``/``longitude`` attributes that ``device_tracker`` entities carry.

The token comes from the environment (``HASS_TOKEN`` by default) and is never
written to the config file or the logs.  ``docs/hardware-plan.html`` notes that
Home Assistant's iCloud/Find My integration is flaky -- Apple rate-limits it and
2FA re-prompts break it every few weeks -- which is exactly why this is the
lowest-priority source and OwnTracks is the one to trust.

A ``device_tracker`` with ``source_type: gps`` reports ``gps_accuracy`` in
metres; one tracking by router or by geofence reports no coordinates at all,
only ``home``/``not_home``, and those are reported as an error rather than
silently turned into a position at home -- a hand pointing at 12 because Home
Assistant said "home" would be indistinguishable from a real fix.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import ClockConfig
from ..state import Fix
from .common import ParseResult, number

log = logging.getLogger(__name__)

FixCallback = Callable[[str, Fix], Awaitable[None] | None]


def parse_hass_state(
    data: Any, config: ClockConfig, entity_id: str, *, now: float | None = None
) -> ParseResult:
    """Turn a Home Assistant state object into a fix.

    Args:
        data: The decoded JSON from ``/api/states/<entity>``.
        config: Used to find which person owns the entity.
        entity_id: The entity that was polled.
        now: Current unix time.

    Returns:
        A :class:`ParseResult`.
    """
    now = now if now is not None else time.time()

    person = None
    for candidate in config.people:
        if candidate.hass_entity and candidate.hass_entity.lower() == entity_id.lower():
            person = candidate
            break
    if person is None:
        return ParseResult(error="no person owns Home Assistant entity %r" % entity_id)

    if not isinstance(data, dict):
        return ParseResult(person=person, error="state for %s is not an object" % entity_id)

    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    lat = number(attributes.get("latitude"))
    lon = number(attributes.get("longitude"))
    if lat is None or lon is None:
        return ParseResult(
            person=person,
            error="%s has no latitude/longitude attributes (state %r); a router- or "
            "geofence-based tracker cannot drive a hand"
            % (entity_id, data.get("state")),
        )

    timestamp = now
    updated = data.get("last_updated") or data.get("last_changed")
    if isinstance(updated, str):
        parsed = _parse_iso(updated)
        if parsed is not None:
            timestamp = parsed

    return ParseResult(
        person=person,
        fix=Fix(
            lat=lat,
            lon=lon,
            source="hass",
            ts=timestamp,
            received=now,
            accuracy_m=number(attributes.get("gps_accuracy")),
            battery=number(attributes.get("battery_level")),
            topic=entity_id,
        ),
    )


def _parse_iso(text: str) -> float | None:
    """Parse a Home Assistant ISO timestamp into unix seconds."""
    from datetime import datetime

    cleaned = text.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


class HassSource:
    """Polls Home Assistant for each person who has an entity configured."""

    name = "hass"

    def __init__(
        self,
        config: ClockConfig,
        on_fix: FixCallback,
        on_reject: Callable[[str, str], None] | None = None,
        *,
        client_factory: Callable[[], Any] | None = None,
    ):
        self.config = config
        self.on_fix = on_fix
        self.on_reject = on_reject
        self.client_factory = client_factory
        self.polls = 0
        self.fixes = 0
        self.errors = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Whether this source is switched on in config."""
        return self.config.sources.hass.enabled

    @property
    def entities(self) -> list[str]:
        """The entity ids to poll."""
        return [p.hass_entity for p in self.config.people if p.hass_entity]

    def token(self) -> str | None:
        """The long-lived token, from the environment only."""
        return os.environ.get(self.config.sources.hass.token_env) or None

    async def start(self, bus=None) -> None:
        """Start the polling task, if enabled and a token is present."""
        if not self.enabled:
            log.info("hass source disabled")
            return
        if not self.token():
            self.last_error = "%s is not set in the environment" % (
                self.config.sources.hass.token_env
            )
            log.warning("hass source enabled but %s", self.last_error)
            return
        self._task = asyncio.create_task(self._loop(), name="hass-poll")

    async def stop(self) -> None:
        """Cancel the polling task."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = self.config.sources.hass.poll_interval_s
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - network trouble
                self.errors += 1
                self.last_error = str(exc)
                log.warning("Home Assistant poll failed: %s", exc)
            await asyncio.sleep(interval)

    async def poll_once(self) -> int:
        """Poll every configured entity once.

        Returns:
            How many fixes were accepted.
        """
        import httpx

        settings = self.config.sources.hass
        token = self.token()
        if not token:
            return 0
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        accepted = 0
        factory = self.client_factory
        client = (
            factory()
            if factory is not None
            else httpx.AsyncClient(timeout=10.0, verify=settings.verify_tls)
        )
        async with client:
            for entity in self.entities:
                self.polls += 1
                url = "%s/api/states/%s" % (settings.url.rstrip("/"), entity)
                try:
                    response = await client.get(url, headers=headers)
                except Exception as exc:
                    self.errors += 1
                    self.last_error = "%s: %s" % (entity, exc)
                    self._reject(entity, str(exc))
                    continue
                if response.status_code != 200:
                    self.errors += 1
                    message = "HTTP %d" % response.status_code
                    self.last_error = "%s: %s" % (entity, message)
                    self._reject(entity, message)
                    continue
                result = parse_hass_state(response.json(), self.config, entity)
                if result.error is not None:
                    self.errors += 1
                    self.last_error = result.error
                    self._reject(entity, result.error)
                    continue
                if result.fix is None or result.person is None:
                    continue
                self.fixes += 1
                accepted += 1
                outcome = self.on_fix(result.person.slug, result.fix)
                if outcome is not None:
                    await outcome
        return accepted

    def _reject(self, entity: str, reason: str) -> None:
        if self.on_reject is not None:
            self.on_reject(entity, reason)

    def describe(self) -> dict:
        """Counters for the UI."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "url": self.config.sources.hass.url,
            "entities": len(self.entities),
            "token_present": bool(self.token()),
            "polls": self.polls,
            "fixes": self.fixes,
            "errors": self.errors,
            "last_error": self.last_error,
        }
