"""End to end: an OwnTracks fix in, microsteps and MQTT state out.

The controller runs against ``tools/pico_sim.py``, which is the firmware's own
``core``/``motion``/``protocol`` modules with the pins swapped for counters.  So
these tests check the real command path, not a mock of it.
"""

from __future__ import annotations

import asyncio

import pytest

from clockd import geo

BOULDER = (40.0150, -105.2705)
WARSAW = (52.2297, 21.0122)
HOME = (44.9109, -93.5017)


async def test_an_owntracks_fix_moves_the_right_hand_to_the_right_angle(stack):
    """The headline path: Austin's phone reports Boulder, Austin's hand points at 254.8."""
    await stack.zero_all()
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await stack.settle()

    state = stack.engine.state("austin")
    assert state.distance_km == pytest.approx(1107, abs=1)
    assert state.target_deg == pytest.approx(254.8, abs=0.1)
    assert state.actual_deg == pytest.approx(254.8, abs=0.5)
    assert state.source == "owntracks"
    assert state.accepted == 1


async def test_the_commanded_microsteps_match_the_angle(stack):
    """3200 microsteps per turn, and the shortest way round."""
    await stack.zero_all()
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await stack.settle()

    person = stack.config.person("austin")
    channel = person.channel
    steps = stack.pico.positions()[channel]
    # 254.8 degrees forward is 105.2 degrees backward, which is the short way.
    assert steps == pytest.approx(-935, abs=2)
    assert geo.wrap360(stack.config.steps_to_angle(person, steps)) == pytest.approx(254.8, abs=0.5)


async def test_each_person_drives_only_their_own_channel(stack):
    """Six people, six levels: a fix for one must not touch another's motor."""
    await stack.zero_all()
    before = list(stack.pico.positions())
    await stack.publish_owntracks("micah", "pixel", 44.5646, -123.2620, acc=20)
    await stack.settle()
    after = stack.pico.positions()

    micah_channel = stack.config.person("micah").channel
    assert after[micah_channel] != before[micah_channel]
    for channel in range(6):
        if channel != micah_channel:
            assert after[channel] == before[channel], f"channel {channel} moved"


async def test_all_six_spec_vectors_end_to_end(stack):
    """Every person in the spec table, through MQTT, to a hand angle."""
    await stack.zero_all()
    vectors = [
        ("becky", 44.9109, -93.5017, 0.0),
        ("elijah", 44.9537, -93.0900, 126.8),
        ("caleb", 41.8661, -88.1070, 229.5),
        ("austin", 40.0150, -105.2705, 254.8),
        ("micah", 44.5646, -123.2620, 281.9),
        ("evan", 52.2297, 21.0122, 324.5),
    ]
    for slug, lat, lon, _expected in vectors:
        await stack.publish_generic(slug, lat=lat, lon=lon, acc=20)
    await stack.settle(seconds=8.0)

    for slug, _lat, _lon, expected in vectors:
        state = stack.engine.state(slug)
        assert state.target_deg == pytest.approx(expected, abs=0.1), slug
        assert state.actual_deg == pytest.approx(expected, abs=0.6), slug


async def test_retained_state_and_display_topics_are_published(stack):
    await stack.zero_all()
    await stack.publish_owntracks("evan", "iphone", *WARSAW, acc=30)
    await stack.settle()

    state = stack.retained("clock/state/evan")
    assert state["name"] == "Evan"
    assert state["distance_km"] == pytest.approx(7537, abs=1)
    assert state["target_deg"] == pytest.approx(324.5, abs=0.1)
    assert state["status"] == "ok"
    assert state["source"] == "owntracks"
    assert state["accuracy_m"] == 30

    display = stack.retained("clock/display/evan")
    assert display["status"] == "away"
    assert display["text"] == "7537 km away"
    assert display["fraction"] == pytest.approx(0.901, abs=0.002)
    assert display["updated_iso"].endswith("+00:00")


async def test_display_says_home_when_home(stack):
    await stack.zero_all()
    await stack.publish_generic("becky", lat=HOME[0], lon=HOME[1], acc=10)
    await stack.settle()
    display = stack.retained("clock/display/becky")
    assert display["status"] == "home"
    assert display["text"] == "HOME"
    assert display["distance_km"] == pytest.approx(0.0, abs=0.1)


