"""Weasley Clock Pico firmware: the hardware layer.

Runs on a plain RP2040 Pico (not a W) under MicroPython, connected to the Pi by
USB.  It owns the twelve STEP/DIR pins and the one shared ENABLE line, and
nothing else: every decision about what a command means lives in :mod:`core`,
and every decision about how a move ramps lives in :mod:`motion`, both of which
are plain Python that the desktop tests and ``tools/pico_sim.py`` run unchanged.

Copy ``motion.py``, ``protocol.py``, ``core.py`` and this file to the Pico's
filesystem root (``mpremote connect /dev/ttyACM0 fs cp ... :``); MicroPython
runs ``main.py`` at boot.

Pins, from ``docs/circuit-diagram.html`` Diagram 2.  Channel order is hub level
order, which is also the controller's default person order:

===  =======  ======  =====  ========
Ch   Level    Person  STEP   DIR
===  =======  ======  =====  ========
0    1        Becky   GP2    GP3
1    2        Evan    GP4    GP5
2    3        Elijah  GP6    GP7
3    4        Caleb   GP8    GP9
4    5        Austin  GP10   GP11
5    6        Micah   GP12   GP13
===  =======  ======  =====  ========

ENABLE for all six A4988s is GP14 and is **active low**: 0 enables the drivers,
1 releases them (and with them the holding torque).

Why a software step loop and not PIO
------------------------------------
``docs/hardware-plan.html`` sketched one PIO state machine per motor.  This
firmware instead pulses from the main loop against per-axis microsecond
deadlines, because the numbers make PIO unnecessary *at the speeds this clock
uses*: six hands at the default 800 microsteps/s is 4800 pulses/s in total, one
every ~208 us, and in exchange the whole real-time path is ordinary Python that
pytest can run on a laptop.  Acceleration also means every step interval
differs, so a PIO program would need the CPU to feed it a new delay per step
anyway.

The headroom is smaller than it looks, and this docstring used to overstate it
as "roughly a hundred times" by comparing that 208 us against a bare loop
iteration.  What actually has to fit in 208 us is :meth:`motion.Axis.take_step`
-- a square root and a dozen float operations -- plus the STEP pulse width, the
serial reader and the housekeeping.  ``tools/motion_check.py`` measures the
planner at under a microsecond per pulse on a desktop and puts the Pico at
single-digit headroom at the default rate, which is enough and is not a hundred
times.  At :data:`motion.SPEED_LIMIT` it would not fit at all, which is one
more reason that ceiling is a limit the drivers survive rather than a speed to
use.  If a future version wants much higher speeds,
:class:`motion.MotionController` already separates "when is the next step due"
from "make a pulse", so a PIO backend can replace :meth:`Hardware.pulse` alone.

A loop that does fall behind no longer loses steps over it: see
:data:`motion.MAX_CATCHUP_INTERVALS`.  It runs the hands slow and says so
through ``late`` in ``status``.  See ``docs/ARCHITECTURE.md``.
"""

import sys

import core
import protocol

try:
    import machine
    import time
    import uselect as select
except ImportError:  # pragma: no cover - importing this file on a desktop
    machine = None
    select = None
    import time

#: (STEP, DIR) GPIO numbers per channel, hub level 1 to 6.
STEP_DIR_PINS = (
    (2, 3),
    (4, 5),
    (6, 7),
    (8, 9),
    (10, 11),
    (12, 13),
)

#: Shared ENABLE pin for all six A4988s, active low.
ENABLE_PIN = 14

#: STEP pulse width.  The A4988 needs 1 us minimum; the spec asks for 5 us, and
#: at 4800 pulses/s that is a 2.4% duty on the loop, which is affordable.
PULSE_US = 5

#: Where positions are kept across a reset.
POSITION_FILE = "positions.json"

#: Milliseconds between pings before the Pico's own hardware watchdog bites.
#: Generous, because :class:`core.PicoCore` already stops the hands on silence;
#: this one only catches the firmware itself hanging.
HW_WATCHDOG_MS = 8000


