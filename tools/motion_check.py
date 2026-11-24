#!/usr/bin/env python3
"""Check the motion planner's limits against the motors that were actually bought.

The planner in ``firmware/pico/motion.py`` defaults to 800 microsteps/s and
1600 microsteps/s^2.  At the A4988's 1/16 stepping on a 200-step motor with no
gearing that is a quarter of a revolution per second.  This script works out
whether those numbers, and the firmware's ceilings, suit the two motor types in
``motors/``: whether a hand can turn 360 degrees without being seen to jerk,
whether the NEMA23s and the NEMA17s want different limits, and what the real
constraint turns out to be.

Everything is computed, not asserted.  Run it::

    python tools/motion_check.py

Where the numbers come from is marked throughout:

* **datasheet** -- from the manufacturer's published specification;
* **computed** -- arithmetic on datasheet figures, or the planner itself run
  offline, which is the same code the Pico executes;
* **bound** -- a deliberately pessimistic estimate, used where a figure is not
  published, chosen so that the conclusion holds whatever the true value is;
* **measured** -- timed on the machine this was run on, which is not a Pico.

Rotor inertia is not published for either motor and is the one figure the
acceleration question needs, so it is bounded rather than guessed: the whole
motor mass is treated as a solid cylinder at the full frame radius.  The real
rotor is smaller in diameter than its frame and a good part of a stepper's mass
is stator and case, which do not rotate at all, so the true inertia is several
times smaller.  Every torque margin below is therefore a floor.

UNTESTED ON HARDWARE.  No motor has been turned.  This is arithmetic about
motors, and arithmetic cannot find a tight bearing or a rubbing tube.
"""

from __future__ import annotations

import math
import sys
import timeit
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PICO_DIR = REPO_ROOT / "firmware" / "pico"
if str(PICO_DIR) not in sys.path:
    sys.path.insert(0, str(PICO_DIR))

import motion  # noqa: E402

#: 200 full steps at 1/16, no gearing.  docs/PROTOCOL.md.
MICROSTEPS_PER_REV = 3200
#: Rotor teeth on a 1.8-degree hybrid stepper.
ROTOR_TEETH = 50

#: Hand geometry, from build_hands.py's current `pointed` style (PROGRESS,
#: pushes 13-16): 115 mm long, 5.2 g.
HAND_LENGTH_M = 0.115
HAND_MASS_KG = 0.0052

#: Effective density of a stepper, as mass per unit of FRAME volume, calibrated
#: on the one motor whose mass is published: 0.23 kg in 42 x 42 x 33.8 mm gives
#: 3858 kg/m^3.  Used to estimate the NEMA23 mass, which no source found states.
EFFECTIVE_DENSITY = 3858.0


class Motor:
    """One motor type, and what is and is not known about it.

    Args:
        name: Part number.
        channels: Which clock channels use it.
        holding_nm: Holding torque, N.m (datasheet).
        current_a: Rated current per phase, A (datasheet).
        frame_mm: Frame width, mm (datasheet) -- square, so also the height.
        length_mm: Body length, mm (datasheet).
        mass_kg: Mass in kg, or ``None`` when not published.
        resistance_ohm: Phase resistance, or ``None``.
        inductance_mh: Phase inductance, or ``None``.
    """

    def __init__(
        self,
        name,
        channels,
        holding_nm,
        current_a,
        frame_mm,
        length_mm,
        mass_kg=None,
        resistance_ohm=None,
        inductance_mh=None,
    ):
        self.name = name
        self.channels = channels
        self.holding_nm = holding_nm
        self.current_a = current_a
        self.frame_mm = frame_mm
        self.length_mm = length_mm
        self.mass_kg = mass_kg
        self.resistance_ohm = resistance_ohm
        self.inductance_mh = inductance_mh

    # -- mass and inertia ------------------------------------------------

    def frame_volume_m3(self):
        """Frame volume, for estimating a mass that is not published."""
        side = self.frame_mm / 1000.0
        return side * side * (self.length_mm / 1000.0)

    def mass_estimate_kg(self):
        """Published mass, or one estimated from the frame volume."""
        if self.mass_kg is not None:
            return self.mass_kg, "datasheet"
        return self.frame_volume_m3() * EFFECTIVE_DENSITY, "bound"

    def inertia_bound(self):
        """Upper bound on rotor inertia, kg.m^2.

        The entire motor mass as a solid cylinder at the full frame radius.
        Generous twice over: the rotor is narrower than its frame, and the
        stator and case do not turn at all.
        """
        mass, _ = self.mass_estimate_kg()
        radius = self.frame_mm / 2000.0
        return 0.5 * mass * radius * radius

    # -- what it can do --------------------------------------------------

    def torque_at(self, current_a):
        """Holding torque derated linearly for a lower set current.

        Linear is the usual first approximation below rated current and is
        conservative at the top of the range.
        """
        return self.holding_nm * min(1.0, current_a / self.current_a)

    def accel_capacity(self):
        """Angular acceleration the rotor alone could manage, rad/s^2.

        Holding torque is not available torque while turning -- at speed a
        stepper delivers considerably less -- so this is an upper bound too.
        It is quoted only to show the size of the margin, not as a target.
        """
        return self.holding_nm / self.inertia_bound()


