"""Tests for ``tools/bench.py``, the bench bring-up tool.

The tool is the thing a person will be holding when a motor first turns, so the
parts that have to be right are not the printing: they are that it never leaves
the drivers energised, that it refuses to enable anything without being told
to, and that it notices a Pico that restarted underneath it instead of
reporting a position that came from flash.

Everything here runs against ``tools/pico_sim.py``.  That proves the tool's
logic and proves nothing at all about a motor.
"""

from __future__ import annotations


import pytest

import motion
from tools import bench
from tools.pico_sim import MonotonicClock, SimPico

# Fast enough that a test is not sitting through a real-time ramp.
FAST = ["--speed", "4000", "--accel", "20000"]


def run(*argv, expect=0):
    """Run the tool against the simulator and return its exit status."""
    status = bench.main(["--port", "sim", *argv])
    assert status == expect, "expected exit %d, got %d" % (expect, status)
    return status


@pytest.fixture
def link():
    """A :class:`bench.Link` over a fresh simulator."""
    pico = SimPico(channels=6, clock=MonotonicClock(1.0))
    with bench.Link(pico, "simulator") as value:
        value.pico = pico  # for the tests to look inside
        yield value


# -- the read-only commands ---------------------------------------------------


def test_info_touches_nothing(capsys):
    run("info")
    out = capsys.readouterr().out
    assert "protocol 1" in out
    assert "released" in out
    # The two things a person must not forget are on the page.
    assert "no home sensors" in out
    assert "Austin" in out and "GP10" in out


def test_pos_reports_every_channel(capsys):
    run("pos")
    out = capsys.readouterr().out
    for person in ("Becky", "Evan", "Elijah", "Caleb", "Austin", "Micah"):
        assert person in out


def test_off_releases_the_drivers(capsys):
    run("off")
    assert "released" in capsys.readouterr().out


# -- consent ------------------------------------------------------------------


def test_it_will_not_enable_without_being_told_to(capsys, monkeypatch):
    """No terminal and no --yes must mean nothing is energised."""
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    run("step", "--ch", "0", "800", expect=2)
    captured = capsys.readouterr()
    assert "not enabling" in captured.err
    assert "no terminal to ask on" in captured.out


def test_the_enable_warning_says_it_is_all_six(capsys):
    run("-y", *FAST, "step", "--ch", "0", "16")
    out = capsys.readouterr().out
    assert "all six drivers" in out


# -- moving -------------------------------------------------------------------


def test_step_moves_the_channel_it_was_asked_for(capsys):
    run("-y", *FAST, "step", "--ch", "3", "800")
    out = capsys.readouterr().out
    assert "requested      800 microsteps (0.250 rev, 90.0 deg)" in out
    assert "firmware moved 800 microsteps" in out
    assert "pulses emitted 800  (expected 800)" in out
    assert "NOT what was asked for" not in out
    assert "mismatch" not in out


def test_a_negative_step_goes_back(capsys):
    run("-y", *FAST, "step", "--ch", "1", "-480")
    out = capsys.readouterr().out
    assert "firmware moved -480 microsteps" in out
    assert "+0 -> -480" in out


def test_step_releases_the_drivers_afterwards(capsys):
    run("-y", *FAST, "step", "--ch", "0", "16")
    assert "drivers released" in capsys.readouterr().out


def test_keep_enabled_says_so_out_loud(capsys):
    run("-y", *FAST, "step", "--ch", "0", "16", "--keep-enabled")
    out = capsys.readouterr().out
    assert "left ENABLED" in out
    assert "holding" in out


def test_wiggle_returns_to_where_it_started(capsys):
    run("-y", *FAST, "wiggle", "--ch", "5", "--steps", "80", "--times", "2")
    out = capsys.readouterr().out
    assert "pass 2 of 2 done" in out
    assert "Back where it started" in out


def test_check_walks_the_stages_and_signs_off(capsys):
    run("-y", *FAST, "check", "--ch", "2")
    out = capsys.readouterr().out
    for index in range(1, len(bench.CHECK_STAGES) + 1):
        assert "stage %d of %d" % (index, len(bench.CHECK_STAGES)) in out
    assert "did everything asked of it" in out
    # It must end back at zero and with the motor released.
    assert "no longer holds" in out
    # And it must not oversell what it proved.
    assert "does not say the" in out


def test_check_includes_a_full_revolution():
    """The one stage that catches lost steps must be there."""
    assert bench.MICROSTEPS_PER_REV in [steps for steps, _ in bench.CHECK_STAGES]


def test_check_fails_when_the_person_says_it_did_not_move(capsys, monkeypatch):
    answers = iter(["y", "y", "n", "n"])  # pre-flight, enable, "did it move", carry on
    monkeypatch.setattr("builtins.input", lambda *a: next(answers, "n"))
    run(*FAST, "check", "--ch", "0", expect=1)
    out = capsys.readouterr().out
    assert "NOT signed off" in out
    assert "ENABLE is one wire for all six" in out
    assert "no longer holds" in out  # released even on the failure path


