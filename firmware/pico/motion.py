"""Trapezoidal motion planner for the six clock hands.

This module is deliberately pure Python: no ``machine``, no ``time``, no
imports at all beyond ``math``.  It runs unchanged on

* the Pico under MicroPython (imported by ``main.py``),
* CPython under pytest (``controller/tests/test_motion.py``), and
* ``tools/pico_sim.py``, the fake Pico the controller talks to in ``--sim`` mode,

so the simulator and the firmware cannot disagree about how a move behaves.

Model
-----
Each axis is an independent :class:`Axis` holding an *unwrapped* integer
position in microsteps.  There are no home sensors and no cable wrap, so a
position may be any signed integer and a hand may turn forever in one
direction.

Stepping is event driven.  An axis that is moving exposes
:attr:`Axis.next_step_us`, the absolute time of its next STEP pulse on a
monotonic microsecond clock supplied by the caller.  The caller pulses the pin
and calls :meth:`Axis.take_step`, which advances the position, recomputes the
velocity for the following step, and schedules it.  Because every axis keeps its
own deadline, six axes interleave on one loop without blocking each other and
without any axis waiting for another to finish.

Velocity profile
----------------
At each step boundary the next speed is the smallest of three bounds, the
classic discrete-step trapezoid:

* ``max_speed`` -- the configured cruise ceiling;
* ``sqrt(2 * accel * steps_remaining)`` -- fast enough to still stop *on* the
  target rather than past it, which gives the deceleration ramp;
* ``sqrt(v**2 + 2 * accel)`` -- at most one step's worth of acceleration above
  the current speed, which gives the acceleration ramp.

Because the position is an integer and the second bound reaches zero exactly at
the target, a move always lands on the commanded step -- never one short, never
one past.  A reversal while moving is handled physically: the axis brakes to a
stop along its current direction first, then goes back.
"""

import math

#: Hard ceilings.  ``set_params`` may lower these but never raise them; they
#: exist so a bad command from the Pi cannot ask for a speed the A4988 drivers
#: and the brass tube stack cannot survive.
SPEED_LIMIT = 4000.0  # microsteps/s  (1.25 rev/s at 3200 microsteps/rev)
ACCEL_LIMIT = 20000.0  # microsteps/s^2
MIN_SPEED = 1.0  # microsteps/s; floors the step interval at 1 s

DEFAULT_MAX_SPEED = 800.0
DEFAULT_ACCEL = 1600.0


