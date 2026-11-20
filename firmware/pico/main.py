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
deadlines, because the numbers make PIO unnecessary here: six hands at the
default 800 microsteps/s is 4800 pulses/s in total, one every ~208 us, while a
loop iteration on a 125 MHz RP2040 is a couple of microseconds.  The loop has
roughly a hundred times the headroom it needs, and in exchange the whole
real-time path is ordinary Python that pytest can run on a laptop.  Acceleration
also means every step interval differs, so a PIO program would need the CPU to
feed it a new delay per step anyway.  If a future version wants much higher
speeds, :class:`motion.MotionController` already separates "when is the next
step due" from "make a pulse", so a PIO backend can replace :func:`_pulse` alone.
See ``docs/ARCHITECTURE.md``.
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
    """

    def __init__(self, path=POSITION_FILE):
        self.path = path

    def load(self):
        """Return the saved positions, or ``None`` if there is nothing usable."""
        try:
            with open(self.path) as handle:
                data = handle.read()
        except OSError:
            return None
        try:
            import json

            positions = json.loads(data)
        except (ValueError, TypeError):
            return None
        if isinstance(positions, list) and all(
            isinstance(v, (int, float)) for v in positions
        ):
            return [int(v) for v in positions]
        return None

    def save(self, positions):
        """Write ``positions`` atomically enough for a brownout to be survivable."""
        import json

        payload = json.dumps([int(v) for v in positions])
        tmp = self.path + ".tmp"
        with open(tmp, "w") as handle:
            handle.write(payload)
        try:
            import os

            os.rename(tmp, self.path)
        except OSError:
            # No atomic rename (or the target exists on some ports): fall back.
            with open(self.path, "w") as handle:
                handle.write(payload)


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

    def __init__(self):
        self.buffer = b""
        self.poller = select.poll()
        self.poller.register(sys.stdin, select.POLLIN)
        self.dropped = 0

    def poll_lines(self):
        """Return a list of complete lines received since the last call."""
        lines = []
        while self.poller.poll(0):
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                break
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

    while True:
        now = hardware.now_us()
        # Steps first: they are the only thing with a deadline.
        pico.tick(now)
        for line in link.poll_lines():
            pico.handle_line(line, hardware.now_us())
        if watchdog is not None:
            watchdog.feed()
        # Sleep only when nothing is moving, so an idle clock does not spin a
        # core at 100%.  While moving, the loop runs flat out.
        if not pico.mc.any_moving():
            time.sleep_ms(2)


if __name__ == "__main__":  # pragma: no cover
    run()
