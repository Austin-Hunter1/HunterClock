#!/usr/bin/env python3
"""Bench bring-up for the six A4988 channels: one motor at a time, over USB.

This is the tool for the step the build guide calls *"power off, connect one
motor, power on, send a few steps, watch it turn"* (``docs/build-board.html``,
Solder order step 9).  It talks the ordinary protocol in ``docs/PROTOCOL.md`` to
the ordinary firmware -- there is no special bench mode on the Pico, so anything
that works here works when the controller does it.

**Nothing in this file has been run against a Pico, a driver or a motor.**  It
has only ever been exercised against ``tools/pico_sim.py``.  Treat the
procedures below as instructions to follow at the bench, not as results.

Start here
----------
With nothing but the Pico on USB (Solder order step 3), confirm you can talk to
it at all.  This touches no driver and no motor::

    python tools/bench.py info

Then, with one motor wired and 12 V on (step 9), walk one channel through a
scripted check::

    python tools/bench.py check --ch 0

and answer the questions it asks.  Everything else is for poking at a channel
by hand afterwards::

    python tools/bench.py step --ch 0 800      # a quarter turn forward
    python tools/bench.py step --ch 0 -800     # and back
    python tools/bench.py wiggle --ch 0        # which motor is this?
    python tools/bench.py pos                  # what does the firmware believe?
    python tools/bench.py off                  # stop and release all six
    python tools/bench.py monitor              # watch events, drive nothing
    python tools/bench.py repl                 # short commands, one connection

Add ``--port sim`` to any of them to run against the simulator instead of a
serial port, which is how to learn the tool before the parts arrive and how its
own tests run.

Three things this tool will not let you forget
----------------------------------------------
**ENABLE is one pin for all six drivers** (GP14, active low).  There is no way
to energise one channel: ``enable`` holds every motor that is plugged in.  The
tool says so before it enables anything, and it releases the drivers again when
it exits -- including on Ctrl-C and on an exception -- so you are never left
with coils energised because a script died.

**The position is a belief, not a measurement.**  There are no home sensors.
The firmware counts the pulses it emitted; if the motor missed some, nobody
knows.  Every report here is labelled with whose claim it is, and the checks
end by asking you to compare a physical mark against the number.

**A reset wipes the belief and keeps the hand.**  If a ``boot`` event arrives
mid-session the Pico restarted underneath us, so its position came from flash
and the move you were watching was abandoned part-way.  The tool aborts rather
than carrying on against a stale number.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PICO_DIR = REPO_ROOT / "firmware" / "pico"
for _path in (str(PICO_DIR), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import motion  # noqa: E402  (firmware/pico/motion.py)
import protocol  # noqa: E402  (firmware/pico/protocol.py)

#: One full revolution of a hand: 200 full steps at the A4988's 1/16 stepping,
#: and no gearing.  Same number as ``docs/PROTOCOL.md``.
MICROSTEPS_PER_REV = 3200

#: What each channel is, for reading next to the board.  Person and level come
#: from ``firmware/pico/main.py``; the driver number, motor and Pico header pins
#: come from the tables in ``docs/build-board.html`` and Circuit Diagram Rev. 2.
#: (channel, person, driver, motor, STEP gpio, STEP pin, DIR gpio, DIR pin)
CHANNELS = (
    (0, "Becky", 1, "23HS18 NEMA23", 2, 4, 3, 5),
    (1, "Evan", 2, "23HS18 NEMA23", 4, 6, 5, 7),
    (2, "Elijah", 3, "17HS13 NEMA17", 6, 9, 7, 10),
    (3, "Caleb", 4, "17HS13 NEMA17", 8, 11, 9, 12),
    (4, "Austin", 5, "17HS13 NEMA17", 10, 14, 11, 15),
    (5, "Micah", 6, "17HS13 NEMA17", 12, 16, 13, 17),
)

#: Bench defaults: half the firmware's own defaults, because the first thing you
#: want from a new channel is to *see* each step, not to get there quickly.
#: 400 microsteps/s is 0.125 rev/s, so a quarter turn takes two seconds.
BENCH_SPEED = 400.0
BENCH_ACCEL = 800.0

#: Serial settings from ``docs/PROTOCOL.md``.
BAUD = 115200

#: Ping often enough that the firmware's 10 s watchdog never trips while a
#: human is reading a prompt.
PING_INTERVAL_S = 2.0

#: How long to wait for a reply before giving up on it.
REPLY_TIMEOUT_S = 5.0


def channel_label(channel: int) -> str:
    """One line describing a channel, for printing next to the board."""
    for ch, person, driver, motor, step_gpio, step_pin, dir_gpio, dir_pin in CHANNELS:
        if ch == channel:
            return (
                "ch %d  %-6s  driver %d  %s  "
                "STEP GP%d (pin %d)  DIR GP%d (pin %d)"
                % (ch, person, driver, motor, step_gpio, step_pin, dir_gpio, dir_pin)
            )
    return "ch %d  (no entry in the channel table)" % channel


def describe_steps(steps: int) -> str:
    """``800`` -> ``"800 microsteps (0.250 rev, 90.0 deg)"``."""
    revs = steps / MICROSTEPS_PER_REV
    return "%d microsteps (%.3f rev, %.1f deg)" % (steps, revs, revs * 360.0)


# -- errors -------------------------------------------------------------------


class BenchError(Exception):
    """Anything that should stop the run with a message and no traceback."""


class PicoReset(BenchError):
    """A ``boot`` event arrived mid-session: the Pico restarted under us."""


class CommandFailed(BenchError):
    """The Pico answered a command with ``ok: false``."""

    def __init__(self, reply: dict):
        self.reply = reply
        BenchError.__init__(
            self,
            "%s was refused: %s (%s)"
            % (reply.get("cmd"), reply.get("err"), reply.get("msg", "")),
        )


# -- transports ---------------------------------------------------------------


def find_port() -> str:
    """Guess the Pico's serial port, or raise with what to pass instead."""
    candidates = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/tty.usbmodem*"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise BenchError(
            "no /dev/ttyACM* found. Is the Pico plugged in and running main.py?\n"
            "  Pass --port explicitly, or --port sim to use the simulator."
        )
    raise BenchError(
        "several serial ports found (%s); pass one with --port" % ", ".join(candidates)
    )