def clamp(value, low, high):
    """Clamp ``value`` into ``[low, high]``."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def sign(value):
    """Return -1, 0 or 1 with the sign of ``value``."""
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


class Axis:
    """One hand: position, target and velocity, in microsteps.

    Args:
        index: Channel number 0-5, used only for reporting.
        max_speed: Cruise speed in microsteps/s, clamped to :data:`SPEED_LIMIT`.
        accel: Acceleration in microsteps/s^2, clamped to :data:`ACCEL_LIMIT`.
    """

    __slots__ = (
        "index",
        "position",
        "target",
        "velocity",
        "max_speed",
        "accel",
        "next_step_us",
        "steps_taken",
        "direction",
    )

    def __init__(self, index=0, max_speed=DEFAULT_MAX_SPEED, accel=DEFAULT_ACCEL):
        self.index = index
        self.position = 0
        self.target = 0
        self.velocity = 0.0
        self.max_speed = clamp(float(max_speed), MIN_SPEED, SPEED_LIMIT)
        self.accel = clamp(float(accel), 1.0, ACCEL_LIMIT)
        self.next_step_us = None
        self.steps_taken = 0
        self.direction = 1

    # -- configuration ---------------------------------------------------

    def set_params(self, max_speed=None, accel=None):
        """Change the speed/acceleration limits, clamped to the safe ceilings."""
        if max_speed is not None:
            self.max_speed = clamp(float(max_speed), MIN_SPEED, SPEED_LIMIT)
        if accel is not None:
            self.accel = clamp(float(accel), 1.0, ACCEL_LIMIT)

    def set_position(self, position):
        """Declare the current position without moving (used for zeroing).

        Only legal at rest: the caller must stop the axis first.  The target is
        moved with the position so the axis does not immediately run off.
        """
        delta = int(position) - self.position
        self.position = int(position)
        self.target += delta
        if not self.is_moving():
            self.target = self.position

    # -- commands --------------------------------------------------------

    def move_to(self, target, now_us):
        """Command an absolute target in microsteps.

        Args:
            target: Absolute unwrapped microstep position to reach.
            now_us: Current monotonic time in microseconds.
        """
        self.target = int(target)
        if self.target != self.position and self.next_step_us is None:
            # Starting from rest: first step is due immediately.
            self.next_step_us = now_us
            self._update_direction()

    def jog(self, delta, now_us):
        """Move ``delta`` microsteps relative to the *current target*."""
        self.move_to(self.target + int(delta), now_us)

    def stop(self, now_us):
        """Decelerate to a stop at the configured acceleration and hold there.

        Sets the target to the closest reachable stopping point, so the normal
        deceleration ramp does the work.  Already-stopped axes do nothing.
        """
        if not self.is_moving():
            self.target = self.position
            return
        braking_steps = int(math.ceil(self.velocity * self.velocity / (2.0 * self.accel)))
        self.target = self.position + sign(self.velocity) * braking_steps
        if self.next_step_us is None:
            self.next_step_us = now_us

    def estop(self):
        """Drop the velocity to zero at once and hold position.

        This loses synchronisation with the physical hand if it was moving fast
        (the rotor may slip), so the controller marks the hand unverified
        afterwards.  It exists for "something is wrong, stop now".
        """
        self.velocity = 0.0
        self.target = self.position
        self.next_step_us = None

    # -- state -----------------------------------------------------------

    def is_moving(self):
        """True when the axis has somewhere to go or is still carrying speed."""
        return self.position != self.target or self.velocity != 0.0

    def steps_remaining(self):
        """Signed distance to the target in microsteps."""
        return self.target - self.position

    # -- the planner itself ----------------------------------------------

    def _update_direction(self):
        """Latch the DIR value for the step that is about to happen."""
        if self.velocity != 0.0:
            self.direction = 1 if self.velocity > 0 else -1
        else:
            delta = self.target - self.position
            if delta != 0:
                self.direction = 1 if delta > 0 else -1

    def _next_velocity(self):
        """Signed velocity for the next step, in microsteps/s.

        Returns 0.0 when the axis should come to rest.
        """
        delta = self.target - self.position
        speed = abs(self.velocity)
        moving_dir = sign(self.velocity)

        if delta == 0:
            if speed == 0.0:
                return 0.0
            # Overshot or asked to stop here while moving: brake along the
            # current direction.  _braked() may return 0.0, ending the move.
            return moving_dir * self._braked(speed)

        want_dir = sign(delta)
        if moving_dir != 0 and moving_dir != want_dir:
            # Reversal: brake first, keep going the way we are already going.
            return moving_dir * self._braked(speed)

        # Accelerate, cruise or decelerate toward the target.
        v_stop = math.sqrt(2.0 * self.accel * abs(delta))
        v_accel = math.sqrt(speed * speed + 2.0 * self.accel)
        v = min(self.max_speed, v_stop, v_accel)
        if v < MIN_SPEED:
            v = MIN_SPEED
        return want_dir * v

    def _braked(self, speed):
        """Speed after one step of deceleration, from ``speed``."""
        squared = speed * speed - 2.0 * self.accel
        if squared <= 0.0:
            return 0.0
        v = math.sqrt(squared)
        return v if v >= MIN_SPEED else 0.0

    def take_step(self):
        """Advance one microstep and schedule the next.

        The caller must already have produced the STEP pulse in the direction
        given by :attr:`direction`.

        Returns:
            The direction actually stepped (``+1`` or ``-1``), or ``0`` if the
            axis had nothing to do.
        """
        if not self.is_moving():
            self.next_step_us = None
            self.velocity = 0.0
            return 0

        stepped = self.direction
        self.position += stepped
        self.steps_taken += 1

        self.velocity = self._next_velocity()
        if self.velocity == 0.0 and self.position == self.target:
            self.next_step_us = None
            return stepped

        if self.velocity == 0.0:
            # Came to rest mid-move (end of a braking phase): restart the ramp
            # toward whatever the target is now.
            self._update_direction()
            if self.position == self.target:
                self.next_step_us = None
                return stepped
            self.velocity = self._next_velocity()

        self._update_direction()
        interval = int(1_000_000.0 / max(abs(self.velocity), MIN_SPEED))
        self.next_step_us = (self.next_step_us or 0) + max(interval, 1)
        return stepped


class MotionController:
    """The six axes together, plus the shared ENABLE line and the watchdog.

    The class knows nothing about pins; :mod:`main` (on the Pico) and
    :mod:`tools.pico_sim` (on a desktop) supply the two callbacks.

    Args:
        channels: Number of axes, normally 6.
        pulse: ``pulse(channel, direction)`` -- emit one STEP pulse.  May be
            ``None`` in tests, in which case steps are only simulated.
        set_enable: ``set_enable(on)`` -- drive the shared active-low ENABLE.
    """

    def __init__(self, channels=6, pulse=None, set_enable=None,
                 max_speed=DEFAULT_MAX_SPEED, accel=DEFAULT_ACCEL):
        self.axes = [Axis(i, max_speed, accel) for i in range(channels)]
        self._pulse = pulse
        self._set_enable = set_enable
        self.enabled = False
        self.step_count = 0

    # -- access ----------------------------------------------------------

    def __len__(self):
        return len(self.axes)

    def axis(self, channel):
        """Return one axis, raising ``IndexError`` for an unknown channel."""
        if not 0 <= channel < len(self.axes):
            raise IndexError("channel %d out of range" % channel)
        return self.axes[channel]

    def positions(self):
        """Current unwrapped positions, in microsteps, as a list."""
        return [a.position for a in self.axes]

    def any_moving(self):
        """True while at least one axis still has work."""
        for a in self.axes:
            if a.is_moving():
                return True
        return False

    # -- commands --------------------------------------------------------

    def set_enable(self, on):
        """Enable or disable all six drivers (the line is shared, active low)."""
        self.enabled = bool(on)
        if self._set_enable is not None:
            self._set_enable(self.enabled)

    def move_to(self, channel, target, now_us):
        """Absolute move on one channel."""
        self.axis(channel).move_to(target, now_us)

    def jog(self, channel, delta, now_us):
        """Relative move on one channel."""
        self.axis(channel).jog(delta, now_us)

    def stop(self, now_us, channel=None):
        """Decelerate one channel, or all of them when ``channel`` is None."""
        targets = self.axes if channel is None else [self.axis(channel)]
        for a in targets:
            a.stop(now_us)

    def estop(self):
        """Immediate stop on every channel."""
        for a in self.axes:
            a.estop()

    def set_params(self, max_speed=None, accel=None, channel=None):
        """Apply speed/acceleration limits to one or all channels."""
        targets = self.axes if channel is None else [self.axis(channel)]
        for a in targets:
            a.set_params(max_speed, accel)

    # -- the step loop ---------------------------------------------------

    def next_deadline(self):
        """Earliest ``next_step_us`` across the axes, or ``None`` if all at rest."""
        best = None
        for a in self.axes:
            due = a.next_step_us
            if due is not None and (best is None or due < best):
                best = due
        return best

    def service(self, now_us, budget=64):
        """Emit every STEP pulse that is due at or before ``now_us``.

        This is the whole real-time core.  It is called as fast as the main loop
        can manage; between calls the serial reader runs, so a long move never
        starves the link.

        Args:
            now_us: Current monotonic time in microseconds.
            budget: Maximum pulses to emit in one call, so that a large backlog
                (after a GC pause, say) cannot block the serial reader either.

        Returns:
            The number of STEP pulses emitted.
        """
        emitted = 0
        while emitted < budget:
            moved = False
            for a in self.axes:
                due = a.next_step_us
                if due is None or due > now_us:
                    continue
                direction = a.direction
                if self._pulse is not None:
                    self._pulse(a.index, direction)
                a.take_step()
                emitted += 1
                self.step_count += 1
                moved = True
                if emitted >= budget:
                    break
            if not moved:
                break
        return emitted

    # -- offline simulation (tests and scenario replay) ------------------

    def run(self, start_us=0, max_us=120_000_000, trace=None):
        """Run every axis to rest in simulated time.

        Used by the tests and by scenario replay to check a profile without a
        clock.  Steps are emitted in time order exactly as :meth:`service`
        would emit them.

        Args:
            start_us: Starting value of the simulated clock.
            max_us: Give up after this much simulated time (a safety net).
            trace: Optional list; each step appends
                ``(time_us, channel, direction, position, velocity)``.

        Returns:
            The simulated time in microseconds at which the last step happened.
        """
        now = start_us
        while True:
            due = self.next_deadline()
            if due is None or due - start_us > max_us:
                return now
            now = max(due, now)
            for a in self.axes:
                if a.next_step_us is not None and a.next_step_us <= now:
                    if self._pulse is not None:
                        self._pulse(a.index, a.direction)
                    direction = a.direction
                    velocity = a.velocity
                    a.take_step()
                    self.step_count += 1
                    if trace is not None:
                        trace.append((now, a.index, direction, a.position, velocity))