#: Datasheet figures.  23HS18-2004H: 0.78 N.m, 2.0 A, 57x57x45 mm, mass not
#: published in any source found.  17HS13-1504H: 0.25 N.m, 1.5 A,
#: 42x42x33.8 mm, 0.23 kg, 1.56 ohm, 1.9 mH.  Both 1.8 deg, bipolar, hollow
#: shaft.  Sources: omc-stepperonline.com listings and the Oyostepper and
#: Skysmotor listings of the same part numbers, which agree.
MOTORS = (
    Motor(
        "23HS18-2004H",
        "0-1 (Becky, Evan)",
        holding_nm=0.78,
        current_a=2.0,
        frame_mm=57.0,
        length_mm=45.0,
    ),
    Motor(
        "17HS13-1504H",
        "2-5 (Elijah, Caleb, Austin, Micah)",
        holding_nm=0.25,
        current_a=1.5,
        frame_mm=42.0,
        length_mm=33.8,
        mass_kg=0.23,
        resistance_ohm=1.56,
        inductance_mh=1.9,
    ),
)

#: What the build guide recommends setting Vref for, rather than rated current:
#: "a clock hand needs almost no torque, the drivers stay cool, and the 12 V
#: supply stops being marginal" (docs/build-board.html).
BENCH_CURRENT_A = 0.8


# -- unit helpers -------------------------------------------------------------


def to_rad_s2(microsteps_s2):
    """Microsteps/s^2 to rad/s^2."""
    return microsteps_s2 / MICROSTEPS_PER_REV * 2.0 * math.pi


def to_rev_s(microsteps_s):
    """Microsteps/s to revolutions/s."""
    return microsteps_s / MICROSTEPS_PER_REV


def hand_inertia():
    """The hand's own inertia about the hub, kg.m^2.

    A uniform rod about one end, (1/3) m L^2.  An over-estimate: the boss and
    the collar sit at the pivot where they contribute almost nothing, and the
    name is pierced out of the middle.
    """
    return HAND_MASS_KG * HAND_LENGTH_M * HAND_LENGTH_M / 3.0


def resonance_estimate_hz(motor):
    """Low-speed resonance, Hz, from the rotor-detent pendulum.

    ``f = (1 / 2 pi) sqrt(2 p T / J)`` with ``p`` rotor teeth.  With an
    inertia *upper* bound this comes out as a *lower* bound on the frequency,
    so the true resonance sits at this speed or above it.
    """
    stiffness = 2.0 * ROTOR_TEETH * motor.holding_nm
    return math.sqrt(stiffness / motor.inertia_bound()) / (2.0 * math.pi)


# -- the planner, run offline -------------------------------------------------


