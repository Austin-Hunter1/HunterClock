"""Motion planner tests: exact arrival, limits, shortest path, six at once.

The planner under test is ``firmware/pico/motion.py``, imported straight from
the firmware tree, so passing here means the Pico and the simulator behave the
same way.
"""

from __future__ import annotations

import math

import pytest

import motion
from clockd import geo

STEPS_PER_REV = 3200


def trace_move(target, max_speed=800.0, accel=1600.0, start=0):
    """Run one axis to ``target`` and return (controller, trace)."""
    mc = motion.MotionController(1, max_speed=max_speed, accel=accel)
    mc.axis(0).set_position(start)
    mc.move_to(0, target, 0)
    trace: list[tuple] = []
    mc.run(trace=trace)
    return mc, trace


def speeds(trace, channel=0):
    """Instantaneous speeds (steps/s) implied by consecutive step times."""
    times = [t for t, ch, *_ in trace if ch == channel]
    return [1_000_000.0 / (b - a) for a, b in zip(times, times[1:], strict=False) if b > a]


# -- exact arrival -----------------------------------------------------------


@pytest.mark.parametrize("target", [1, 2, 3, 10, 99, 800, 1600, 3200, 10_000, -1, -7, -3200])
def test_move_lands_exactly_on_target(target):
    mc, trace = trace_move(target)
    assert mc.axis(0).position == target
    assert len(trace) == abs(target)
    assert not mc.any_moving()
    assert mc.axis(0).velocity == 0.0
    assert mc.axis(0).next_step_us is None


def test_zero_length_move_emits_no_steps():
    mc, trace = trace_move(0)
    assert trace == []
    assert not mc.any_moving()


def test_every_step_is_one_microstep_in_one_direction():
    _mc, trace = trace_move(500)
    directions = {d for _t, _ch, d, *_ in trace}
    assert directions == {1}
    positions = [p for *_rest, p, _v in trace]
    assert positions == list(range(1, 501))


# -- limits ------------------------------------------------------------------


def test_never_exceeds_max_speed():
    _mc, trace = trace_move(5000, max_speed=800.0, accel=1600.0)
    assert max(speeds(trace)) <= 800.0 + 1e-6


def test_reaches_cruise_speed_on_a_long_move():
    _mc, trace = trace_move(5000, max_speed=800.0, accel=1600.0)
    assert max(speeds(trace)) == pytest.approx(800.0, rel=1e-3)


def test_acceleration_is_respected():
    """Speed never jumps by more than one step's worth of acceleration."""
    accel = 1600.0
    _mc, trace = trace_move(4000, max_speed=800.0, accel=accel)
    vs = [abs(v) for *_r, v in trace]
    for a, b in zip(vs, vs[1:], strict=False):
        # v2**2 - v1**2 <= 2 * a * 1 step, with a little float slack.
        assert b * b - a * a <= 2.0 * accel * 1.000001 + 1e-6


def test_profile_is_trapezoidal():
    """Speed rises, holds, falls -- and is symmetric end to end."""
    _mc, trace = trace_move(4000, max_speed=800.0, accel=1600.0)
    vs = [abs(v) for *_r, v in trace]
    peak = vs.index(max(vs))
    assert 0 < peak < len(vs) - 1
    rising = vs[1 : peak + 1]
    assert rising == sorted(rising)
    # The last steps are the deceleration ramp.
    tail = vs[-10:]
    assert tail == sorted(tail, reverse=True)


def test_short_move_is_triangular_and_never_reaches_cruise():
    """50 steps at 1600 steps/s^2 cannot get near 800 steps/s."""
    _mc, trace = trace_move(50, max_speed=800.0, accel=1600.0)
    peak = max(abs(v) for *_r, v in trace)
    assert peak < 800.0
    # Triangular peak is sqrt(a * n) for n steps.
    assert peak == pytest.approx(math.sqrt(1600.0 * 50), rel=0.15)


def test_duration_is_close_to_the_analytic_trapezoid():
    """A 3200-step move at 800 steps/s, 1600 steps/s^2: ramp 0.5 s each end."""
    mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
    mc.move_to(0, 3200, 0)
    end_us = mc.run()
    ramp_s = 800.0 / 1600.0
    ramp_steps = 0.5 * 1600.0 * ramp_s**2
    cruise_s = (3200 - 2 * ramp_steps) / 800.0
    expected = 2 * ramp_s + cruise_s
    assert end_us / 1e6 == pytest.approx(expected, rel=0.08)