def open_transport(port: str, baud: int = BAUD):
    """Return an object with ``write``/``read``/``in_waiting``, and its name."""
    if port == "sim":
        from tools.pico_sim import MonotonicClock, SimPico

        return SimPico(channels=6, clock=MonotonicClock(1.0)), "simulator"
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - pyserial is a controller dep
        raise BenchError(
            "pyserial is not installed: pip install pyserial  (or use --port sim)"
        ) from exc
    if port == "auto":
        port = find_port()
    try:
        handle = serial.Serial(port, baud, timeout=0.05, write_timeout=2.0)
    except Exception as exc:
        raise BenchError("cannot open %s: %s" % (port, exc)) from exc
    return handle, port


# -- the link -----------------------------------------------------------------


class Link:
    """One connection to a Pico (or to the simulator), with a reader thread.

    A reader thread exists for one reason: the firmware stops the hands if it
    sees no ``ping`` for ten seconds, and this tool spends most of its life
    waiting for a human to read a prompt.  The thread keeps the pings going and
    keeps events flowing even while the main thread is blocked on ``input()``.
    """

    def __init__(self, transport, name: str, verbose: bool = False, ping: bool = True):
        self.transport = transport
        self.name = name
        self.verbose = verbose
        self.ping_enabled = ping
        self._lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, dict] = {}
        self._replies = threading.Condition()
        self._buffer = b""
        self._stop = threading.Event()
        self._reset_seen: dict | None = None
        self._last_uptime_ms: float | None = None
        self.boot: dict | None = None
        self.faults: list[dict] = []
        self.last_pos: dict | None = None
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Stop the reader thread and close the port."""
        self._stop.set()
        self._thread.join(timeout=2.0)
        with contextlib.suppress(Exception):
            self.transport.close()

    def __enter__(self) -> Link:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the reader ------------------------------------------------------

    def _read_loop(self) -> None:
        next_ping = time.monotonic()
        while not self._stop.is_set():
            try:
                with self._lock:
                    waiting = getattr(self.transport, "in_waiting", 0)
                    chunk = self.transport.read(waiting if waiting else 1)
            except Exception:  # the port went away; let the main thread notice
                time.sleep(0.05)
                chunk = b""
            if chunk:
                self._buffer += chunk
                self._drain_buffer()
            else:
                time.sleep(0.01)
            now = time.monotonic()
            if self.ping_enabled and now >= next_ping:
                next_ping = now + PING_INTERVAL_S
                with contextlib.suppress(Exception):
                    self._write_raw({"seq": self._take_seq(), "cmd": "ping"})

    def _drain_buffer(self) -> None:
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = protocol.decode(line)
            except protocol.ProtocolError:
                self._note("garbage from the Pico: %r" % line[:80])
                continue
            self._dispatch(message)

    def _note_uptime(self, message: dict) -> None:
        """Spot a restart from the clock going backwards.

        The ``boot`` event is the obvious signal, but it is a single line on a
        link that can drop one -- and a reset that happens before we ever see
        the first ``boot`` leaves nothing to compare against.  Uptime appears
        on every ``ping`` reply, every ``status`` and every ``pos`` event, so a
        restart cannot hide for more than one of those.
        """
        value = message.get("uptime_ms", message.get("t"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if self._last_uptime_ms is not None and value < self._last_uptime_ms - 1000:
            if self._reset_seen is None:
                self._reset_seen = {"ev": "boot", "reason": "uptime went backwards"}
                self._note(
                    "!! the Pico's uptime went backwards (%.1f s -> %.1f s): it restarted"
                    % (self._last_uptime_ms / 1000.0, value / 1000.0)
                )
            return
        self._last_uptime_ms = max(value, self._last_uptime_ms or 0)

    def _dispatch(self, message: dict) -> None:
        self._note_uptime(message)
        if protocol.is_reply(message):
            with self._replies:
                self._pending[message["seq"]] = message
                self._replies.notify_all()
            if self.verbose:
                self._note("<- %s" % json.dumps(message))
            return
        name = message.get("ev")
        if name == "boot":
            if self.boot is None:
                self.boot = message
            else:
                self._reset_seen = message
                self._note("!! boot event: the Pico restarted underneath us")
            with self._replies:
                self._replies.notify_all()
        elif name == "fault":
            self.faults.append(message)
            self._note(
                "!! fault %s: %s" % (message.get("code"), message.get("msg", ""))
            )
        elif name == "pos":
            self.last_pos = message
            if self.verbose:
                self._note("<- %s" % json.dumps(message))
        elif name == "log":
            self._note("log: %s" % message.get("msg", json.dumps(message)))
        elif self.verbose:
            self._note("<- %s" % json.dumps(message))

    @staticmethod
    def _note(text: str) -> None:
        print(text, flush=True)

    # -- sending ---------------------------------------------------------

    def _take_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _write_raw(self, command: dict) -> None:
        raw = protocol.encode(command)
        with self._lock:
            self.transport.write(raw)
            with contextlib.suppress(AttributeError, Exception):
                self.transport.flush()

    def command(self, cmd: str, timeout: float = REPLY_TIMEOUT_S, **args) -> dict:
        """Send one command and return its reply.

        Raises:
            CommandFailed: the Pico answered ``ok: false``.
            PicoReset: a ``boot`` event arrived while we waited.
            BenchError: no reply inside ``timeout``.
        """
        self.check_reset()
        seq = self._take_seq()
        message = {"seq": seq, "cmd": cmd}
        message.update({k: v for k, v in args.items() if v is not None})
        if self.verbose:
            self._note("-> %s" % json.dumps(message))
        self._write_raw(message)
        deadline = time.monotonic() + timeout
        with self._replies:
            while seq not in self._pending:
                if self._reset_seen is not None:
                    raise PicoReset(
                        "the Pico reset while waiting for %r. Its position now comes "
                        "from flash, not from the motor: re-check this channel from "
                        "the start." % cmd
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BenchError(
                        "no reply to %r after %.1f s. Is main.py running on the Pico?"
                        % (cmd, timeout)
                    )
                self._replies.wait(min(remaining, 0.1))
            reply = self._pending.pop(seq)
        if not reply.get("ok"):
            raise CommandFailed(reply)
        return reply

    def check_reset(self) -> None:
        """Raise :class:`PicoReset` if the Pico has restarted under us."""
        if self._reset_seen is not None:
            raise PicoReset(
                "the Pico reset mid-session (reason %r). Nothing it reports about "
                "position is trustworthy until the hand is checked by eye."
                % self._reset_seen.get("reason")
            )

    # -- convenience -----------------------------------------------------

    def hello(self) -> dict:
        """Identify the firmware, and refuse a protocol we do not speak."""
        info = self.command("hello")
        proto = info.get("proto")
        if proto != protocol.PROTOCOL_VERSION:
            raise BenchError(
                "the Pico speaks protocol %r; this tool speaks %d. Re-upload the "
                "firmware from this checkout (make firmware-upload)."
                % (proto, protocol.PROTOCOL_VERSION)
            )
        return info

    def status(self) -> dict:
        """Live motion state."""
        return self.command("status")

    def enable(self, on: bool) -> dict:
        """Drive the shared ENABLE line.  This is all six drivers at once."""
        return self.command("enable", on=on)

    def release(self) -> None:
        """Stop everything and release the drivers.  Never raises."""
        for cmd, args in (("stop", {}), ("enable", {"on": False})):
            with contextlib.suppress(Exception):
                self.command(cmd, **args)


# -- predicting a move --------------------------------------------------------


def predicted_move_s(steps: int, max_speed: float, accel: float) -> float:
    """How long the planner says ``steps`` microsteps should take, in seconds.

    Uses :mod:`motion` -- the planner the Pico is running -- rather than an
    independent formula, so the prediction cannot drift from the firmware.  The
    point of having it is that a move finishing *late* is a symptom: a stalling
    motor still returns pulses on time, but a Pico that is swapping between the
    step loop and a flood of serial traffic does not.
    """
    if steps == 0:
        return 0.0
    controller = motion.MotionController(1, max_speed=max_speed, accel=accel)
    controller.move_to(0, int(steps), 0)
    return controller.run(0) / 1_000_000.0


# -- reporting ----------------------------------------------------------------

RULE = "-" * 72


def heading(text: str) -> None:
    """Print a section heading."""
    print("\n%s\n%s\n%s" % (RULE, text, RULE), flush=True)


def report_positions(positions: list[int], highlight: int | None = None) -> None:
    """Print the firmware's position for every channel, labelled as its claim."""
    print("  the firmware believes, in microsteps:")
    for ch, person, *_ in CHANNELS:
        if ch >= len(positions):
            continue
        mark = ">" if ch == highlight else " "
        value = positions[ch]
        print(
            "   %s ch %d %-6s %+8d  (%+.3f rev, %+.1f deg)"
            % (mark, ch, person, value, value / MICROSTEPS_PER_REV,
               360.0 * value / MICROSTEPS_PER_REV)
        )


