"""The clock engine: locations in, hand angles out.

This is where the pieces meet.  Fixes arrive from the sources, get filtered,
become a distance and an angle, and -- if the change is worth it -- a microstep
target for the Pico.  Meanwhile the retained MQTT topics are kept current for
the six display nodes, and the UI drives the same methods a person would.

What it deliberately does *not* do: move a hand it does not believe in.  With no
home sensors, a hand whose zero has never been confirmed has an unknown physical
position, so it is marked ``unverified`` and left alone until someone at the
clock walks through the Assist wizard.  The alternative -- driving to a computed
angle from a guessed origin -- looks like it is working and is wrong by an
unknown amount, which is much worse to debug from another state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from . import geo
from .bus import Bus, Message
from .config import ClockConfig, Person
from .eventlog import EventLog
from .filters import accept_fix, should_move
from .picolink import CommandFailed, LinkError, PicoLink
from .sources import GenericMqttSource, HassSource, OwnTracksSource
from .state import Fix, Override, PersonState
from .store import Store

log = logging.getLogger(__name__)

#: How often the engine republishes state and the heartbeat, in seconds.
PUBLISH_INTERVAL_S = 5.0

#: How often the engine re-reads positions from the link, in seconds.
POLL_INTERVAL_S = 0.5


class Engine:
    """Everything the controller does, minus the HTTP layer.

    Args:
        config: The validated configuration.
        bus: An MQTT bus (real or in-memory).
        link: The Pico link.
        store: Durable state.
        events: The event log.
        sim: True when running in ``--sim`` mode, which the UI displays.
    """

    def __init__(
        self,
        config: ClockConfig,
        bus: Bus,
        link: PicoLink,
        store: Store,
        events: EventLog,
        *,
        sim: bool = False,
    ):
        self.config = config
        self.bus = bus
        self.link = link
        self.store = store
        self.events = events
        self.sim = sim
        self.started_at = time.time()
        self.running = False

        self.people: dict[str, PersonState] = {}
        self._last_move_at: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._listeners: list[Any] = []

        self.sources: dict[str, Any] = {
            "owntracks": OwnTracksSource(config, self.on_fix, self._on_source_error),
            "generic_mqtt": GenericMqttSource(config, self.on_fix, self._on_source_error),
            "hass": HassSource(config, self.on_fix, self._on_source_error),
        }

        self._build_people()
        link.set_reset_handler(self.on_pico_reset)

    # -- setup -----------------------------------------------------------

    def _build_people(self) -> None:
        """Create a :class:`PersonState` per configured person, from the store."""
        for person in self.config.people:
            slug = person.slug
            state = PersonState(
                name=person.name,
                slug=slug,
                level=person.level,
                channel=person.channel,
                color=person.color,
                enabled=person.enabled,
                verified=self.store.is_verified(slug),
                verified_at=self.store.verified_at(slug),
                zero_offset_deg=self.store.get_zero(slug) or person.zero_offset_deg,
            )
            stored = self.store.get_fix(slug)
            if stored:
                fix = Fix.from_dict(stored)
                self._apply_fix_to_state(state, person, fix, publish=False)
            elif person.fallback_lat is not None and person.fallback_lon is not None:
                # Something on the dial beats a blank one on a cold start, but
                # it is labelled as the config fallback, not a real sighting.
                distance, angle = self.config.angle_for(person.fallback_lat, person.fallback_lon)
                state.distance_km = distance
                state.target_deg = angle
                state.source = "startup"
            steps = self.store.get_steps(slug)
            if steps is not None:
                state.target_steps = steps
            self.people[slug] = state

    def person(self, slug: str) -> Person:
        """Config record for a slug.

        Raises:
            KeyError: when there is no such person.
        """
        return self.config.person(slug)

    def state(self, slug: str) -> PersonState:
        """Live state for a slug.

        Raises:
            KeyError: when there is no such person.
        """
        if slug not in self.people:
            raise KeyError("no person named %r" % slug)
        return self.people[slug]

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Connect everything and start the background loops."""
        if self.running:
            return
        self.running = True
        self.events.system(
            "controller starting (%s mode)" % ("sim" if self.sim else "live"),
            sim=self.sim,
            people=len(self.people),
        )

        base = self.config.mqtt.base_topic.strip("/")
        will = {"online": False, "reason": "last will", "at": None}
        if hasattr(self.bus, "set_will"):
            self.bus.set_will("%s/system/status" % base, will, retain=True)

        await self.bus.start()
        for source in self.sources.values():
            with contextlib.suppress(Exception):
                await source.start(self.bus)

        await self.link.start()

        self._tasks = [
            asyncio.create_task(self._publish_loop(), name="publish"),
            asyncio.create_task(self._poll_loop(), name="poll"),
        ]
        await self.publish_system(online=True)

    async def stop(self) -> None:
        """Stop the loops, say goodbye on MQTT and close the link."""
        if not self.running:
            return
        self.running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        hass = self.sources.get("hass")
        if hass is not None:
            with contextlib.suppress(Exception):
                await hass.stop()
        with contextlib.suppress(Exception):
            await self.publish_system(online=False, reason="clean shutdown")
        with contextlib.suppress(Exception):
            await self.link.close()
        with contextlib.suppress(Exception):
            await self.bus.stop()
        with contextlib.suppress(Exception):
            self.store.save()
        self.events.system("controller stopped")

    # -- incoming fixes --------------------------------------------------

    def _on_source_error(self, topic: str, reason: str) -> None:
        """A source could not use a payload."""
        self.events.add("error", "rejected payload on %s: %s" % (topic, reason), topic=topic)

    async def on_fix(self, slug: str, fix: Fix) -> None:
        """Handle one incoming fix: filter it, store it, maybe move the hand.

        Args:
            slug: The person the fix belongs to.
            fix: The incoming position.
        """
        try:
            state = self.state(slug)
            person = self.person(slug)
        except KeyError:
            self.events.error("fix for unknown person %r" % slug)
            return

        if not person.enabled:
            return

        # A live manual override outranks every real source, so a fix arriving
        # while someone is testing does not yank the hand away mid-test.
        if state.override is not None and state.override.active():
            state.last_reject_reason = "override"
            self.events.fix(
                "%s: fix from %s ignored, manual override is active" % (person.name, fix.source),
                person=slug,
                source=fix.source,
            )
            return

        last = state.last_fix
        decision = accept_fix(fix, last, self.config.filtering)
        if not decision.accept:
            state.rejected += 1
            state.last_reject_reason = decision.reason
            self.events.fix(
                "%s: rejected %s fix (%s)" % (person.name, fix.source, decision.detail),
                person=slug,
                reason=decision.reason,
                source=fix.source,
            )
            return

        state.accepted += 1
        state.last_reject_reason = None
        self._apply_fix_to_state(state, person, fix, publish=False)
        self.store.set_fix(slug, fix.to_dict())

        self.events.fix(
            "%s: %s fix accepted, %.1f km -> %.1f deg"
            % (person.name, fix.source, state.distance_km or 0.0, state.target_deg or 0.0),
            person=slug,
            source=fix.source,
            distance_km=state.distance_km,
            target_deg=state.target_deg,
            accuracy_m=fix.accuracy_m,
        )

        await self.apply_target(slug)
        await self.publish_person(slug)

    def _apply_fix_to_state(
        self, state: PersonState, person: Person, fix: Fix, *, publish: bool = True
    ) -> None:
        """Copy a fix into a person's state and recompute the angle."""
        if fix.distance_km is not None:
            distance = fix.distance_km
            angle = geo.distance_to_angle(distance)
        else:
            distance, angle = self.config.angle_for(fix.lat, fix.lon)
        state.last_fix = fix
        state.last_seen = fix.ts
        state.source = fix.source
        state.accuracy_m = fix.accuracy_m
        state.battery = fix.battery
        state.distance_km = distance
        state.target_deg = angle

    # -- driving the hands -----------------------------------------------

    async def apply_target(self, slug: str, *, force: bool = False) -> bool:
        """Command the hand for ``slug`` to its current target angle.

        Args:
            slug: Whose hand.
            force: Bypass the deadband and the minimum interval, as the UI's
                direct controls do.

        Returns:
            True when a move was actually commanded.
        """
        state = self.state(slug)
        person = self.person(slug)

        if not person.enabled:
            return False
        if state.target_deg is None:
            return False
        if not state.verified and not force:
            # Known-unknown position: refuse rather than guess.  The UI says so.
            return False
        if not self.link.connected:
            return False

        current_steps = self._current_steps(state)
        current_deg = self.config.steps_to_angle(person, current_steps)
        last_commanded = (
            self.config.steps_to_angle(person, state.target_steps)
            if state.target_steps is not None
            else None
        )
        decision = should_move(
            last_commanded,
            state.target_deg,
            self.config.filtering,
            last_move_at=self._last_move_at.get(slug),
            force=force,
        )
        if not decision.accept:
            return False

        # Shortest path in angle space, then convert once.
        unwrapped = geo.shortest_path_target(current_deg, state.target_deg)
        target_steps = round(self.config.angle_to_steps(person, unwrapped))

        try:
            await self._ensure_enabled()
            await self.link.move(person.channel, target_steps)
        except (LinkError, CommandFailed) as exc:
            state.fault = str(exc)
            self.events.error("%s: move failed: %s" % (person.name, exc), person=slug)
            return False

        state.fault = None
        state.target_steps = target_steps
        self._last_move_at[slug] = time.time()
        state.last_move_at = self._last_move_at[slug]
        self.store.set_steps(slug, target_steps)
        self.events.action(
            "%s: hand to %.1f deg (%d microsteps, %s)"
            % (person.name, geo.wrap360(unwrapped), target_steps, decision.detail),
            person=slug,
            target_deg=state.target_deg,
            target_steps=target_steps,
        )
        return True

    def _current_steps(self, state: PersonState) -> int:
        """Best estimate of where a hand physically is, in microsteps."""
        positions = self.link.positions
        if 0 <= state.channel < len(positions):
            return int(positions[state.channel])
        if state.target_steps is not None:
            return int(state.target_steps)
        return 0

    async def _ensure_enabled(self) -> None:
        """Turn the drivers on if they are off, so a move is not a no-op."""
        if not self.link.enabled:
            await self.link.set_enable(True)

    # -- UI actions ------------------------------------------------------

    async def jog(self, slug: str, degrees: float) -> dict[str, Any]:
        """Nudge one hand by a number of degrees.

        Used by the Control page's +/- buttons and by the Assist wizard's big
        arrows.  Works whether or not the hand is verified -- nudging is how a
        hand *becomes* verified.
        """
        person = self.person(slug)
        state = self.state(slug)
        delta_steps = round(degrees * self.config.steps_per_degree(person))
        if person.invert:
            delta_steps = -delta_steps
        await self._ensure_enabled()
        reply = await self.link.jog(person.channel, delta_steps)
        state.target_steps = int(reply.get("target", state.target_steps or 0))
        self.store.set_steps(slug, state.target_steps)
        self._last_move_at[slug] = time.time()
        self.events.action(
            "%s: jog %+.2f deg (%+d microsteps)" % (person.name, degrees, delta_steps),
            person=slug,
            degrees=degrees,
            steps=delta_steps,
        )
        return reply

    async def move_to_angle(self, slug: str, angle_deg: float) -> dict[str, Any]:
        """Send one hand to an absolute dial angle, by the shortest path."""
        person = self.person(slug)
        state = self.state(slug)
        current_steps = self._current_steps(state)
        current_deg = self.config.steps_to_angle(person, current_steps)
        unwrapped = geo.shortest_path_target(current_deg, angle_deg)
        target_steps = round(self.config.angle_to_steps(person, unwrapped))
        await self._ensure_enabled()
        reply = await self.link.move(person.channel, target_steps)
        state.target_steps = target_steps
        self.store.set_steps(slug, target_steps)
        self._last_move_at[slug] = time.time()
        self.events.action(
            "%s: move to %.2f deg (%d microsteps)" % (person.name, geo.wrap360(angle_deg), target_steps),
            person=slug,
            angle_deg=angle_deg,
            target_steps=target_steps,
        )
        return reply

    async def set_zero(self, slug: str, *, verify: bool = True) -> dict[str, Any]:
        """Declare that this hand is pointing at 12 o'clock right now.

        This is the single most important action in the whole system, because
        it is the only way an open-loop hand with no home sensor learns where it
        is.  It tells the Pico that the current microstep position is zero, and
        records the hand as verified so the engine will drive it again.
        """
        person = self.person(slug)
        state = self.state(slug)
        with contextlib.suppress(LinkError, CommandFailed):
            await self.link.stop(person.channel)
            await asyncio.sleep(0.1)
        reply = await self.link.set_pos(person.channel, 0)
        state.target_steps = 0
        state.actual_steps = 0
        state.actual_deg = 0.0
        self.store.set_steps(slug, 0)
        self.store.set_zero(slug, 0.0)
        state.zero_offset_deg = 0.0
        if verify:
            state.verified = True
            state.verified_at = time.time()
            self.store.set_verified(slug, True, when=state.verified_at)
        self._last_move_at.pop(slug, None)
        self.events.action(
            "%s: zero set here; hand is now at 12 o'clock%s"
            % (person.name, " and verified" if verify else ""),
            person=slug,
        )
        await self.publish_person(slug)
        return reply

    async def set_verified(self, slug: str, verified: bool) -> None:
        """Mark a hand as confirmed (or not) without changing its position."""
        state = self.state(slug)
        state.verified = bool(verified)
        state.verified_at = time.time() if verified else None
        self.store.set_verified(slug, bool(verified), when=state.verified_at)
        self.events.action(
            "%s: marked %s" % (state.name, "verified" if verified else "unverified"),
            person=slug,
        )
        await self.publish_person(slug)

    async def set_enabled(self, on: bool) -> dict[str, Any]:
        """Enable or disable all six drivers."""
        reply = await self.link.set_enable(on)
        self.events.action("drivers %s" % ("enabled" if on else "disabled"), enabled=on)
        return reply

    async def stop_all(self) -> dict[str, Any]:
        """Decelerate every hand to a stop."""
        reply = await self.link.stop()
        self.events.action("STOP ALL: every hand decelerating")
        return reply

    async def estop(self) -> dict[str, Any]:
        """Stop every hand immediately and mark them all unverified.

        An immediate stop at speed can slip the rotor, so the positions can no
        longer be trusted.  Saying so is the honest outcome, even though it
        means a trip to the clock.
        """
        reply = await self.link.estop()
        for slug, state in self.people.items():
            state.verified = False
            state.verified_at = None
            self.store.set_verified(slug, False)
        self.events.action("E-STOP: all hands halted; every hand is now unverified")
        await self.publish_all()
        return reply

    async def move_all_to_twelve(self) -> int:
        """Send every enabled hand to 12 o'clock.  Returns how many moved."""
        moved = 0
        for slug, state in self.people.items():
            if not state.enabled:
                continue
            with contextlib.suppress(LinkError, CommandFailed):
                await self.move_to_angle(slug, 0.0)
                moved += 1
        self.events.action("all hands to 12 o'clock (%d hands)" % moved)
        return moved

    async def resume_live(self) -> int:
        """Send every verified hand to its current live target.

        The Assist wizard's last step: after zeroing, put every hand where the
        locations actually say it should be, so the person at the clock can see
        it do something real before they walk away.
        """
        moved = 0
        for slug in self.people:
            if await self.apply_target(slug, force=True):
                moved += 1
        self.events.action("all hands to their live positions (%d moved)" % moved)
        return moved

    async def test_sweep(self, slug: str, *, speed: float | None = None) -> None:
        """Walk one hand all the way round and back, slowly.

        A whole-turn sweep is the quickest way to prove a channel end to end:
        wiring, direction, current limit and the tube stack all show up in it.
        """
        person = self.person(slug)
        state = self.state(slug)
        sweep_speed = speed or self.config.motion.assist_speed
        start = self._current_steps(state)
        full_turn = person.steps_per_rev
        self.events.action(
            "%s: test sweep, one turn and back at %.0f steps/s" % (person.name, sweep_speed),
            person=slug,
        )
        await self.link.set_params(max_speed=sweep_speed, channel=person.channel)
        try:
            await self._ensure_enabled()
            await self.link.move(person.channel, start + full_turn)
            await self._await_rest(person.channel)
            await self.link.move(person.channel, start)
            await self._await_rest(person.channel)
        finally:
            await self.link.set_params(
                max_speed=self.config.motion.max_speed, channel=person.channel
            )
        self.events.action("%s: test sweep finished" % person.name, person=slug)

    async def _await_rest(self, channel: int, timeout_s: float = 180.0) -> None:
        """Wait until one channel stops moving, or the timeout expires."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            with contextlib.suppress(LinkError, CommandFailed):
                await self.link.status()
            moving = self.link.moving
            if channel < len(moving) and not moving[channel]:
                return

    async def set_motion_params(
        self, max_speed: float | None = None, accel: float | None = None
    ) -> dict[str, Any]:
        """Change speed and acceleration, clamped to the configured ceilings."""
        limits = self.config.motion
        if max_speed is not None:
            max_speed = max(1.0, min(float(max_speed), limits.speed_limit))
        if accel is not None:
            accel = max(1.0, min(float(accel), limits.accel_limit))
        reply = await self.link.set_params(max_speed=max_speed, accel=accel)
        self.events.action(
            "motion parameters: speed %s, accel %s" % (max_speed, accel),
            max_speed=max_speed,
            accel=accel,
        )
        return reply

    # -- overrides -------------------------------------------------------

    async def set_override(
        self,
        slug: str,
        *,
        lat: float | None = None,
        lon: float | None = None,
        distance_km: float | None = None,
        ttl_s: float | None = None,
        note: str = "",
    ) -> Override:
        """Put a fake position or distance on one hand, with an expiry.

        Args:
            slug: Whose hand.
            lat: Fake latitude, with ``lon``.
            lon: Fake longitude, with ``lat``.
            distance_km: Fake distance; used when no coordinates are given.
            ttl_s: How long it lasts; defaults to the configured TTL.
            note: Free text shown on the UI badge.

        Returns:
            The :class:`Override` that is now in force.
        """
        state = self.state(slug)
        person = self.person(slug)
        ttl = ttl_s if ttl_s is not None else self.config.sources.manual.default_ttl_s
        override = Override(
            expires_at=time.time() + ttl,
            lat=lat,
            lon=lon,
            distance_km=distance_km,
            note=note,
        )
        state.override = override

        if lat is not None and lon is not None:
            distance, angle = self.config.angle_for(lat, lon)
        elif distance_km is not None:
            distance = max(0.0, distance_km)
            angle = geo.distance_to_angle(distance)
        else:
            raise ValueError("an override needs either lat and lon, or distance_km")

        state.distance_km = distance
        state.target_deg = angle
        state.source = "manual"
        state.last_seen = time.time()
        self.events.action(
            "%s: override -> %.1f km (%.1f deg) for %.0f s"
            % (person.name, distance, angle, ttl),
            person=slug,
            distance_km=distance,
            target_deg=angle,
        )
        await self.apply_target(slug, force=True)
        await self.publish_person(slug)
        return override

    async def clear_override(self, slug: str) -> None:
        """Drop a manual override and go back to the last real fix."""
        state = self.state(slug)
        if state.override is None:
            return
        state.override = None
        person = self.person(slug)
        if state.last_fix is not None:
            self._apply_fix_to_state(state, person, state.last_fix, publish=False)
        self.events.action("%s: override cleared" % person.name, person=slug)
        await self.apply_target(slug, force=True)
        await self.publish_person(slug)

    def _expire_overrides(self) -> list[str]:
        """Drop overrides whose time is up.  Returns the slugs affected."""
        now = time.time()
        expired = []
        for slug, state in self.people.items():
            if state.override is not None and not state.override.active(now):
                state.override = None
                expired.append(slug)
        return expired

    def set_source_enabled(self, name: str, enabled: bool) -> bool:
        """Switch a location source on or off at runtime.

        The change is held in the live config object, so it applies at once and
        is lost on restart unless the YAML is saved from the Config page.  That
        is deliberate: switching a source off to test something should not
        quietly persist and leave the clock blind next month.
        """
        settings = getattr(self.config.sources, name, None)
        if settings is None or not hasattr(settings, "enabled"):
            raise KeyError("no source named %r" % name)
        settings.enabled = bool(enabled)
        self.events.action(
            "source %s %s" % (name, "enabled" if enabled else "disabled"),
            source=name,
            enabled=enabled,
        )
        return settings.enabled

    # -- link callbacks --------------------------------------------------

    async def on_pico_reset(self) -> None:
        """The Pico restarted: nothing about hand positions can be trusted."""
        for slug, state in self.people.items():
            state.verified = False
            state.verified_at = None
            state.actual_steps = None
            self.store.set_verified(slug, False)
        self.events.error(
            "Pico reset: every hand is unverified until someone confirms it at the clock"
        )
        await self.publish_all()

    async def on_link_change(self, up: bool) -> None:
        """The serial link came up or went down."""
        for state in self.people.values():
            state.link_down = not up
        self.events.system("Pico link %s" % ("up" if up else "down"), connected=up)
        await self.publish_system(online=True)

    # -- publishing ------------------------------------------------------

    def topic(self, *parts: str) -> str:
        """Build a topic under the configured base."""
        base = self.config.mqtt.base_topic.strip("/")
        return "/".join([base, *parts])

    async def publish_person(self, slug: str) -> None:
        """Publish the retained state and display topics for one person."""
        state = self.people[slug]
        stale_after = self.config.filtering.stale_after_s
        await self.bus.publish(
            self.topic("state", slug), state.to_state_payload(stale_after), retain=True
        )
        await self.bus.publish(
            self.topic("display", slug), state.to_display_payload(stale_after), retain=True
        )

    async def publish_all(self) -> None:
        """Publish every person's topics."""
        for slug in self.people:
            await self.publish_person(slug)

    async def publish_system(self, *, online: bool = True, reason: str = "") -> None:
        """Publish the system status topic (the Last Will's live counterpart)."""
        payload = {
            "online": online,
            "sim": self.sim,
            "version": _version(),
            "uptime_s": round(time.time() - self.started_at, 1),
            "pico": {
                "connected": self.link.connected,
                "firmware": self.link.info.get("fw"),
                "enabled": self.link.enabled,
                # The controller's own records, not the firmware's reboot flag:
                # the firmware only knows "I restarted", while the controller
                # knows whether a person has since confirmed each hand.
                "unverified_hands": sorted(
                    slug for slug, state in self.people.items() if not state.verified
                ),
                "pico_rebooted": self.link.needs_verification,
            },
            "people": len(self.people),
            "at": time.time(),
        }
        if reason:
            payload["reason"] = reason
        await self.bus.publish(self.topic("system", "status"), payload, retain=True)

    async def _publish_loop(self) -> None:
        """Republish state and a heartbeat on a timer."""
        while self.running:
            try:
                for slug in self._expire_overrides():
                    await self.clear_override(slug)
                await self.publish_all()
                await self.publish_system(online=True)
                await self.bus.publish(
                    self.topic("system", "heartbeat"),
                    {"at": time.time(), "uptime_s": round(time.time() - self.started_at, 1)},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("publish loop failed")
            await asyncio.sleep(PUBLISH_INTERVAL_S)

    async def _poll_loop(self) -> None:
        """Copy positions from the link into the person states.

        Also publishes a person's topics the moment their hand starts or stops
        moving, instead of waiting for the next five-second republish.  Without
        this the dashboard and the display beside the hand both lag the hand
        itself, which makes the clock look broken while it is working.
        """
        was_moving = {slug: state.moving for slug, state in self.people.items()}
        while self.running:
            try:
                self.refresh_positions()
                for slug, state in self.people.items():
                    if state.moving != was_moving.get(slug):
                        was_moving[slug] = state.moving
                        await self.publish_person(slug)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop failed")
            await asyncio.sleep(POLL_INTERVAL_S)

    def refresh_positions(self) -> None:
        """Read the link's latest positions into every person's state."""
        positions = self.link.positions
        moving = self.link.moving
        for state in self.people.values():
            person = self.config.person_for_channel(state.channel)
            if person is None:
                continue
            if 0 <= state.channel < len(positions):
                steps = int(positions[state.channel])
                state.actual_steps = steps
                state.actual_deg = geo.wrap360(self.config.steps_to_angle(person, steps))
            if 0 <= state.channel < len(moving):
                state.moving = bool(moving[state.channel])
            state.link_down = not self.link.connected

    # -- views for the UI ------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the UI needs in one object, for the dashboard and the WS."""
        self.refresh_positions()
        now = time.time()
        stale_after = self.config.filtering.stale_after_s
        return {
            "at": now,
            "sim": self.sim,
            "site": self.config.site,
            "title": self.config.web.title,
            "home": {
                "name": self.config.home.name,
                "lat": self.config.home.lat,
                "lon": self.config.home.lon,
            },
            "uptime_s": round(now - self.started_at, 1),
            "version": _version(),
            "people": [state.to_ui(stale_after, now) for state in self.people.values()],
            "pico": self.link.describe(),
            "mqtt": self.bus.describe(),
            "sources": {name: source.describe() for name, source in self.sources.items()},
            "motion": self.config.motion.model_dump(),
            "filtering": self.config.filtering.model_dump(),
            "store": {"path": str(self.store.path), "error": self.store.load_error},
            "events_last_id": self.events.last_id(),
        }

    def mqtt_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent published MQTT messages, for the Logs page."""
        history: list[Message] = list(getattr(self.bus, "history", []))
        out = []
        for message in history[-limit:]:
            out.append(
                {
                    "at": message.at,
                    "topic": message.topic,
                    "payload": message.text()[:500],
                    "retain": message.retain,
                }
            )
        return out


def _version() -> str:
    """The controller's version string."""
    from . import __version__

    return __version__