class FileStore:
    """Position persistence in a small JSON file on the Pico's flash.

    :class:`core.PicoCore` only calls :meth:`save` at rest and no more than once
    every ten seconds, which is what keeps this from wearing the flash out.

    Surviving a power cut mid-write
    -------------------------------
    The clock loses power without warning -- that is the normal way it is turned
    off -- and it may well do so while this file is being written, because a
    write follows every move.  So the file is never overwritten in place.  A new
    copy is written beside it and then swapped in, and :meth:`load` reads
    whichever of the two survived:

    * power cut while writing ``.tmp``: the real file is untouched and holds the
      previous positions, which are at most one move out of date;
    * power cut during the swap: the real file may be gone, but ``.tmp`` holds a
      complete newer copy and :meth:`load` falls back to it;
    * flash full or worn out: the write is read back before anything is swapped,
      so a write that kept nothing cannot destroy the good copy.

    What none of that fixes is a hand that was turned, or a motor that slipped,
    while the power was off -- no stored number can know about that, which is
    why every boot sets ``needs_verification``.  :attr:`source` says which of
    the cases above happened, so the controller can tell "nothing was ever
    saved" from "something was saved and could not be read".
    """

    #: Nothing was found: a Pico that has never saved, or a wiped filesystem.
    SOURCE_NONE = "none"
    #: The ordinary case: the primary file was read.
    SOURCE_FLASH = "flash"
    #: The primary file was missing or bad and the ``.tmp`` copy was used.
    SOURCE_RECOVERED = "recovered"
    #: Something was there and could not be parsed.  Positions are all zero and
    #: are not merely unverified but unknown.
    SOURCE_CORRUPT = "corrupt"

    def __init__(self, path=POSITION_FILE):
        self.path = path
        self.tmp = path + ".tmp"
        self.source = self.SOURCE_NONE
        self.write_failures = 0

    def _read(self, path):
        """Return a list of ints, the string ``"corrupt"``, or ``None``."""
        try:
            with open(path) as handle:
                data = handle.read()
        except OSError:
            return None
        try:
            import json

            positions = json.loads(data)
        except (ValueError, TypeError):
            return "corrupt"
        if (
            isinstance(positions, list)
            and positions
            and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in positions
            )
        ):
            return [int(v) for v in positions]
        return "corrupt"

    def load(self):
        """Return the saved positions, or ``None`` if there is nothing usable."""
        primary = self._read(self.path)
        if isinstance(primary, list):
            self.source = self.SOURCE_FLASH
            return primary
        spare = self._read(self.tmp)
        if isinstance(spare, list):
            self.source = self.SOURCE_RECOVERED
            return spare
        if primary == "corrupt" or spare == "corrupt":
            self.source = self.SOURCE_CORRUPT
        else:
            self.source = self.SOURCE_NONE
        return None

    def save(self, positions):
        """Write ``positions`` so that a power cut cannot lose both copies.

        Raises:
            OSError: the new copy did not read back.  The old one is still
                there; :meth:`core.PicoCore._maybe_flash` will try again.
        """
        import json
        import os

        wanted = [int(v) for v in positions]
        payload = json.dumps(wanted)
        with open(self.tmp, "w") as handle:
            handle.write(payload)
        if self._read(self.tmp) != wanted:
            # A full or worn flash can accept a write and keep nothing of it.
            # Overwriting the good copy with that is how the positions you were
            # trying to protect get lost.
            self.write_failures += 1
            raise OSError("positions did not read back from %s" % self.tmp)
        try:
            os.rename(self.tmp, self.path)
        except OSError:
            # Some ports will not rename onto a file that exists.  Removing
            # first leaves a moment with no primary file, which is exactly the
            # moment load() covers by reading the .tmp copy.
            try:
                os.remove(self.path)
            except OSError:
                pass
            os.rename(self.tmp, self.path)
        self.source = self.SOURCE_FLASH


class Hardware:
    """The twelve STEP/DIR pins, the ENABLE line and the microsecond clock."""

    def __init__(self):
        self.steps = []
        self.dirs = []
        self.last_dir = []
        for step_gpio, dir_gpio in STEP_DIR_PINS:
            self.steps.append(machine.Pin(step_gpio, machine.Pin.OUT, value=0))
            self.dirs.append(machine.Pin(dir_gpio, machine.Pin.OUT, value=0))
            self.last_dir.append(None)
        # Active low: start at 1 so the drivers are released until the
        # controller asks for them.  A hand is then free to be turned by hand,
        # which is what the zeroing wizard needs.
        self.enable = machine.Pin(ENABLE_PIN, machine.Pin.OUT, value=1)
        self._wrap_base = 0
        self._last_ticks = time.ticks_us()

    def now_us(self):
        """A monotonic microsecond clock that does not wrap.

        ``time.ticks_us()`` wraps at 2**30 on MicroPython (about 17.9 minutes),
        which a clock that runs for months cannot use directly.  Accumulating
        ``ticks_diff`` gives an ever-increasing Python int instead, and the
        planner only ever sees that.
        """
        now = time.ticks_us()
        self._wrap_base += time.ticks_diff(now, self._last_ticks)
        self._last_ticks = now
        return self._wrap_base

    def pulse(self, channel, direction):
        """Emit one STEP pulse on ``channel`` in ``direction`` (+1 or -1)."""
        want = 1 if direction > 0 else 0
        if self.last_dir[channel] != want:
            self.dirs[channel].value(want)
            self.last_dir[channel] = want
            # A4988 DIR setup time is 200 ns; one bytecode is longer than that,
            # but be explicit about it for the reader.
            time.sleep_us(1)
        step = self.steps[channel]
        step.value(1)
        time.sleep_us(PULSE_US)
        step.value(0)

    def set_enable(self, on):
        """Drive the shared ENABLE line.  ``on=True`` means 0 (active low)."""
        self.enable.value(0 if on else 1)