def ask(question: str, assume_yes: bool = False) -> bool:
    """Ask the human a yes/no question.  ``--yes`` answers yes to all of them."""
    if assume_yes:
        print("  %s [assuming yes: --yes]" % question, flush=True)
        return True
    while True:
        try:
            answer = input("  %s [y/n] " % question).strip().lower()
        except EOFError:
            print("(no terminal to ask on; treating as no)")
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def pause(text: str, assume_yes: bool = False) -> None:
    """Wait for the human, unless ``--yes``."""
    if assume_yes:
        print("  %s [continuing: --yes]" % text, flush=True)
        return
    with contextlib.suppress(EOFError):
        input("  %s " % text)


# -- the shared "do one move and account for it" step -------------------------


class MoveResult:
    """What one bench move did, as far as anyone can tell from this end."""

    def __init__(self, channel, start, end, requested, emitted, seconds, predicted):
        self.channel = channel
        self.start = start
        self.end = end
        self.requested = requested
        self.emitted = emitted
        self.seconds = seconds
        self.predicted = predicted

    @property
    def believed(self) -> int:
        """Microsteps the firmware thinks it moved."""
        return self.end - self.start

    @property
    def landed(self) -> bool:
        """True when the firmware's own position arithmetic came out right."""
        return self.believed == self.requested

    def report(self) -> None:
        """Print the accounting for this move."""
        print("  requested      %s" % describe_steps(self.requested))
        print(
            "  firmware moved %s%s"
            % (
                describe_steps(self.believed),
                "" if self.landed else "   <-- NOT what was asked for",
            )
        )
        print("  position       %+d -> %+d" % (self.start, self.end))
        print(
            "  pulses emitted %d  (expected %d)%s"
            % (
                self.emitted,
                abs(self.requested),
                "" if self.emitted == abs(self.requested) else "   <-- mismatch",
            )
        )
        print(
            "  took           %.2f s  (planner predicted %.2f s)%s"
            % (
                self.seconds,
                self.predicted,
                ""
                if self.predicted <= 0 or self.seconds <= self.predicted * 1.5 + 0.5
                else "   <-- late; the step loop is being starved",
            )
        )