def test_set_params_clamps_to_the_safe_ceiling():
    axis = motion.Axis(0)
    axis.set_params(max_speed=1e9, accel=1e9)
    assert axis.max_speed == motion.SPEED_LIMIT
    assert axis.accel == motion.ACCEL_LIMIT
    axis.set_params(max_speed=-5, accel=0)
    assert axis.max_speed == motion.MIN_SPEED
    assert axis.accel == 1.0


def test_faster_params_make_a_move_quicker():
    slow = motion.MotionController(1, max_speed=400.0, accel=1600.0)
    slow.move_to(0, 3200, 0)
    slow_us = slow.run()
    fast = motion.MotionController(1, max_speed=1600.0, accel=1600.0)
    fast.move_to(0, 3200, 0)
    fast_us = fast.run()
    assert fast_us < slow_us


# -- direction changes and shortest path -------------------------------------


def test_reversal_brakes_before_going_back():
    """Retargeting mid-flight overshoots and returns; it never teleports."""
    mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
    mc.move_to(0, 4000, 0)
    trace: list[tuple] = []
    # Run partway: 1 second of simulated time.
    now = 0
    while True:
        due = mc.next_deadline()
        if due is None or due > 1_000_000:
            break
        now = due
        mc.service(now)
    assert mc.axis(0).velocity > 0
    mc.move_to(0, 0, now)
    mc.run(start_us=now, trace=trace)
    assert mc.axis(0).position == 0
    forward = [d for _t, _ch, d, *_ in trace if d > 0]
    backward = [d for _t, _ch, d, *_ in trace if d < 0]
    assert forward, "should coast forward while braking"
    assert backward, "then come back"
    # It braked first: every forward step precedes every backward step.
    dirs = [d for _t, _ch, d, *_ in trace]
    assert dirs == sorted(dirs, reverse=True)


def test_shortest_path_across_twelve_oclock_is_a_short_forward_move():
    """The spec's case, in steps: 324 deg -> 2 deg is +38 deg, not -322 deg."""
    per_deg = STEPS_PER_REV / 360.0
    current_deg = 324.0
    current = round(current_deg * per_deg)
    target_deg = geo.shortest_path_target(current_deg, 2.0)
    target = round(target_deg * per_deg)
    mc = motion.MotionController(1)
    mc.axis(0).set_position(current)
    mc.move_to(0, target, 0)
    trace: list[tuple] = []
    mc.run(trace=trace)
    assert len(trace) == pytest.approx(38 * per_deg, abs=2)
    assert all(d == 1 for _t, _ch, d, *_ in trace)
    assert mc.axis(0).position == target


def test_hands_may_turn_without_limit():
    """There is no cable wrap, so ten turns in one direction is legal."""
    mc = motion.MotionController(1, max_speed=4000.0, accel=20000.0)
    mc.move_to(0, 10 * STEPS_PER_REV, 0)
    mc.run()
    assert mc.axis(0).position == 10 * STEPS_PER_REV


# -- stop, estop, set_pos ----------------------------------------------------


def test_stop_decelerates_and_does_not_overshoot_far():
    mc = motion.MotionController(1, max_speed=800.0, accel=1600.0)
    mc.move_to(0, 100_000, 0)
    now = 0
    while True:
        due = mc.next_deadline()
        if due is None or due > 2_000_000:
            break
        now = due
        mc.service(now)
    at_stop_request = mc.axis(0).position
    speed = abs(mc.axis(0).velocity)
    mc.stop(now)
    mc.run(start_us=now)
    travelled = mc.axis(0).position - at_stop_request
    braking_distance = speed * speed / (2 * 1600.0)
    assert travelled == pytest.approx(braking_distance, abs=2)
    assert not mc.any_moving()


def test_stop_all_stops_every_channel():
    mc = motion.MotionController(6, max_speed=800.0, accel=1600.0)
    for ch in range(6):
        mc.move_to(ch, 100_000, 0)
    now = 1_000_000
    mc.service(now)
    mc.stop(now)
    mc.run(start_us=now)
    assert not mc.any_moving()
    assert all(a.position < 100_000 for a in mc.axes)


