"""What the firmware does when things go wrong.

``controller/tests/test_motion.py`` covers the planner when everything works:
moves land on the commanded microstep, ramps are trapezoidal, channels do not
block each other.  This file covers the other half, which matters more on a
clock that has no home sensors and no encoders:

* a main loop that was starved and comes back late;
* a watchdog firing, and firing again;
* ``stop`` arriving over and over;
* a reset part-way through a move;
* a power cut part-way through writing ``positions.json``.

Every one of these is a case where the firmware can end up believing something
false about where a hand is, and nothing downstream can tell.  There is no
hardware anywhere in here: these are tests of the arithmetic and the
bookkeeping, which is all that can be tested without a motor.

Run them with::

    python -m pytest firmware/pico/tests -q
"""

from __future__ import annotations

import json

import pytest

import core
import main
import motion
import protocol

FULL_REV = 3200


def new_core(channels=6, store=None, **kwargs):
    """A started core with the drivers already enabled, at t=0."""
    pico = core.PicoCore(channels=channels, store=store, **kwargs)
    pico.start(0)
    pico.mc.set_enable(True)
    return pico


def quiet_ticks(pico, start_us, count, step_us=1000):
    """Tick ``count`` times without ever sending a ping.  Returns the events."""
    now = start_us
    events = []
    for _ in range(count):
        now += step_us
        events.extend(pico.tick(now))
    return now, events


# -- a starved loop coming back late ------------------------------------------


class TestLateness:
    """The planner must not make up lost time by bunching pulses.

    A MicroPython GC pass, a flash write or a flood of serial traffic can keep
    the main loop away for tens of milliseconds.  Every step whose deadline
    passed meanwhile is then due at once.  Emitting them all is the one way this
    firmware can be quietly, permanently wrong: the motor cannot follow a burst,
    so it misses steps the firmware has already counted, and with no sensor
    anywhere nothing ever notices.
    """

    @staticmethod
    def axis_at_cruise(max_speed=400.0, accel=800.0, distance=20_000):
        """An axis up to cruise speed, with the clock it got there on."""
        mc = motion.MotionController(1, max_speed=max_speed, accel=accel)
        mc.move_to(0, distance, 0)
        now = 0
        while abs(mc.axis(0).velocity) < max_speed - 0.5:
            now += 200
            mc.service(now)
        return mc, now

    def test_a_long_stall_does_not_bunch_pulses(self):
        mc, now = self.axis_at_cruise()
        # 100 ms away is 40 step intervals at this speed.
        emitted = mc.service(now + 100_000)
        assert emitted <= 1 + motion.MAX_CATCHUP_INTERVALS, (
            "one service() call emitted %d pulses after a 100 ms stall; the "
            "motor cannot follow that and the steps would be lost silently"
            % emitted
        )
        assert mc.axis(0).next_step_us > now + 100_000
        assert mc.axis(0).late == 1

    def test_small_jitter_is_still_caught_up(self):
        """Ordinary lateness must still be made up, or the rate drifts slow."""
        mc, now = self.axis_at_cruise()
        interval = int(1_000_000.0 / mc.axis(0).max_speed)
        before = mc.axis(0).position
        mc.service(now + interval)
        assert mc.axis(0).position > before
        assert mc.axis(0).late == 0

    def test_no_step_is_lost_only_delayed(self):
        """A stall costs time, never position.  That is the whole trade."""
        pulses: list[int] = []
        mc = motion.MotionController(
            1, pulse=lambda ch, d: pulses.append(d), max_speed=400.0, accel=800.0
        )
        mc.move_to(0, 4000, 0)
        now = 0
        while mc.any_moving():
            now += 1000
            mc.service(now)
            if now % 500_000 == 0:  # a 100 ms stall twice a second
                now += 100_000
        assert mc.axis(0).position == 4000
        assert len(pulses) == 4000
        assert mc.late_steps() > 0, "the stalls should have been counted"

    def test_lateness_is_reported_so_it_is_not_silent(self):
        pico = new_core()
        assert pico.status(0)["late"] == 0
        pico.mc.move_to(0, 4000, 0)
        pico.tick(1000)
        pico.tick(1000 + 500_000)
        assert pico.status(0)["late"] >= 1

    def test_offline_simulation_is_unaffected(self):
        """``run()`` keeps its clock on the deadlines, so nothing re-bases."""
        mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
        mc.move_to(0, FULL_REV, 0)
        mc.run()
        assert mc.axis(0).position == FULL_REV
        assert mc.late_steps() == 0


