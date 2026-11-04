"""Shared pytest setup: make ``clockd`` and the firmware modules importable."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = REPO_ROOT / "controller"
PICO = REPO_ROOT / "firmware" / "pico"

for path in (CONTROLLER, PICO, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def sim_config(tmp_path):
    """A validated config pointing its state at a temp directory."""
    from clockd.config import example_config_path, load_config

    config = load_config(example_config_path())
    config.paths.var_dir = str(tmp_path / "var")
    config.paths.log_dir = str(tmp_path / "var" / "log")
    config.filtering.min_move_interval_s = 0.0
    config.serial.ping_interval_s = 0.2
    return config


class SimStack:
    """Everything a test needs to drive the controller with no hardware.

    Holds the fake Pico, the in-memory broker, the engine and the event log, and
    offers the few helpers the tests keep reaching for.
    """

    def __init__(self, config, engine, bus, link, pico, store, events):
        self.config = config
        self.engine = engine
        self.bus = bus
        self.link = link
        self.pico = pico
        self.store = store
        self.events = events

    async def publish_owntracks(self, user, device, lat, lon, **extra):
        """Publish a realistic OwnTracks location message."""
        payload = {"_type": "location", "lat": lat, "lon": lon, "tst": time.time()}
        payload.update(extra)
        await self.bus.publish(f"owntracks/{user}/{device}", json.dumps(payload))
        await asyncio.sleep(0.05)

    async def publish_generic(self, slug, **payload):
        """Publish a clock/location/<person> message."""
        payload.setdefault("ts", time.time())
        await self.bus.publish(f"clock/location/{slug}", json.dumps(payload))
        await asyncio.sleep(0.05)

    async def zero_all(self):
        """Zero and verify every hand, as the Assist wizard would."""
        for slug in list(self.engine.people):
            await self.engine.set_zero(slug)

    async def settle(self, seconds=3.0, step=0.05):
        """Wait until no hand is moving, or the budget runs out."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(step)
            self.engine.refresh_positions()
            idle = not any(p.moving for p in self.engine.people.values())
            if idle and not self.pico.core.mc.any_moving():
                return True
        return False

    def retained(self, topic):
        """The retained payload on a topic, decoded, or ``None``."""
        message = self.bus.retained.get(topic)
        return message.json() if message else None


@pytest.fixture
async def stack(sim_config):
    """A started controller with a fake Pico and an in-memory broker."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.pico_sim import MonotonicClock, SimPico

    from clockd.bus import MemoryBus
    from clockd.engine import Engine
    from clockd.eventlog import EventLog
    from clockd.picolink import PicoLink, SimTransport
    from clockd.store import Store

    bus = MemoryBus()
    store = Store(sim_config.var_path("state.json"))
    events = EventLog(500)
    # 20x simulated time so a full sweep finishes inside a test; the ping
    # interval is scaled to match or the firmware watchdog would trip.
    pico = SimPico(channels=6, clock=MonotonicClock(20.0))
    link = PicoLink(
        lambda: SimTransport(pico),
        ping_interval_s=0.1,
        command_timeout_s=3.0,
        channels=6,
    )
    engine = Engine(sim_config, bus, link, store, events, sim=True)
    link.on_connection_change = engine.on_link_change
    await engine.start()

    deadline = time.monotonic() + 5.0
    while not link.connected and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await engine.set_enabled(True)

    value = SimStack(sim_config, engine, bus, link, pico, store, events)
    try:
        yield value
    finally:
        await engine.stop()