def do_move(
    link: Link,
    channel: int,
    steps: int,
    speed: float,
    accel: float,
    budget_s: float | None = None,
) -> MoveResult:
    """Jog one channel by ``steps`` microsteps and wait for it to finish.

    ``jog`` rather than ``move`` so the caller never has to track an absolute
    target, and so two of these in a row add up the way the bench expects.

    Args:
        budget_s: How long to let the move run before stopping it and
            complaining.  Default: three times what the planner predicts, and
            never less than ten seconds.
    """
    before = link.status()
    start = before["pos"][channel]
    pulses_before = before["steps"]
    predicted = predicted_move_s(steps, speed, accel)
    budget = budget_s if budget_s is not None else max(predicted * 3.0 + 5.0, 10.0)

    link.command("jog", ch=channel, delta=steps)
    began = time.monotonic()
    deadline = began + budget
    while True:
        link.check_reset()
        state = link.status()
        if not state["moving"][channel]:
            break
        if time.monotonic() > deadline:
            where = stop_and_settle(link, channel)
            raise BenchError(
                "ch %d was still moving after %.1f s (the planner said %.1f s). "
                "Stopped it; it came to rest at %+d, having started from %+d on "
                "the way to %+d. Either the Pico is not keeping up, or the move "
                "is not the one you meant."
                % (channel, budget, predicted, where, start, start + steps)
            )
        time.sleep(0.05)
    seconds = time.monotonic() - began
    after = link.status()
    return MoveResult(
        channel=channel,
        start=start,
        end=after["pos"][channel],
        requested=steps,
        emitted=after["steps"] - pulses_before,
        seconds=seconds,
        predicted=predicted,
    )