def boot_reason():
    """Map ``machine.reset_cause()`` to a protocol boot reason."""
    if machine is None:  # pragma: no cover
        return protocol.BOOT_UNKNOWN
    try:
        cause = machine.reset_cause()
    except Exception:  # pragma: no cover
        return protocol.BOOT_UNKNOWN
    mapping = {
        getattr(machine, "PWRON_RESET", -1): protocol.BOOT_POWER_ON,
        getattr(machine, "HARD_RESET", -2): protocol.BOOT_RESET,
        getattr(machine, "WDT_RESET", -3): protocol.BOOT_WATCHDOG,
        getattr(machine, "SOFT_RESET", -4): protocol.BOOT_SOFT,
    }
    return mapping.get(cause, protocol.BOOT_UNKNOWN)


class SerialLink:
    """Line-buffered reader and writer for the USB CDC port.

    Reads are non-blocking: :meth:`poll_line` returns whatever complete lines
    have arrived and nothing else, so the step loop is never held up waiting for
    the Pi.  Over-long lines are dropped rather than growing without bound.
    """

    MAX_LINE = 512

    #: Most bytes one :meth:`poll_lines` call will take off the port.
    #:
    #: Without a bound, a host that writes faster than the Pico can execute
    #: keeps this loop fed forever and the step loop never runs again.  512
    #: bytes is a whole maximum-length line, so a well-behaved host is never
    #: slowed down, and a flood is served a line at a time between step
    #: deadlines instead of starving them.
    MAX_READ = 512

    def __init__(self):
        self.buffer = b""
        self.poller = select.poll()
        self.poller.register(sys.stdin, select.POLLIN)
        self.dropped = 0

    def poll_lines(self):
        """Return a list of complete lines received since the last call."""
        lines = []
        read = 0
        while read < self.MAX_READ and self.poller.poll(0):
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                break
            read += 1
            self.buffer += chunk
            if len(self.buffer) > self.MAX_LINE and b"\n" not in self.buffer:
                self.buffer = b""
                self.dropped += 1
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.strip():
                lines.append(line)
        return lines

    def send(self, message):
        """Write one message as a JSON line."""
        try:
            sys.stdout.write(protocol.encode(message).decode("utf-8"))
        except Exception:  # pragma: no cover - host closed the port
            pass


def run():  # pragma: no cover - needs a Pico
    """Boot, then loop forever: service steps, read commands, do housekeeping.

    UNTESTED ON HARDWARE: no Pico was connected when this was written.
    """
    hardware = Hardware()

    # Stop MicroPython reading the data stream for Ctrl-C.  Without this, one
    # 0x03 byte on the wire -- a serial terminal, a stray keystroke, a host
    # program being helpful -- raises KeyboardInterrupt inside the step loop and
    # drops to the REPL, leaving ENABLE low and six coils energised with nothing
    # feeding the watchdog.  There is no console on this port in service: the
    # Pi speaks JSON to it.
    try:
        import micropython

        micropython.kbd_intr(-1)
    except (ImportError, AttributeError):
        pass

    link = SerialLink()
    pico = core.PicoCore(
        channels=len(STEP_DIR_PINS),
        pulse=hardware.pulse,
        set_enable=hardware.set_enable,
        store=FileStore(),
        boot_reason=boot_reason(),
        send=link.send,
    )
    pico.start(hardware.now_us())

    watchdog = None
    try:
        watchdog = machine.WDT(timeout=HW_WATCHDOG_MS)
    except Exception:
        # Not every port supports WDT; the core's own watchdog still applies.
        watchdog = None

    try:
        while True:
            now = hardware.now_us()
            # Steps first: they are the only thing with a deadline.
            pico.tick(now)
            for line in link.poll_lines():
                pico.handle_line(line, hardware.now_us())
            if watchdog is not None:
                watchdog.feed()
            # Sleep only when nothing is moving, so an idle clock does not spin
            # a core at 100%.  While moving, the loop runs flat out.
            if not pico.mc.any_moving():
                time.sleep_ms(2)
    finally:
        # However this loop ends -- a bug here, a MemoryError, a reset request
        # -- do not walk away with the drivers on.  Six energised coils with
        # nobody stepping them is a quarter of an amp each into a motor that is
        # going nowhere, and the hardware watchdog will not reset us for eight
        # seconds.  Releasing ENABLE costs the holding torque, which a clock
        # hand does not need, and the position was already a belief.
        try:
            hardware.set_enable(False)
            link.send(
                protocol.event(
                    "fault",
                    code=protocol.FAULT_INTERNAL,
                    msg="firmware loop exited; drivers released",
                    pos=pico.mc.positions(),
                )
            )
        except Exception:
            pass


if __name__ == "__main__":  # pragma: no cover
    run()