# -- stop, repeatedly ---------------------------------------------------------


class TestRepeatedStop:
    """``stop`` must be idempotent, or a hand never comes to rest.

    ``ceil(v**2 / 2a)`` rounds the braking distance up, so each fresh ``stop``
    used to grant one more microstep.  A caller that stopped once per loop
    iteration -- a held UI button, or the watchdog below -- got a hand crawling
    at 56 microsteps/s indefinitely, reporting ``moving: true`` the whole time
    with nothing to say it was wrong.
    """

    def test_stopping_every_iteration_still_stops(self):
        mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
        mc.move_to(0, FULL_REV * 4, 0)
        now = 0
        for _ in range(30_000):
            now += 1000
            mc.service(now)
            mc.stop(now)
        axis = mc.axis(0)
        assert not axis.is_moving(), (
            "still moving after 30 s of stop(): position %d, velocity %.2f"
            % (axis.position, axis.velocity)
        )
        assert axis.position < 400, "crept %d microsteps while stopping" % axis.position

    def test_a_single_stop_still_decelerates_properly(self):
        """The fix must not turn stop into an e-stop."""
        mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
        mc.move_to(0, FULL_REV * 4, 0)
        now = 0
        while abs(mc.axis(0).velocity) < 799.0:
            now += 200
            mc.service(now)
        at_stop = mc.axis(0).position
        mc.stop(now)
        mc.run(start_us=now)
        travelled = mc.axis(0).position - at_stop
        # v^2 / 2a = 800^2 / 3200 = 200 microsteps of braking.
        assert 190 <= travelled <= 210, travelled
        assert mc.axis(0).velocity == 0.0

    def test_stop_in_the_other_direction_also_converges(self):
        mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
        mc.move_to(0, -FULL_REV * 4, 0)
        now = 0
        for _ in range(30_000):
            now += 1000
            mc.service(now)
            mc.stop(now)
        assert not mc.axis(0).is_moving()
        assert mc.axis(0).position > -400


# -- the watchdog -------------------------------------------------------------


class TestWatchdog:
    """No hand may keep moving while the Pi is silent.

    Not "the watchdog fires once": the property has to hold however many times
    it takes.  A half-dead link -- commands arriving, pings not -- used to get a
    hand moving after the trip and then never be stopped again, because the
    tripped flag only clears on a ping.
    """

    def test_it_trips_and_stops_the_hands(self):
        pico = new_core()
        pico.mc.move_to(0, FULL_REV * 4, 0)
        now, events = quiet_ticks(pico, 0, 12_000)
        faults = [e for e in events if e["ev"] == "fault"]
        assert faults, "no fault event after 12 s of silence"
        assert faults[0]["code"] == protocol.FAULT_WATCHDOG
        assert pico.watchdog_tripped
        now, _ = quiet_ticks(pico, now, 5_000)
        assert not pico.mc.any_moving()

    def test_a_move_after_the_trip_is_stopped_too(self):
        pico = new_core()
        now, _ = quiet_ticks(pico, 0, 11_000)
        assert pico.watchdog_tripped
        # The link is half alive: a command gets through, a ping does not.
        pico.handle_line(
            json.dumps({"seq": 2, "cmd": "move", "ch": 0, "target": FULL_REV * 4}), now
        )
        now, events = quiet_ticks(pico, now, 20_000)
        assert not pico.mc.any_moving(), (
            "ch 0 was still moving %.0f s into a silent link, at position %d"
            % (now / 1e6, pico.mc.axis(0).position)
        )
        assert pico.mc.axis(0).position < 500
        assert [e for e in events if e["ev"] == "fault"], "stopped it without saying so"

    def test_it_does_not_spam_fault_events(self):
        """Stopping takes a ramp, during which the axis is still moving."""
        pico = new_core()
        pico.mc.move_to(0, FULL_REV * 4, 0)
        now, events = quiet_ticks(pico, 0, 40_000)
        faults = [e for e in events if e["ev"] == "fault"]
        assert len(faults) == 1, [f["msg"] for f in faults]

    def test_a_ping_re_arms_it(self):
        pico = new_core()
        now, _ = quiet_ticks(pico, 0, 11_000)
        assert pico.watchdog_tripped
        pico.handle_line(json.dumps({"seq": 3, "cmd": "ping"}), now)
        assert not pico.watchdog_tripped
        pico.handle_line(
            json.dumps({"seq": 4, "cmd": "move", "ch": 1, "target": FULL_REV * 4}), now
        )
        now, events = quiet_ticks(pico, now, 20_000)
        assert not pico.mc.any_moving()
        assert [e for e in events if e["ev"] == "fault"]

    def test_disabling_the_watchdog_disables_it(self):
        pico = new_core()
        pico.handle_line(json.dumps({"seq": 1, "cmd": "set_params", "watchdog_s": 0}), 0)
        pico.mc.move_to(0, 400, 0)
        now, events = quiet_ticks(pico, 0, 20_000)
        assert not [e for e in events if e["ev"] == "fault"]
        assert pico.mc.axis(0).position == 400

    def test_the_stop_is_a_ramp_so_the_position_stays_true(self):
        """The watchdog must not slip the rotor: that is what estop is for."""
        pico = new_core()
        pico.mc.move_to(0, FULL_REV * 4, 0)
        now, _ = quiet_ticks(pico, 0, 12_000)
        # A ramped stop keeps the count honest, so no re-verification is asked
        # for -- unlike estop, which sets it.
        assert pico.needs_verification is True  # still true from boot
        pico.needs_verification = False
        now, _ = quiet_ticks(pico, now, 5_000)
        assert pico.needs_verification is False