def stop_and_settle(link: Link, channel: int, timeout_s: float = 10.0) -> int:
    """Decelerate ``channel`` and wait for it to come to rest.

    Returns the position it stopped at.  Giving up on a move without waiting
    for the ramp to finish would report a position the hand was still leaving,
    which is the one number a person at the bench must be able to trust.
    """
    link.command("stop", ch=channel)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = link.status()
        if not state["moving"][channel]:
            return state["pos"][channel]
        time.sleep(0.02)
    return link.status()["pos"][channel]


def enable_with_warning(link: Link, assume_yes: bool) -> None:
    """Enable the drivers, having said what that means."""
    print("\n  ENABLE is one pin (GP14) for all six drivers, active low.")
    print("  Enabling energises every driver that is fitted, and every motor")
    print("  that is plugged into one will hold its position and get warm.")
    if not ask("Enable the drivers?", assume_yes):
        raise BenchError("not enabling; nothing was driven.")
    link.enable(True)
    print("  drivers enabled.")


def apply_bench_params(link: Link, channel: int | None, speed: float, accel: float) -> None:
    """Set the slow bench speed, and say what the firmware accepted."""
    reply = link.command("set_params", ch=channel, max_speed=speed, accel=accel)
    got_speed = reply.get("max_speed")
    got_accel = reply.get("accel")
    print(
        "  speed %.0f microsteps/s (%.3f rev/s), accel %.0f microsteps/s^2"
        % (got_speed, got_speed / MICROSTEPS_PER_REV, got_accel)
    )
    if (got_speed, got_accel) != (speed, accel):
        print(
            "  note: asked for %.0f / %.0f; the firmware clamped it to its own "
            "ceiling." % (speed, accel)
        )


# -- subcommands --------------------------------------------------------------


def cmd_info(link: Link, args) -> int:
    """Identify the firmware and print what it believes.  Drives nothing."""
    heading("Firmware")
    info = link.hello()
    print("  connection      %s" % link.name)
    print("  firmware        %s   protocol %s" % (info["fw"], info["proto"]))
    print("  boot reason     %s" % info["boot_reason"])
    print("  uptime          %.1f s" % (info["uptime_ms"] / 1000.0))
    print("  channels        %d" % info["channels"])
    print("  drivers         %s" % ("ENABLED" if info["enabled"] else "released"))
    print(
        "  verification    %s"
        % (
            "needed -- the controller will refuse to drive these hands"
            if info["needs_verification"]
            else "done"
        )
    )
    print(
        "  speed / accel   %.0f / %.0f microsteps/s(^2)   ceilings %.0f / %.0f"
        % (
            info["max_speed"],
            info["accel"],
            info["speed_limit"],
            info["accel_limit"],
        )
    )
    print("  watchdog        %.1f s" % info["watchdog_s"])
    heading("Positions")
    report_positions(info["pos"])
    print("\n  These are counts of pulses the firmware emitted, not measurements.")
    print("  There are no home sensors: only you can say where a hand really is.")
    heading("Channel map")
    for ch, *_ in CHANNELS:
        print("  " + channel_label(ch))
    return 0


def cmd_pos(link: Link, args) -> int:
    """Print live motion state."""
    link.hello()
    state = link.status()
    heading("Live state")
    report_positions(state["pos"])
    print()
    print("  moving          %s" % state["moving"])
    print("  velocity        %s microsteps/s" % state["vel"])
    print("  target          %s" % state["target"])
    print("  drivers         %s" % ("ENABLED" if state["enabled"] else "released"))
    print("  pulses so far   %d" % state["steps"])
    print("  protocol errors %d" % state["errors"])
    print("  watchdog        %s" % ("TRIPPED" if state["watchdog_tripped"] else "fed"))
    return 0