async def test_system_status_is_retained_and_the_will_fires_on_stop(stack):
    status = stack.retained("clock/system/status")
    assert status["online"] is True
    assert status["sim"] is True
    assert status["pico"]["connected"] is True

    await stack.engine.stop()
    # The engine publishes a clean goodbye; the broker then delivers the will.
    final = stack.retained("clock/system/status")
    assert final["online"] is False


async def test_a_rejected_fix_leaves_the_hand_alone(stack):
    await stack.zero_all()
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await stack.settle()
    good = stack.engine.state("austin").target_deg
    steps = list(stack.pico.positions())

    await stack.publish_owntracks("austin", "iphone", 0.0, 0.0, acc=9999)
    await asyncio.sleep(0.3)

    state = stack.engine.state("austin")
    assert state.target_deg == pytest.approx(good)
    assert state.rejected == 1
    assert state.last_reject_reason == "accuracy"
    assert stack.pico.positions() == steps


async def test_an_unverified_hand_is_not_driven(stack):
    """Refusing to guess is the whole point of the verified flag."""
    before = list(stack.pico.positions())
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await asyncio.sleep(0.4)

    state = stack.engine.state("austin")
    assert state.target_deg == pytest.approx(254.8, abs=0.1)  # it knows where to go
    assert not state.verified
    assert stack.pico.positions() == before  # and it does not go there
    assert state.status(stack.config.filtering.stale_after_s) == "unverified"


async def test_zeroing_then_resuming_puts_the_hand_where_it_belongs(stack):
    """The Assist wizard's path: zero every hand, then 'all to live positions'."""
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await asyncio.sleep(0.3)
    await stack.zero_all()
    moved = await stack.engine.resume_live()
    await stack.settle(seconds=6.0)

    assert moved >= 1
    assert stack.engine.state("austin").actual_deg == pytest.approx(254.8, abs=0.6)


async def test_the_deadband_suppresses_a_pointless_move(stack):
    await stack.zero_all()
    await stack.publish_generic("austin", lat=BOULDER[0], lon=BOULDER[1], acc=10)
    await stack.settle()
    steps = list(stack.pico.positions())

    # Thirty metres away: well under half a degree on a log scale at 1100 km.
    await stack.publish_generic("austin", lat=BOULDER[0] + 0.0003, lon=BOULDER[1], acc=10)
    await asyncio.sleep(0.3)
    assert stack.pico.positions() == steps


async def test_an_override_wins_over_a_live_fix_and_then_expires(stack):
    await stack.zero_all()
    await stack.engine.set_override("micah", distance_km=10.0, ttl_s=0.4)
    await stack.settle()
    assert stack.engine.state("micah").target_deg == pytest.approx(83.69, abs=0.1)

    # A real fix arriving during the override is ignored, not queued.
    await stack.publish_generic("micah", lat=44.5646, lon=-123.2620, acc=20)
    await asyncio.sleep(0.2)
    assert stack.engine.state("micah").target_deg == pytest.approx(83.69, abs=0.1)

    await asyncio.sleep(0.5)
    expired = stack.engine._expire_overrides()
    assert "micah" in expired
    assert stack.engine.state("micah").override is None


async def test_clearing_an_override_returns_to_the_last_real_fix(stack):
    await stack.zero_all()
    await stack.publish_generic("caleb", lat=41.8661, lon=-88.1070, acc=20)
    await stack.settle()
    await stack.engine.set_override("caleb", distance_km=5.0)
    await stack.settle()
    assert stack.engine.state("caleb").target_deg == pytest.approx(58.6, abs=0.3)

    await stack.engine.clear_override("caleb")
    await stack.settle()
    assert stack.engine.state("caleb").target_deg == pytest.approx(229.5, abs=0.1)


async def test_a_pico_reset_marks_every_hand_unverified(stack):
    """After a brownout the stored angles are guesses, and we say so."""
    await stack.zero_all()
    assert all(p.verified for p in stack.engine.people.values())

    stack.pico.reset()
    await asyncio.sleep(0.6)

    assert all(not p.verified for p in stack.engine.people.values())
    assert not stack.store.is_verified("austin")
    messages = [e.message for e in stack.events.events]
    assert any("Pico reset" in m for m in messages)