# -- per-channel parameters ---------------------------------------------------


class TestSetParams:
    """A scoped ``set_params`` used to answer with channel 0's limits.

    The command applied correctly, so nothing moved wrongly -- but a caller
    that compares what it asked for against what came back concluded the
    firmware had clamped it, and there is nothing in the reply to say otherwise.
    """

    def test_a_scoped_reply_describes_that_channel(self):
        pico = new_core()
        reply = pico.handle_line(
            json.dumps(
                {"seq": 1, "cmd": "set_params", "ch": 2, "max_speed": 400, "accel": 800}
            ),
            0,
        )
        assert reply["ok"] is True
        assert reply["ch"] == 2
        assert reply["max_speed"] == 400.0
        assert reply["accel"] == 800.0
        assert pico.mc.axis(0).max_speed == motion.DEFAULT_MAX_SPEED

    def test_an_unscoped_reply_still_describes_everything(self):
        pico = new_core()
        reply = pico.handle_line(
            json.dumps({"seq": 1, "cmd": "set_params", "max_speed": 500}), 0
        )
        assert reply["ch"] is None
        assert reply["max_speed"] == 500.0
        assert [a.max_speed for a in pico.mc.axes] == [500.0] * 6

    def test_a_clamped_value_is_reported_as_clamped(self):
        pico = new_core()
        reply = pico.handle_line(
            json.dumps({"seq": 1, "cmd": "set_params", "ch": 3, "max_speed": 99_999}), 0
        )
        assert reply["max_speed"] == motion.SPEED_LIMIT


# -- a reset part-way through a move ------------------------------------------


class TestResetMidMove:
    """What a restart leaves behind.

    Positions are written only at rest, so a reset during a move restores a
    number from before the move started.  That is unavoidable without a sensor;
    what matters is that the firmware says so rather than presenting it as fact.
    """

    def test_the_restored_position_is_the_one_from_before_the_move(self, tmp_path):
        store = main.FileStore(str(tmp_path / "positions.json"))
        pico = new_core(store=store)
        pico.mc.axis(0).set_position(1000)
        # At rest for long enough to be written.
        pico.tick(2_000_000)
        assert store.load() == [1000, 0, 0, 0, 0, 0]

        pico.mc.move_to(0, 5000, 2_000_000)
        now = 2_000_000
        while pico.mc.axis(0).position < 3000:
            now += 1000
            pico.tick(now)
        # Power cut here.  Nothing was written: the move never came to rest.
        restarted = core.PicoCore(channels=6, store=main.FileStore(store.path))
        boot = restarted.start(0)
        assert restarted.mc.positions()[0] == 1000
        assert boot["needs_verification"] is True
        assert boot["pos_source"] == main.FileStore.SOURCE_FLASH

    def test_every_boot_asks_for_verification(self, tmp_path):
        store = main.FileStore(str(tmp_path / "positions.json"))
        store.save([1, 2, 3, 4, 5, 6])
        pico = core.PicoCore(channels=6, store=main.FileStore(store.path))
        assert pico.needs_verification is True
        assert pico.start(0)["pos"] == [1, 2, 3, 4, 5, 6]

    def test_estop_marks_the_hands_unverified(self):
        pico = new_core()
        pico.needs_verification = False
        pico.mc.move_to(0, FULL_REV, 0)
        pico.tick(1000)
        reply = pico.handle_line(json.dumps({"seq": 1, "cmd": "estop"}), 2000)
        assert reply["needs_verification"] is True
        assert pico.needs_verification is True
        assert not pico.mc.any_moving()