def cmd_off(link: Link, args) -> int:
    """Stop every channel and release every driver."""
    link.hello()
    link.command("stop")
    link.enable(False)
    print("All six channels stopped and all six drivers released.")
    print("The hands can now be turned by hand, and hold no position.")
    return 0


def cmd_step(link: Link, args) -> int:
    """Move one channel by a signed number of microsteps, and account for it."""
    channel = args.ch
    steps = args.steps
    heading("Step one channel")
    print("  " + channel_label(channel))
    print("  moving %s" % describe_steps(steps))
    link.hello()
    enable_with_warning(link, args.yes)
    try:
        apply_bench_params(link, channel, args.speed, args.accel)
        result = do_move(link, channel, steps, args.speed, args.accel)
        print()
        result.report()
        print()
        report_positions(link.status()["pos"], highlight=channel)
    finally:
        if not args.keep_enabled:
            link.release()
            print("\n  drivers released.")
        else:
            print("\n  drivers left ENABLED (--keep-enabled): the motors are holding.")
    return 0


def cmd_wiggle(link: Link, args) -> int:
    """Rock a channel back and forth, so you can see which motor it is."""
    channel = args.ch
    amount = args.steps
    heading("Wiggle one channel")
    print("  " + channel_label(channel))
    print("  rocking +-%s, %d times" % (describe_steps(amount), args.times))
    print("  Watch the motors: the one that moves is this channel.")
    link.hello()
    enable_with_warning(link, args.yes)
    try:
        apply_bench_params(link, channel, args.speed, args.accel)
        for index in range(args.times):
            for steps in (amount, -amount):
                result = do_move(link, channel, steps, args.speed, args.accel)
                if not result.landed:
                    raise BenchError(
                        "ch %d moved %d microsteps when asked for %d"
                        % (channel, result.believed, steps)
                    )
            print("  pass %d of %d done" % (index + 1, args.times))
        print()
        report_positions(link.status()["pos"], highlight=channel)
        print("\n  Back where it started, as far as the firmware is concerned.")
    finally:
        if not args.keep_enabled:
            link.release()
            print("\n  drivers released.")
    return 0