async def test_the_controller_recovers_after_a_pico_reset(stack):
    stack.pico.reset()
    await asyncio.sleep(0.6)
    await stack.engine.set_enabled(True)
    await stack.zero_all()
    await stack.publish_generic("evan", lat=WARSAW[0], lon=WARSAW[1], acc=20)
    await stack.settle(seconds=8.0)
    assert stack.engine.state("evan").actual_deg == pytest.approx(324.5, abs=0.6)


async def test_an_estop_halts_and_unverifies(stack):
    await stack.zero_all()
    await stack.engine.move_to_angle("evan", 300.0)
    await asyncio.sleep(0.1)
    await stack.engine.estop()
    assert all(not p.verified for p in stack.engine.people.values())
    assert not stack.pico.core.mc.any_moving()


async def test_stop_all_brings_every_hand_to_rest(stack):
    await stack.zero_all()
    for slug in stack.engine.people:
        await stack.engine.move_to_angle(slug, 180.0)
    await asyncio.sleep(0.1)
    await stack.engine.stop_all()
    await stack.settle(seconds=4.0)
    assert not stack.pico.core.mc.any_moving()


async def test_state_survives_a_controller_restart(stack):
    """Zero offsets and the last fix are the expensive things to lose."""
    await stack.zero_all()
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await stack.settle()
    await stack.engine.stop()

    from clockd.bus import MemoryBus
    from clockd.engine import Engine
    from clockd.eventlog import EventLog
    from clockd.store import Store

    store = Store(stack.config.var_path("state.json"))
    engine = Engine(stack.config, MemoryBus(), stack.link, store, EventLog(100), sim=True)
    state = engine.state("austin")
    assert state.verified
    assert state.distance_km == pytest.approx(1107, abs=1)
    assert state.target_deg == pytest.approx(254.8, abs=0.1)


async def test_switching_a_source_off_stops_its_fixes(stack):
    await stack.zero_all()
    stack.engine.set_source_enabled("owntracks", False)
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await asyncio.sleep(0.3)
    assert stack.engine.state("austin").accepted == 0

    stack.engine.set_source_enabled("owntracks", True)
    await stack.publish_owntracks("austin", "iphone", *BOULDER, acc=15)
    await asyncio.sleep(0.3)
    assert stack.engine.state("austin").accepted == 1


async def test_a_scenario_walks_a_hand_across_the_dial(stack):
    """'Evan flies home' ends at 12 o'clock, having crossed most of the dial."""
    from clockd.scenarios import run_scenario

    await stack.zero_all()
    await run_scenario(stack.engine, "evan_flies_home", seconds=1.0)
    await stack.settle(seconds=10.0)

    state = stack.engine.state("evan")
    assert state.distance_km == pytest.approx(0.0, abs=1.0)
    assert state.target_deg == pytest.approx(0.0, abs=0.1)
    assert state.actual_deg == pytest.approx(0.0, abs=0.6)


async def test_every_hardware_action_is_logged(stack):
    await stack.zero_all()
    await stack.engine.jog("evan", 5.0)
    await stack.engine.move_to_angle("evan", 100.0)
    await stack.engine.stop_all()

    actions = [e.message for e in stack.events.events if e.category == "action"]
    assert any("zero set here" in m for m in actions)
    assert any("jog" in m for m in actions)
    assert any("move to" in m for m in actions)
    assert any("STOP ALL" in m for m in actions)
    assert all(e.at > 0 for e in stack.events.events)


async def test_a_bad_payload_is_logged_and_does_not_crash_anything(stack):
    await stack.bus.publish("clock/location/austin", b"{ not json")
    await asyncio.sleep(0.2)
    errors = [e.message for e in stack.events.events if e.category == "error"]
    assert any("rejected payload" in m for m in errors)
    assert stack.link.connected


async def test_the_simulator_and_the_firmware_share_one_planner(stack):
    """If this import ever diverges, the simulator stops proving anything."""
    import motion

    from clockd.picolib import motion as controller_motion

    assert stack.pico.core.mc.__class__ is motion.MotionController
    assert controller_motion is motion