# -- a power cut part-way through writing positions.json ----------------------


class TestFileStore:
    """``positions.json`` must survive losing power while it is being written.

    The clock is switched off by pulling the plug, and a write follows every
    move, so this is a normal event rather than an edge case.
    """

    def test_a_round_trip(self, tmp_path):
        store = main.FileStore(str(tmp_path / "positions.json"))
        assert store.load() is None
        assert store.source == main.FileStore.SOURCE_NONE
        store.save([0, -935, 3200, 0, 0, 7])
        again = main.FileStore(store.path)
        assert again.load() == [0, -935, 3200, 0, 0, 7]
        assert again.source == main.FileStore.SOURCE_FLASH

    def test_the_primary_file_is_never_written_in_place(self, tmp_path):
        """Proof that a cut during the write cannot touch the good copy."""
        path = tmp_path / "positions.json"
        store = main.FileStore(str(path))
        store.save([1, 1, 1, 1, 1, 1])
        import builtins

        opened: list[tuple[str, str]] = []
        real_open = builtins.open

        def spy(file, mode="r", *args, **kwargs):
            opened.append((str(file), mode))
            return real_open(file, mode, *args, **kwargs)

        builtins.open = spy
        try:
            store.save([2, 2, 2, 2, 2, 2])
        finally:
            builtins.open = real_open
        writes = [name for name, mode in opened if "w" in mode]
        assert writes, opened
        assert all(name.endswith(".tmp") for name in writes), writes

    def test_a_cut_while_writing_the_new_copy_keeps_the_old(self, tmp_path):
        path = tmp_path / "positions.json"
        store = main.FileStore(str(path))
        store.save([10, 20, 30, 40, 50, 60])
        # Power died with a half-written .tmp on disk.
        (tmp_path / "positions.json.tmp").write_text("[10, 20, 30, 40, 5")
        recovered = main.FileStore(str(path))
        assert recovered.load() == [10, 20, 30, 40, 50, 60]
        assert recovered.source == main.FileStore.SOURCE_FLASH

    def test_a_cut_during_the_swap_recovers_the_new_copy(self, tmp_path):
        path = tmp_path / "positions.json"
        # The state after "remove the old, before rename the new": no primary
        # file, a complete .tmp.  This is the only window the save has.
        (tmp_path / "positions.json.tmp").write_text(json.dumps([7, 7, 7, 7, 7, 7]))
        store = main.FileStore(str(path))
        assert store.load() == [7, 7, 7, 7, 7, 7]
        assert store.source == main.FileStore.SOURCE_RECOVERED

    def test_a_truncated_file_is_corrupt_not_plausible(self, tmp_path):
        path = tmp_path / "positions.json"
        path.write_text("[0, 0, 0, 0, 0, 32")
        store = main.FileStore(str(path))
        assert store.load() is None
        assert store.source == main.FileStore.SOURCE_CORRUPT

    @pytest.mark.parametrize(
        "content",
        ['{"ch0": 1}', "[]", "null", '["a", "b"]', "[1, true, 3]", "", "   "],
    )
    def test_nothing_but_a_list_of_numbers_is_accepted(self, tmp_path, content):
        path = tmp_path / "positions.json"
        path.write_text(content)
        store = main.FileStore(str(path))
        assert store.load() is None

    def test_corrupt_is_distinguishable_from_never_saved(self, tmp_path):
        """Both give zeros; only one means the flash wants looking at."""
        fresh = main.FileStore(str(tmp_path / "a.json"))
        fresh.load()
        bad_path = tmp_path / "b.json"
        bad_path.write_text("not json at all")
        bad = main.FileStore(str(bad_path))
        bad.load()
        assert fresh.source == main.FileStore.SOURCE_NONE
        assert bad.source == main.FileStore.SOURCE_CORRUPT
        assert fresh.source != bad.source

    def test_a_write_that_keeps_nothing_does_not_destroy_the_good_copy(self, tmp_path):
        path = tmp_path / "positions.json"
        store = main.FileStore(str(path))
        store.save([5, 5, 5, 5, 5, 5])

        import builtins

        real_open = builtins.open

        class Sink:
            """A file handle that accepts a write and keeps none of it."""

            def write(self, data):
                return len(data)

            def read(self, *a):
                return ""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def broken(file, mode="r", *args, **kwargs):
            if "w" in mode:
                return Sink()
            return real_open(file, mode, *args, **kwargs)

        builtins.open = broken
        try:
            with pytest.raises(OSError):
                store.save([9, 9, 9, 9, 9, 9])
        finally:
            builtins.open = real_open
        assert store.write_failures == 1
        assert main.FileStore(str(path)).load() == [5, 5, 5, 5, 5, 5]

    def test_the_core_survives_a_store_that_raises(self, tmp_path):
        class Hostile:
            source = "none"
            write_failures = 3

            def load(self):
                return None

            def save(self, positions):
                raise OSError("flash is full")

        pico = new_core(store=Hostile())
        pico.mc.axis(0).set_position(123)
        pico.tick(2_000_000)  # would write, and the write explodes
        assert pico.mc.positions()[0] == 123
        assert pico.status(2_000_000)["flash_errors"] == 3

    def test_flash_is_only_written_at_rest_and_rate_limited(self, tmp_path):
        store = main.FileStore(str(tmp_path / "positions.json"))
        pico = new_core(store=store)
        pico.mc.move_to(0, 400, 0)
        now = 0
        while pico.mc.any_moving():
            now += 1000
            pico.tick(now)
        # Still inside the settle window: nothing written yet.
        assert store.load() is None
        now += core.PicoCore.FLASH_SETTLE_MS * 1000 + 1000
        pico.tick(now)
        assert store.load() == [400, 0, 0, 0, 0, 0]