def move_profile(microsteps, max_speed, accel):
    """Run one move through the real planner.  Returns a dict of findings."""
    trace: list[tuple] = []
    mc = motion.MotionController(1, max_speed=max_speed, accel=accel)
    mc.move_to(0, int(microsteps), 0)
    end_us = mc.run(0, trace=trace)
    speeds = [abs(v) for (_t, _ch, _d, _p, v) in trace]
    peak = max(speeds) if speeds else 0.0
    # Time to the first ten microsteps: the visible part of a move's start.
    first_ten = trace[9][0] / 1000.0 if len(trace) >= 10 else None
    return {
        "steps": int(microsteps),
        "seconds": end_us / 1_000_000.0,
        "peak_speed": peak,
        "reached_cruise": peak >= max_speed - 0.5,
        "first_ten_ms": first_ten,
        "landed": mc.axis(0).position == int(microsteps),
    }


def analytic_trapezoid_s(microsteps, max_speed, accel):
    """The textbook trapezoid, for comparison with the discrete planner."""
    distance = abs(microsteps)
    ramp = max_speed * max_speed / (2.0 * accel)
    if 2.0 * ramp >= distance:  # triangular: never reaches cruise
        return 2.0 * math.sqrt(distance / accel)
    return 2.0 * (max_speed / accel) + (distance - 2.0 * ramp) / max_speed


# -- the loop budget ---------------------------------------------------------


def measure_cost_per_pulse_us(axes=6, max_speed=4000.0, accel=20000.0, seconds=1.0):
    """Time the planner per STEP pulse emitted, on *this* machine.

    Per pulse, not per ``service()`` call: a call emits anywhere from no
    pulses to one per axis depending on where the deadlines fall, so a
    per-call figure cannot be compared against a pulse interval.
    """
    mc = motion.MotionController(axes, max_speed=max_speed, accel=accel)
    for channel in range(axes):
        mc.move_to(channel, 10**9, 0)
    now = 0
    interval = int(1_000_000.0 / max_speed)
    while abs(mc.axis(0).velocity) < max_speed - 1:  # get to cruise first
        now += 50
        mc.service(now)
    before = mc.step_count
    start = timeit.default_timer()
    calls = 0
    while timeit.default_timer() - start < seconds:
        for _ in range(200):
            now += interval
            mc.service(now)
        calls += 200
    elapsed = timeit.default_timer() - start
    pulses = mc.step_count - before
    return elapsed / pulses * 1e6, pulses / calls


#: How much slower to assume MicroPython on a 125 MHz RP2040 is than CPython on
#: this machine, for the same bytecode.  A wide range rather than a number,
#: because it has NOT been measured on a Pico -- nothing in this repository has.
#: MicroPython on a Cortex-M0+ at 125 MHz runs on the order of a million simple
#: bytecodes a second where desktop CPython runs tens of millions, and
#: :meth:`motion.Axis.take_step` is a few dozen bytecodes with a square root in
#: it.  Both ends of the range are quoted everywhere below so the conclusion can
#: be read off either.  ``late`` in ``status`` is how to replace this guess with
#: a fact once there is a Pico.
MICROPYTHON_FACTOR = (30.0, 100.0)


# -- report ------------------------------------------------------------------

RULE = "=" * 76


def heading(text):
    """Print a section heading."""
    print("\n%s\n%s\n%s" % (RULE, text, RULE))


def report_motors():
    """What is known about each motor, and the acceleration margin."""
    heading("The two motors")
    hand_j = hand_inertia()
    print(
        "  Hand (both types): %.1f g, %.0f mm, inertia about the hub %.2e kg.m^2"
        % (HAND_MASS_KG * 1000, HAND_LENGTH_M * 1000, hand_j)
    )
    print("  (computed, uniform rod about one end -- an over-estimate)\n")

    for motor in MOTORS:
        mass, mass_source = motor.mass_estimate_kg()
        bound = motor.inertia_bound()
        print("  %s   channels %s" % (motor.name, motor.channels))
        print(
            "    holding torque   %.2f N.m at %.1f A          [datasheet]"
            % (motor.holding_nm, motor.current_a)
        )
        print(
            "    at bench current %.3f N.m at %.1f A          [computed]"
            % (motor.torque_at(BENCH_CURRENT_A), BENCH_CURRENT_A)
        )
        if motor.resistance_ohm:
            print(
                "    winding          %.2f ohm, %.1f mH                [datasheet]"
                % (motor.resistance_ohm, motor.inductance_mh)
            )
        print(
            "    mass             %.3f kg                        [%s]"
            % (mass, mass_source)
        )
        print("    rotor inertia    <= %.2e kg.m^2               [bound]" % bound)
        print(
            "    hand adds        %.1f%% of that                    [computed]"
            % (100.0 * hand_j / bound)
        )
        print(
            "    could accelerate <= %.0f rad/s^2 = %.0f rev/s^2    [bound]"
            % (motor.accel_capacity(), motor.accel_capacity() / (2 * math.pi))
        )
        print()


