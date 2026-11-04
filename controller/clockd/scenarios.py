"""Scripted scenarios for the Simulate page.

A scenario drives manual overrides over time so a whole sequence can be watched
without anyone actually flying anywhere.  "Evan flies home" is the one the spec
names: Warsaw to the front door over sixty seconds, which sweeps a hand through
most of the dial and crosses 12 o'clock at the end -- exactly the move most
likely to expose a direction or shortest-path mistake.

Every scenario is driven through :meth:`clockd.engine.Engine.set_override`, so
what the hands do here is what they would do with real fixes arriving.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Warsaw to Minnetonka: the waypoints a real trip would report.
EVAN_ROUTE = [
    (52.2297, 21.0122, "Warsaw"),
    (52.1657, 20.9671, "Warsaw Chopin airport"),
    (50.0379, 8.5622, "Frankfurt"),
    (51.4700, -0.4543, "London Heathrow"),
    (45.0000, -45.0000, "mid-Atlantic"),
    (44.6400, -63.5700, "Halifax"),
    (41.9742, -87.9073, "Chicago O'Hare"),
    (44.8848, -93.2223, "Minneapolis-St Paul"),
    (44.9109, -93.5017, "home"),
]

#: A loop out and back, for checking one hand over its whole range.
ROUND_TRIP_KM = [1.0, 5.0, 50.0, 500.0, 5000.0, 19000.0, 5000.0, 500.0, 50.0, 1.0]


SCENARIOS: dict[str, dict[str, Any]] = {
    "evan_flies_home": {
        "title": "Evan flies home (Warsaw to the front door)",
        "needs_person": False,
    },
    "everyone_home": {
        "title": "Everyone comes home",
        "needs_person": False,
    },
    "everyone_scatters": {
        "title": "Everyone scatters to their usual places",
        "needs_person": False,
    },
    "round_trip": {
        "title": "One hand out to the antipode and back",
        "needs_person": True,
    },
    "commute": {
        "title": "One hand on a short commute (1 to 40 km and back)",
        "needs_person": True,
    },
}


async def run_scenario(
    engine: Any, name: str, person: str | None = None, seconds: float = 60.0
) -> None:
    """Play a scenario against a running engine.

    Args:
        engine: The :class:`clockd.engine.Engine`.
        name: A key of :data:`SCENARIOS`.
        person: Whose hand, for the scenarios that need one.
        seconds: How long the whole scenario should take.

    Raises:
        KeyError: for an unknown scenario or person.
        ValueError: when a scenario needs a person and none was given.
    """
    if name not in SCENARIOS:
        raise KeyError(f"no scenario named {name!r}")
    if SCENARIOS[name]["needs_person"] and not person:
        raise ValueError(f"scenario {name!r} needs a person")

    engine.events.action(
        f"scenario started: {SCENARIOS[name]['title']}"
        + (f" ({person})" if person else "")
        + f" over {seconds:.0f} s"
    )
    try:
        if name == "evan_flies_home":
            await _fly_route(engine, "evan", EVAN_ROUTE, seconds)
        elif name == "everyone_home":
            await _everyone(engine, lambda _slug: (engine.config.home.lat, engine.config.home.lon))
        elif name == "everyone_scatters":
            await _scatter(engine)
        elif name == "round_trip":
            await _distances(engine, person, ROUND_TRIP_KM, seconds)
        elif name == "commute":
            await _distances(
            engine, person, [1.0, 3.0, 12.0, 28.0, 40.0, 28.0, 12.0, 3.0, 1.0], seconds
        )
    except asyncio.CancelledError:
        engine.events.action(f"scenario {name} cancelled")
        raise
    engine.events.action(f"scenario finished: {name}")


async def _fly_route(engine: Any, slug: str, route: list[tuple], seconds: float) -> None:
    """Walk one person through a list of waypoints, evenly spaced in time."""
    try:
        engine.person(slug)
    except KeyError:
        # The person was renamed or removed from the config; use the first one.
        slug = next(iter(engine.people))
    step = seconds / max(1, len(route))
    ttl = seconds + 120.0
    for lat, lon, label in route:
        await engine.set_override(slug, lat=lat, lon=lon, ttl_s=ttl, note=f"scenario: {label}")
        await asyncio.sleep(step)


async def _distances(engine: Any, slug: str, distances: list[float], seconds: float) -> None:
    """Walk one hand through a list of distances."""
    engine.person(slug)
    step = seconds / max(1, len(distances))
    ttl = seconds + 120.0
    for distance in distances:
        await engine.set_override(
            slug, distance_km=distance, ttl_s=ttl, note=f"scenario: {distance:.0f} km"
        )
        await asyncio.sleep(step)


async def _everyone(engine: Any, position_for) -> None:
    """Put every person at a position chosen by a callable."""
    for slug in list(engine.people):
        lat, lon = position_for(slug)
        await engine.set_override(slug, lat=lat, lon=lon, note="scenario")
        await asyncio.sleep(0.2)


async def _scatter(engine: Any) -> None:
    """Send everyone back to the configured fallback position."""
    for person in engine.config.people:
        if person.fallback_lat is None or person.fallback_lon is None:
            continue
        await engine.set_override(
            person.slug,
            lat=person.fallback_lat,
            lon=person.fallback_lon,
            note="scenario: usual place",
        )
        await asyncio.sleep(0.2)