def test_estop_halts_immediately_and_keeps_position():
    mc = motion.MotionController(1)
    mc.move_to(0, 100_000, 0)
    mc.service(500_000)
    position = mc.axis(0).position
    mc.estop()
    assert mc.axis(0).velocity == 0.0
    assert mc.axis(0).position == position
    assert not mc.any_moving()
    assert mc.next_deadline() is None


def test_set_pos_declares_position_without_moving():
    """Zeroing: the hand is physically at 12, so call this 0 and do not move."""
    mc = motion.MotionController(1)
    mc.axis(0).set_position(12345)
    assert mc.axis(0).position == 12345
    assert mc.axis(0).target == 12345
    assert not mc.any_moving()
    mc.run()
    assert mc.axis(0).position == 12345


def test_stop_on_an_idle_axis_is_a_no_op():
    mc = motion.MotionController(1)
    mc.stop(0)
    assert not mc.any_moving()
    assert mc.next_deadline() is None


def test_jog_is_relative_to_the_target():
    mc = motion.MotionController(1)
    mc.jog(0, 100, 0)
    mc.jog(0, 100, 0)
    assert mc.axis(0).target == 200
    mc.run()
    assert mc.axis(0).position == 200


# -- six channels at once ----------------------------------------------------


def test_six_channels_move_simultaneously():
    """All six ramp together; none waits for another to finish."""
    mc = motion.MotionController(6, max_speed=800.0, accel=1600.0)
    for ch in range(6):
        mc.move_to(ch, 1600, 0)
    trace: list[tuple] = []
    mc.run(trace=trace)
    assert all(a.position == 1600 for a in mc.axes)
    # In the first 100 ms every channel has stepped.
    early = {ch for t, ch, *_ in trace if t <= 100_000}
    assert early == set(range(6))


def test_a_long_move_does_not_delay_a_short_one():
    mc = motion.MotionController(6, max_speed=800.0, accel=1600.0)
    mc.move_to(0, 20_000, 0)  # ~25 s
    mc.move_to(1, 100, 0)  # well under a second
    trace: list[tuple] = []
    mc.run(trace=trace)
    ch1_last = max(t for t, ch, *_ in trace if ch == 1)
    ch0_last = max(t for t, ch, *_ in trace if ch == 0)
    assert ch1_last < 1_000_000
    assert ch0_last > 20_000_000


def test_channels_keep_independent_positions_and_limits():
    mc = motion.MotionController(6)
    mc.set_params(max_speed=200.0, channel=3)
    assert mc.axis(3).max_speed == 200.0
    assert mc.axis(0).max_speed == motion.DEFAULT_MAX_SPEED
    for ch in range(6):
        mc.move_to(ch, (ch + 1) * 100, 0)
    mc.run()
    assert mc.positions() == [100, 200, 300, 400, 500, 600]


def test_service_respects_its_pulse_budget():
    """One service() call cannot hog the loop and starve the serial reader."""
    mc = motion.MotionController(6, max_speed=4000.0, accel=20000.0)
    for ch in range(6):
        mc.move_to(ch, 50_000, 0)
    emitted = mc.service(10_000_000, budget=10)
    assert emitted == 10


def test_pulse_callback_sees_every_step_with_its_direction():
    seen: list[tuple[int, int]] = []
    mc = motion.MotionController(2, pulse=lambda ch, d: seen.append((ch, d)))
    mc.move_to(0, 5, 0)
    mc.move_to(1, -3, 0)
    mc.run()
    assert seen.count((0, 1)) == 5
    assert seen.count((1, -1)) == 3
    assert len(seen) == 8


def test_enable_callback_is_wired_to_the_shared_line():
    states: list[bool] = []
    mc = motion.MotionController(6, set_enable=states.append)
    mc.set_enable(True)
    mc.set_enable(False)
    assert states == [True, False]
    assert mc.enabled is False


def test_unknown_channel_raises():
    mc = motion.MotionController(6)
    with pytest.raises(IndexError):
        mc.axis(6)
    with pytest.raises(IndexError):
        mc.axis(-1)