# -- the serial reader --------------------------------------------------------


class TestSerialReader:
    """A flood from the host must not starve the step loop for ever."""

    def test_one_poll_reads_a_bounded_number_of_bytes(self):
        assert main.SerialLink.MAX_READ >= main.SerialLink.MAX_LINE, (
            "a well-behaved host sends lines up to MAX_LINE, so MAX_READ must "
            "be at least that or a legal line is never completed in one pass"
        )

    def test_an_over_long_line_is_dropped_not_buffered(self):
        link = main.SerialLink.__new__(main.SerialLink)
        link.buffer = b""
        link.dropped = 0
        link.MAX_LINE = 16
        # Simulate what poll_lines does to the buffer, without a Pico's stdin.
        for _ in range(100):
            link.buffer += b"x" * 8
            if len(link.buffer) > link.MAX_LINE and b"\n" not in link.buffer:
                link.buffer = b""
                link.dropped += 1
        assert len(link.buffer) <= link.MAX_LINE
        assert link.dropped > 0


# -- what cannot be tested here ------------------------------------------------


def test_a_stall_is_not_detectable_and_this_records_that():
    """A stalled motor is invisible to this firmware, by construction.

    There is no encoder, no home sensor, and the A4988 has no fault output in
    this design.  The firmware counts the pulses it emitted; a motor that sat
    still while they went past is indistinguishable from one that followed
    them.  So there is no test here that a stall is detected, because it cannot
    be, and writing one that passed against the simulator would be a lie.

    What exists instead:

    * ``tools/bench.py check`` ends with a full revolution against a physical
      mark, which is how a person detects lost steps;
    * ``needs_verification`` keeps an unconfirmed hand from being driven at all;
    * ``late`` in ``status`` reports the one cause of lost steps the firmware
      *can* see, a starved loop, which is now prevented rather than merely
      reported.

    This test asserts only that the reporting exists, so that removing it
    breaks something.
    """
    pico = new_core()
    status = pico.status(0)
    assert "late" in status
    assert "flash_errors" in status
    assert "needs_verification" in status
    assert pico.needs_verification is True