def cmd_monitor(link: Link, args) -> int:
    """Ping and print events until Ctrl-C.  Drives nothing."""
    heading("Monitoring")
    link.hello()
    link.verbose = True
    print("  Pinging every %.0f s so the watchdog stays fed." % PING_INTERVAL_S)
    print("  Events are printed as they arrive. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(0.2)
            link.check_reset()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


#: The scripted channel check, as stages.  Each is (microsteps, what to look for).
CHECK_STAGES = (
    (
        16,
        "16 microsteps -- one full step, 1.8 deg. Almost nothing, but the shaft\n"
        "     should twitch and the motor should not buzz or sit still.",
    ),
    (
        -16,
        "back the same 16. This is the direction change: DIR flips, and the shaft\n"
        "     should return to where it was, not carry on the same way.",
    ),
    (
        800,
        "a quarter turn forward. Put a mark on the shaft first -- tape will do --\n"
        "     and it should end up a clean quarter turn round.",
    ),
    (
        -800,
        "a quarter turn back. The mark should land exactly where it started.",
    ),
    (
        MICROSTEPS_PER_REV,
        "one full revolution forward. This is the one that catches lost steps:\n"
        "     the mark must come back to itself. If it is short, the motor is\n"
        "     missing steps -- see what to do about it at the end.",
    ),
)


def cmd_check(link: Link, args) -> int:
    """Walk one channel through the bring-up check, asking as it goes."""
    channel = args.ch
    heading("Channel check")
    print("  " + channel_label(channel))
    print()
    print("  Before starting, from docs/build-board.html:")
    print("   * Vref is set on this driver and 12 V was off while you set it.")
    print("   * this motor was plugged in with the 12 V supply OFF.")
    print("   * the driver is the right way round: ENABLE at the low-numbered row.")
    if not ask("All three true?", args.yes):
        raise BenchError("stopping. Nothing was enabled.")

    info = link.hello()
    print("\n  firmware %s, protocol %s, %d channels, booted %s"
          % (info["fw"], info["proto"], info["channels"], info["boot_reason"]))
    if channel >= info["channels"]:
        raise BenchError(
            "this firmware reports %d channels, so ch %d does not exist"
            % (info["channels"], channel)
        )

    state = link.status()
    if state["moving"][channel]:
        raise BenchError("ch %d is already moving; run `bench.py off` first" % channel)

    enable_with_warning(link, args.yes)
    failures: list[str] = []
    try:
        apply_bench_params(link, channel, args.speed, args.accel)
        link.command("set_pos", ch=channel, pos=0)
        print("  called this position zero, so the numbers below are easy to read.")

        for index, (steps, what) in enumerate(CHECK_STAGES, start=1):
            print("\n  -- stage %d of %d --" % (index, len(CHECK_STAGES)))
            print("     %s" % what)
            pause("Press enter to move.", args.yes)
            result = do_move(link, channel, steps, args.speed, args.accel)
            print()
            result.report()
            if not result.landed:
                failures.append(
                    "stage %d: the firmware's own arithmetic came out wrong "
                    "(asked %d, moved %d). That is a firmware bug, not a wiring "
                    "one -- please report it." % (index, steps, result.believed)
                )
            if not ask("Did the motor do that?", args.yes):
                failures.append("stage %d: you said the motor did not do that." % index)
                if not ask("Carry on anyway?", args.yes):
                    break

        print("\n  -- back to the start --")
        final = link.status()["pos"][channel]
        if final != 0:
            result = do_move(link, channel, -final, args.speed, args.accel)
            result.report()
        print()
        report_positions(link.status()["pos"], highlight=channel)
    finally:
        link.release()
        print("\n  drivers released; this motor no longer holds.")

    heading("Result")
    if failures:
        print("  This channel is NOT signed off:")
        for line in failures:
            print("   * %s" % line)
        print()
        print("  Worth checking, roughly in order of likelihood:")
        print("   * nothing moved at all: ENABLE is one wire for all six drivers.")
        print("     Meter GP14 (pin 19) -- it must be near 0 V while enabled.")
        print("     Then check this driver's STEP and DIR wires against the table")
        print("     above, and that VMOT really has 12 V on it.")
        print("   * it buzzes and does not turn: one coil is miswired. The pairs")
        print("     are RED/RED-WHT and GRN/GRN-WHT; each pair reads about 1.5 ohm")
        print("     across itself and open to the other. Power 12 V off first.")
        print("   * it turns the wrong way: that is fine and expected on some")
        print("     channels. Note it; the controller has a per-hand direction.")
        print("   * it turns but comes up short on the full revolution: lost")
        print("     steps. Raise Vref a little, or lower --speed and --accel and")
        print("     run this again. The default here is already half the")
        print("     firmware's own default.")
        return 1
    print("  ch %d did everything asked of it, at %.0f microsteps/s." % (channel, args.speed))
    print("  Both the motor and the firmware's arithmetic agree with each other.")
    print()
    print("  Next: power the 12 V off, plug in the next motor, power on, and run")
    print("  this again with --ch %d." % (channel + 1 if channel + 1 < 6 else 0))
    print()
    print("  This says the channel works at a bench speed. It does not say the")
    print("  hand will keep its position for a month -- nothing can, open loop.")
    return 0


REPL_HELP = """\
  <n>            jog the current channel by n microsteps (negative goes back)
  ch <n>         change channel (0-5)
  rev [n]        jog n revolutions, default 1
  deg <n>        jog n degrees
  pos            what the firmware believes
  on / off       enable / release all six drivers
  stop           decelerate the current channel
  zero           call this position zero on the current channel
  speed <n>      set max speed, microsteps/s
  accel <n>      set acceleration
  info           firmware identity
  help / quit
"""


def cmd_repl(link: Link, args) -> int:
    """Short commands against one connection, for poking at a channel."""
    heading("Bench REPL")
    link.hello()
    channel = args.ch
    speed, accel = args.speed, args.accel
    print("  channel %d. `help` for commands, `quit` to leave." % channel)
    print("  Leaving releases the drivers.")
    apply_bench_params(link, None, speed, accel)
    try:
        while True:
            link.check_reset()
            try:
                raw = input("ch%d> " % channel).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            word, _, rest = raw.partition(" ")
            rest = rest.strip()
            try:
                if word in ("quit", "q", "exit"):
                    break
                elif word in ("help", "h", "?"):
                    print(REPL_HELP)
                elif word == "ch":
                    new = int(rest)
                    if not 0 <= new < 6:
                        print("  channel must be 0-5")
                    else:
                        channel = new
                        print("  " + channel_label(channel))
                elif word == "pos":
                    report_positions(link.status()["pos"], highlight=channel)
                elif word == "on":
                    enable_with_warning(link, args.yes)
                elif word == "off":
                    link.command("stop")
                    link.enable(False)
                    print("  released.")
                elif word == "stop":
                    link.command("stop", ch=channel)
                    print("  stopping.")
                elif word == "zero":
                    link.command("set_pos", ch=channel, pos=0)
                    print("  ch %d is now zero." % channel)
                elif word == "info":
                    cmd_info(link, args)
                elif word == "speed":
                    speed = float(rest)
                    apply_bench_params(link, None, speed, accel)
                elif word == "accel":
                    accel = float(rest)
                    apply_bench_params(link, None, speed, accel)
                elif word in ("rev", "revs"):
                    count = float(rest) if rest else 1.0
                    do_move(
                        link, channel, int(round(count * MICROSTEPS_PER_REV)), speed, accel
                    ).report()
                elif word == "deg":
                    steps = int(round(float(rest) * MICROSTEPS_PER_REV / 360.0))
                    do_move(link, channel, steps, speed, accel).report()
                else:
                    do_move(link, channel, int(word), speed, accel).report()
            except ValueError:
                print("  did not understand %r; try `help`" % raw)
            except CommandFailed as error:
                print("  %s" % error)
            except BenchError as error:
                print("  %s" % error)
    finally:
        link.release()
        print("  drivers released.")
    return 0


# -- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description=(
            "Bench bring-up for the Weasley Clock's six A4988 channels. "
            "UNTESTED ON HARDWARE: only ever run against tools/pico_sim.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical first session:\n"
            "  python tools/bench.py info                 # Pico alone, nothing driven\n"
            "  python tools/bench.py check --ch 0         # one motor, scripted check\n"
            "  python tools/bench.py off                  # release everything\n"
        ),
    )
    parser.add_argument(
        "--port",
        default="auto",
        help="serial port, 'auto' to find one, or 'sim' for the simulator",
    )
    parser.add_argument("--baud", type=int, default=BAUD, help="default %d" % BAUD)
    parser.add_argument(
        "--speed",
        type=float,
        default=BENCH_SPEED,
        help="microsteps/s, default %.0f (half the firmware default, so you can see it)"
        % BENCH_SPEED,
    )
    parser.add_argument(
        "--accel", type=float, default=BENCH_ACCEL, help="microsteps/s^2, default %.0f"
        % BENCH_ACCEL,
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="answer every prompt yes (for scripts)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print every line")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_channel(sp, default=0):
        sp.add_argument(
            "--ch", type=int, default=default, choices=range(6), help="channel 0-5"
        )

    def add_keep(sp):
        sp.add_argument(
            "--keep-enabled",
            action="store_true",
            help="leave the drivers energised when done (they hold position)",
        )

    sub.add_parser("info", help="identify the firmware; drives nothing")
    sub.add_parser("pos", help="print live positions and motion state")
    sub.add_parser("off", help="stop every channel and release every driver")
    sub.add_parser("monitor", help="ping and print events until Ctrl-C")

    p_check = sub.add_parser("check", help="scripted bring-up check for one channel")
    add_channel(p_check)

    p_step = sub.add_parser("step", help="move one channel by n microsteps")
    add_channel(p_step)
    p_step.add_argument("steps", type=int, help="signed microsteps; 3200 is one turn")
    add_keep(p_step)

    p_wiggle = sub.add_parser("wiggle", help="rock a channel so you can spot it")
    add_channel(p_wiggle)
    p_wiggle.add_argument("--steps", type=int, default=160, help="amplitude, default 160")
    p_wiggle.add_argument("--times", type=int, default=3, help="default 3")
    add_keep(p_wiggle)

    p_repl = sub.add_parser("repl", help="interactive short commands")
    add_channel(p_repl)

    return parser


HANDLERS = {
    "info": cmd_info,
    "pos": cmd_pos,
    "off": cmd_off,
    "step": cmd_step,
    "wiggle": cmd_wiggle,
    "check": cmd_check,
    "monitor": cmd_monitor,
    "repl": cmd_repl,
}


def main(argv: list[str] | None = None) -> int:
    """Open the port, run one subcommand, and always release the drivers."""
    args = build_parser().parse_args(argv)
    try:
        transport, name = open_transport(args.port, args.baud)
    except BenchError as error:
        print("error: %s" % error, file=sys.stderr)
        return 2

    link = Link(transport, name, verbose=args.verbose)
    status = 1
    try:
        status = HANDLERS[args.command](link, args)
    except KeyboardInterrupt:
        print("\ninterrupted: stopping the hands and releasing the drivers.")
        link.release()
        status = 130
    except BenchError as error:
        print("\nerror: %s" % error, file=sys.stderr)
        link.release()
        status = 2
    except Exception:
        # A bug in this tool must still not leave six coils energised.
        link.release()
        raise
    finally:
        link.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