def report_accel_margin():
    """Torque needed to accelerate, against torque available."""
    heading("Is the acceleration torque-limited?")
    hand_j = hand_inertia()
    rates = (
        ("default", motion.DEFAULT_ACCEL),
        ("firmware ceiling", motion.ACCEL_LIMIT),
    )
    print(
        "  %-18s %-22s %-14s %-14s %s"
        % ("accel", "", "NEMA23 torque", "NEMA17 torque", "of holding at 0.8 A")
    )
    for label, accel in rates:
        alpha = to_rad_s2(accel)
        line = "  %-18s %-22s" % (
            label,
            "%.0f usteps/s^2 = %.2f rev/s^2" % (accel, accel / MICROSTEPS_PER_REV),
        )
        shares = []
        for motor in MOTORS:
            torque = (motor.inertia_bound() + hand_j) * alpha
            line += " %-14s" % ("%.1f mN.m" % (torque * 1000))
            shares.append(100.0 * torque / motor.torque_at(BENCH_CURRENT_A))
        print(line + " %.2f%% / %.2f%%" % tuple(shares))
    print()
    print("  No. Both motors are two to three orders of magnitude clear of what")
    print("  the firmware's own ceiling asks of them, even at the reduced bench")
    print("  current the build guide recommends, and even against an inertia")
    print("  bound that is several times the real figure. Torque is not the")
    print("  constraint on this clock and will not become one: the hand is 5 g.")
    print()
    print("  The non-obvious part: per unit of its own inertia the NEMA23 is the")
    print("  *weaker* of the two.")
    for motor in MOTORS:
        print(
            "    %s  torque/inertia >= %5.0f rad/s^2"
            % (motor.name, motor.accel_capacity())
        )
    ratio = MOTORS[1].accel_capacity() / MOTORS[0].accel_capacity()
    print(
        "  The NEMA17 self-accelerates about %.1fx better. So if the two types"
        % ratio
    )
    print("  ever do want different limits, the NEMA23s want the LOWER one --")
    print("  the opposite of the intuition that the bigger motor is stronger.")
    print("  At these rates neither is close enough to its limit for it to matter.")


def report_resonance():
    """Where the low-speed resonance sits, in the planner's own units."""
    heading("Where the resonance band sits")
    print("  A stepper's low-speed resonance is the rotor oscillating in its own")
    print("  detent torque. It is where an unloaded motor stalls, and an unloaded")
    print("  clock hand is about as unloaded as a motor gets.\n")
    lowest = None
    for motor in MOTORS:
        hz = resonance_estimate_hz(motor)
        usteps = hz * MICROSTEPS_PER_REV / 200.0
        lowest = hz if lowest is None else min(lowest, hz)
        print("  %s" % motor.name)
        print(
            "    >= %.0f Hz full-step rate = %.2f rev/s = %.0f microsteps/s  [bound]"
            % (hz, hz / 200.0, usteps)
        )
    print()
    print("  Read these as floors: the inertia bound is high, which pushes the")
    print("  frequency estimate low, so the real band is at these speeds or above.")
    print()
    print(
        "  default %.0f microsteps/s = %.2f rev/s   -- clear of it, by a lot"
        % (motion.DEFAULT_MAX_SPEED, to_rev_s(motion.DEFAULT_MAX_SPEED))
    )
    print(
        "  ceiling %.0f microsteps/s = %.2f rev/s  -- inside the plausible range"
        % (motion.SPEED_LIMIT, to_rev_s(motion.SPEED_LIMIT))
    )
    print()
    print("  1/16 microstepping suppresses most of this -- it is largely a")
    print("  full-stepping phenomenon -- which is the reason the five pins are")
    print("  bridged high. But the ceiling is not a speed to go looking for.")