# -- a Pico that restarts underneath us ---------------------------------------


def test_a_reset_mid_session_is_an_error_not_a_shrug():
    pico = SimPico(channels=6, clock=MonotonicClock(1.0), reset_after_s=0.4)
    with bench.Link(pico, "simulator") as link:
        link.hello()
        with pytest.raises(bench.PicoReset):
            for _ in range(400):
                link.check_reset()
                link.status()


def test_a_reset_is_caught_even_when_the_boot_event_is_lost():
    """Uptime going backwards is the backstop; one dropped line must not hide it."""
    pico = SimPico(channels=6, clock=MonotonicClock(1.0))
    with bench.Link(pico, "simulator", ping=False) as link:
        link.hello()
        link._dispatch({"seq": 1, "cmd": "ping", "ok": True, "t": 40_000})
        link.check_reset()
        link._dispatch({"seq": 2, "cmd": "ping", "ok": True, "t": 12})
        with pytest.raises(bench.PicoReset):
            link.check_reset()


def test_uptime_going_forwards_is_not_a_reset():
    pico = SimPico(channels=6, clock=MonotonicClock(1.0))
    with bench.Link(pico, "simulator", ping=False) as link:
        for value in (10, 500, 4000, 4000, 90_000):
            link._dispatch({"ev": "pos", "t": value, "pos": [0] * 6, "moving": [False] * 6})
        link.check_reset()


def test_check_reset_is_quiet_when_nothing_happened(link):
    link.hello()
    link.check_reset()


# -- the prediction -----------------------------------------------------------


def test_the_prediction_comes_from_the_firmware_planner():
    """Not a second formula to drift from the first."""
    seconds = bench.predicted_move_s(3200, 800.0, 1600.0)
    mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
    mc.move_to(0, 3200, 0)
    assert seconds == pytest.approx(mc.run(0) / 1e6)
    assert bench.predicted_move_s(0, 800.0, 1600.0) == 0.0


def test_a_move_that_runs_long_is_stopped_and_reported(link):
    """Giving up must stop the hand, not walk away leaving it running."""
    link.hello()
    link.enable(True)
    with pytest.raises(bench.BenchError) as error:
        # 3200 microsteps at 10/s is five minutes; allow it a tenth of a second.
        bench.do_move(link, 0, 3200, 10.0, 20.0, budget_s=0.1)
    assert "still moving" in str(error.value)
    assert "came to rest at" in str(error.value)
    assert not link.status()["moving"][0], "gave up without stopping the hand"


# -- the labels people read at the bench --------------------------------------


def test_every_channel_has_a_label():
    for channel in range(6):
        label = bench.channel_label(channel)
        assert "no entry" not in label
        assert "STEP GP" in label and "DIR GP" in label


def test_the_channel_table_matches_the_firmware_pin_map():
    """The bench labels and the pins the Pico actually drives are one table."""
    import main as firmware

    assert len(bench.CHANNELS) == len(firmware.STEP_DIR_PINS)
    for (ch, _person, _driver, _motor, step, _sp, direction, _dp) in bench.CHANNELS:
        assert (step, direction) == firmware.STEP_DIR_PINS[ch]


def test_describe_steps_speaks_revolutions_and_degrees():
    assert bench.describe_steps(3200) == "3200 microsteps (1.000 rev, 360.0 deg)"
    assert bench.describe_steps(-800) == "-800 microsteps (-0.250 rev, -90.0 deg)"


def test_one_revolution_agrees_with_the_protocol_document():
    assert bench.MICROSTEPS_PER_REV == 200 * 16


# -- the safety net -----------------------------------------------------------


def test_a_bug_in_the_tool_still_releases_the_drivers(monkeypatch):
    released: list[bool] = []

    def exploding(link, args):
        link.enable(True)
        released.clear()
        monkeypatch.setattr(link, "release", lambda: released.append(True))
        raise ZeroDivisionError("a bug in the tool")

    monkeypatch.setitem(bench.HANDLERS, "info", exploding)
    with pytest.raises(ZeroDivisionError):
        bench.main(["--port", "sim", "info"])
    assert released == [True], "the drivers were left energised by a crash"


def test_ctrl_c_releases_the_drivers_and_says_so(monkeypatch, capsys):
    def interrupted(link, args):
        raise KeyboardInterrupt

    monkeypatch.setitem(bench.HANDLERS, "info", interrupted)
    assert bench.main(["--port", "sim", "info"]) == 130
    assert "releasing the drivers" in capsys.readouterr().out


def test_an_unknown_port_fails_without_a_traceback(capsys):
    assert bench.main(["--port", "/dev/does-not-exist", "info"]) == 2
    assert "cannot open" in capsys.readouterr().err


def test_bad_channels_are_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        bench.main(["--port", "sim", "step", "--ch", "9", "100"])