def report_profiles():
    """Move durations, from the planner itself."""
    heading("What a move actually looks like")
    speed, accel = motion.DEFAULT_MAX_SPEED, motion.DEFAULT_ACCEL
    print(
        "  At the defaults: %.0f microsteps/s (%.3f rev/s), %.0f microsteps/s^2\n"
        % (speed, to_rev_s(speed), accel)
    )
    print(
        "  %-22s %8s %9s %9s %10s %s"
        % ("move", "usteps", "planner", "textbook", "peak", "cruise?")
    )
    moves = (
        ("one microstep, 0.11 deg", 1),
        ("1 degree", round(MICROSTEPS_PER_REV / 360)),
        ("one full step, 1.8 deg", 16),
        ("15 degrees", round(MICROSTEPS_PER_REV / 24)),
        ("90 degrees", MICROSTEPS_PER_REV // 4),
        ("180 degrees", MICROSTEPS_PER_REV // 2),
        ("360 degrees", MICROSTEPS_PER_REV),
    )
    for label, steps in moves:
        result = move_profile(steps, speed, accel)
        textbook = analytic_trapezoid_s(steps, speed, accel)
        assert result["landed"], "the planner missed its target"
        print(
            "  %-22s %8d %8.3fs %8.3fs %9.0f  %s"
            % (
                label,
                steps,
                result["seconds"],
                textbook,
                result["peak_speed"],
                "yes" if result["reached_cruise"] else "no, triangular",
            )
        )
    print()
    full = move_profile(MICROSTEPS_PER_REV, speed, accel)
    print("  A full turn takes %.2f s. Judge that against the clock, not the" % full["seconds"])
    print("  arithmetic: it is a deliberate sweep, not a snap.")
    print()
    print("  Can a microstep be seen?")
    per_step_deg = 360.0 / MICROSTEPS_PER_REV
    tip_mm = HAND_LENGTH_M * 1000 * math.radians(per_step_deg)
    print("    one microstep          %.4f deg" % per_step_deg)
    print("    at the hand's tip      %.3f mm                    [computed]" % tip_mm)
    print(
        "    arriving every         %.2f ms at the default rate"
        % (1000.0 / speed)
    )
    print("    Individually, %.3f mm is near the limit of what an eye resolves at" % tip_mm)
    print("    arm's length -- but they arrive at %.0f Hz, far above flicker" % speed)
    print("    fusion, so the motion reads as continuous.")
    print()
    print("  Is the start a jerk?")
    print(
        "    first microstep at sqrt(2a) = %.1f microsteps/s, so %.1f ms after"
        % (math.sqrt(2 * accel), 1e3 / math.sqrt(2 * accel))
    )
    print("    the command; the first ten take %.0f ms and cover %.2f deg."
          % (full["first_ten_ms"], 10 * per_step_deg))
    print("    That is a soft start, which is the opposite of a jerk. The")
    print("    trapezoid's own corners are discontinuities in acceleration, but")
    print("    with the inertia above there is nothing there to excite.")


def report_loop_budget():
    """Whether the Pico can emit the pulses the ceiling implies."""
    heading("Can the loop keep up?")
    cost, per_call = measure_cost_per_pulse_us()
    low = cost * MICROPYTHON_FACTOR[0]
    high = cost * MICROPYTHON_FACTOR[1]
    print(
        "  Planner cost per STEP pulse: %.2f us   [measured here, CPython on a"
        % cost
    )
    print("  desktop, %.1f pulses per service() call]" % per_call)
    print(
        "  On a Pico, assuming %.0f-%.0fx:  %.0f-%.0f us per pulse   [estimate,"
        % (MICROPYTHON_FACTOR[0], MICROPYTHON_FACTOR[1], low, high)
    )
    print("  not measured on a Pico], plus the %d us the STEP pulse itself holds" % 5)
    print("  the loop, plus whatever the serial reader and housekeeping cost.\n")
    print("  %-26s %10s %13s  %s" % ("six axes at", "pulses/s", "us available", "verdict"))
    for label, speed in (
        ("default %.0f" % motion.DEFAULT_MAX_SPEED, motion.DEFAULT_MAX_SPEED),
        ("1600", 1600.0),
        ("ceiling %.0f" % motion.SPEED_LIMIT, motion.SPEED_LIMIT),
    ):
        pulses = speed * 6
        budget_us = 1_000_000.0 / pulses
        if high < budget_us * 0.5:
            verdict = "fits, %.0f-%.0fx headroom" % (budget_us / high, budget_us / low)
        elif low < budget_us:
            verdict = "marginal: fits at %.0fx, not at %.0fx" % MICROPYTHON_FACTOR
        else:
            verdict = "does not fit at either end"
        print("  %-26s %10.0f %13.1f  %s" % (label, pulses, budget_us, verdict))
    print()
    print("  main.py's docstring claims 'roughly a hundred times the headroom it")
    print("  needs'. That looks too generous, and the reason is interesting: it")
    print("  compared the 208 us pulse interval against a bare loop iteration")
    print("  rather than against the cost of take_step, which does a square root")
    print("  and a dozen float operations per step. Single-digit headroom at the")
    print("  default rate is the better reading. The conclusion the docstring")
    print("  draws -- that a software step loop is enough and PIO is not needed --")
    print("  still holds at 800 microsteps/s. It does not hold at the ceiling.")
    print()
    print("  This is no longer a silent problem. A loop that cannot keep up now")
    print("  re-bases its schedule instead of bunching pulses, so the hands run")
    print("  slow and `late` in `status` climbs -- rather than the firmware")
    print("  counting steps the motor never took.")


def report_recommendation():
    """What to actually do."""
    heading("Recommendation")
    print("  1. Keep the defaults: 800 microsteps/s and 1600 microsteps/s^2.")
    print("     A quarter turn per second, a 4.5 s full sweep, a soft start, and")
    print("     a long way clear of the resonance band. Nothing in the motors")
    print("     argues for changing them, and the loop budget argues against")
    print("     raising them: single-digit headroom at 800 is the one estimate")
    print("     here that is not comfortable.")
    print()
    print("  2. Do not give the NEMA23s and the NEMA17s different limits. The")
    print("     margin is 2-3 orders of magnitude for both. If a reason ever")
    print("     appears it will be the load -- a tight tube, a stiff belt under")
    print("     option D -- not the motor, and it will want the limit LOWERED on")
    print("     whichever channel has the problem, which is what per-channel")
    print("     set_params is for.")
    print()
    print("  3. Treat SPEED_LIMIT = 4000 as what it says it is: a ceiling the")
    print("     drivers survive, not a speed to use. Six axes there are inside")
    print("     the plausible resonance band and at the edge of what a")
    print("     MicroPython loop can emit. If a faster sweep is ever wanted,")
    print("     1600 microsteps/s (0.5 rev/s, a 2.5 s full turn) is as far as")
    print("     this arithmetic reaches, and even that is marginal on the loop")
    print("     budget: it needs a Pico and a look at `late` before trusting it.")
    print()
    print("  4. Set Vref for 0.8 A, as docs/build-board.html says. The torque")
    print("     margin at 0.8 A is still ~1000x what the hand needs, the drivers")
    print("     stay cool, and the 12 V supply stops being marginal.")
    print()
    print("  5. What this cannot tell you, and the bench can:")
    print("     * whether a tube or bearing binds -- run tools/bench.py check and")
    print("       watch the full revolution land on its mark;")
    print("     * whether the loop keeps up on a real Pico -- watch `late` in")
    print("       `bench.py pos` after a long move;")
    print("     * where the resonance actually is -- step one channel at rising")
    print("       speeds and listen for it, with no hand fitted.")
    print()
    print("  UNTESTED ON HARDWARE. No motor has been turned. Arithmetic cannot")
    print("  find a rubbing tube.")


def main(argv=None):
    """Print the whole report."""
    print(__doc__.split("Everything is computed")[0].strip())
    report_motors()
    report_accel_margin()
    report_resonance()
    report_profiles()
    report_loop_budget()
    report_recommendation()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
