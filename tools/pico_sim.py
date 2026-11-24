"""A fake Pico that speaks the real protocol.

The simulator imports ``core``, ``motion`` and ``protocol`` straight out of
``firmware/pico/``, so it is not a reimplementation: it is the firmware with the
pin layer swapped for a counter.  That is what makes ``--sim`` mode worth
trusting -- if the simulator accepts a command, the Pico's parser accepted it
too, and the step counts come out of the same planner.

Two ways to use it:

* **In process**, which is what ``clockd --sim`` does: build a :class:`SimPico`
  and pump bytes through :meth:`SimPico.write` / :meth:`SimPico.read`.  No
  serial port, no subprocess, and written to work the same on Windows
  and Linux (only Linux exercised so far).
* **Standalone over stdio**, for poking at it by hand::

      python tools/pico_sim.py            # then type JSON lines
      echo '{"seq":1,"cmd":"info"}' | python tools/pico_sim.py --once

Fault injection exists so the UI's "Pico is unhappy" paths can be exercised
without unplugging anything: see ``--reset-after``, ``--drop-rate`` and
``--garbage-rate``.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PICO_DIR = REPO_ROOT / "firmware" / "pico"
if str(PICO_DIR) not in sys.path:
    sys.path.insert(0, str(PICO_DIR))

import core  # noqa: E402
import protocol  # noqa: E402

DEFAULT_CHANNELS = 6


class MemoryStore:
    """Position store that keeps the list in RAM (a fresh Pico every run)."""

    def __init__(self, positions: list[int] | None = None):
        self.positions = list(positions) if positions else None
        self.writes = 0

    def load(self) -> list[int] | None:
        return list(self.positions) if self.positions else None

    def save(self, positions) -> None:
        self.positions = [int(v) for v in positions]
        self.writes += 1


class JsonFileStore(MemoryStore):
    """Position store backed by a JSON file, so a restart keeps positions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        loaded = None
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, list):
                    loaded = [int(v) for v in data]
            except (OSError, ValueError, TypeError):
                loaded = None
        super().__init__(loaded)

    def save(self, positions) -> None:
        super().save(positions)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.positions))


class MonotonicClock:
    """Wall-clock microseconds, optionally sped up or slowed down.

    Args:
        scale: Multiplier on elapsed time.  ``10.0`` makes the simulated clock
            run ten times faster, which is how the integration tests watch a
            60-second sweep finish in six seconds.
    """

    def __init__(self, scale: float = 1.0):
        self.scale = float(scale)
        self._start = time.monotonic()

    def __call__(self) -> int:
        return int((time.monotonic() - self._start) * 1_000_000 * self.scale)


class FakeClock:
    """A clock the tests drive by hand, in microseconds."""

    def __init__(self, now_us: int = 0):
        self.now_us = int(now_us)

    def __call__(self) -> int:
        return self.now_us

    def advance(self, microseconds: int) -> None:
        """Move the clock forward."""
        self.now_us += int(microseconds)

    def advance_s(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self.now_us += int(seconds * 1_000_000)


class SimPico:
    """A Pico with no pins: the firmware core driven by a software clock.

    Args:
        channels: Number of axes, 6 on the clock.
        clock: Callable returning monotonic microseconds.
        store: Position store; defaults to in-memory.
        boot_reason: What the ``boot`` event should claim.
        reset_after_s: Pretend to brown out this many seconds after boot, once.
        drop_rate: Fraction of outgoing lines to silently discard (0..1).
        garbage_rate: Fraction of outgoing lines to corrupt (0..1).
        seed: Seed for the fault injection, so a failing run can be repeated.
    """

    def __init__(
        self,
        channels: int = DEFAULT_CHANNELS,
        clock: Callable[[], int] | None = None,
        store=None,
        boot_reason: str = protocol.BOOT_POWER_ON,
        reset_after_s: float | None = None,
        drop_rate: float = 0.0,
        garbage_rate: float = 0.0,
        seed: int | None = None,
    ):
        self.channels = channels
        self.clock = clock or MonotonicClock()
        self.store = store if store is not None else MemoryStore()
        self.initial_boot_reason = boot_reason
        self.reset_after_s = reset_after_s
        self.drop_rate = drop_rate
        self.garbage_rate = garbage_rate
        self.random = random.Random(seed)

        self._out = bytearray()
        self._in = bytearray()
        self.steps_by_channel = [0] * channels
        self.directions: list[int | None] = [None] * channels
        self.enable_history: list[bool] = []
        self.lines_sent = 0
        self.lines_dropped = 0
        self.resets = 0
        self._boot_us = 0
        self._has_reset = False
        self.core = self._make_core(boot_reason)
        self.core.start(self.clock())
        self._boot_us = self.clock()

    # -- construction ----------------------------------------------------

    def _make_core(self, boot_reason: str) -> core.PicoCore:
        return core.PicoCore(
            channels=self.channels,
            pulse=self._pulse,
            set_enable=self._set_enable,
            store=self.store,
            boot_reason=boot_reason,
            send=self._send,
        )

    def _pulse(self, channel: int, direction: int) -> None:
        self.steps_by_channel[channel] += 1
        self.directions[channel] = direction

    def _set_enable(self, on: bool) -> None:
        self.enable_history.append(bool(on))

    def _send(self, message: dict) -> None:
        self.lines_sent += 1
        if self.drop_rate and self.random.random() < self.drop_rate:
            self.lines_dropped += 1
            return
        raw = protocol.encode(message)
        if self.garbage_rate and self.random.random() < self.garbage_rate:
            raw = b"{this is not json" + raw
        self._out += raw

    # -- the host side of the "serial port" ------------------------------

    def write(self, data: bytes) -> int:
        """Host sends bytes to the Pico.  Complete lines are executed at once."""
        self._in += data
        while b"\n" in self._in:
            line, _, rest = bytes(self._in).partition(b"\n")
            self._in = bytearray(rest)
            if line.strip():
                self.core.handle_line(line, self.clock())
        return len(data)

    def read(self, size: int = -1) -> bytes:
        """Host reads whatever the Pico has produced.  Never blocks."""
        self.service()
        if size is None or size < 0 or size >= len(self._out):
            out = bytes(self._out)
            self._out = bytearray()
            return out
        out = bytes(self._out[:size])
        del self._out[:size]
        return out

    @property
    def in_waiting(self) -> int:
        """Bytes available to read, mirroring ``pyserial``'s attribute."""
        self.service()
        return len(self._out)

    def service(self) -> None:
        """Advance simulated motion and housekeeping to the current time."""
        now = self.clock()
        if (
            self.reset_after_s is not None
            and not self._has_reset
            and (now - self._boot_us) / 1e6 >= self.reset_after_s
        ):
            self._has_reset = True
            self.reset(protocol.BOOT_RESET)
            return
        self.core.tick(now)

    def reset(self, boot_reason: str = protocol.BOOT_RESET) -> None:
        """Simulate a Pico reset: positions reload from the store, hands stop.

        What the controller must survive: a ``boot`` event out of nowhere,
        ``needs_verification`` true again, and in-flight command sequence
        numbers never answered.
        """
        self.resets += 1
        self._out = bytearray()
        self._in = bytearray()
        self.core = self._make_core(boot_reason)
        self.core.start(self.clock())
        self._boot_us = self.clock()

    # -- convenience for tests -------------------------------------------

    def send_command(self, **command) -> None:
        """Write one command dict as a line."""
        self.write(protocol.encode(command))

    def read_messages(self) -> list[dict]:
        """Drain the output and decode every complete line."""
        raw = self.read()
        out = []
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                out.append(protocol.decode(line))
            except protocol.ProtocolError:
                continue
        return out

    def positions(self) -> list[int]:
        """Current microstep positions."""
        return self.core.mc.positions()

    def run_until_idle(self, limit_s: float = 300.0) -> None:
        """Advance a :class:`FakeClock` until every axis is at rest."""
        if not isinstance(self.clock, FakeClock):
            raise TypeError("run_until_idle needs a FakeClock")
        deadline = self.clock.now_us + int(limit_s * 1_000_000)
        while self.core.mc.any_moving():
            due = self.core.mc.next_deadline()
            if due is None:
                break
            self.clock.now_us = max(due, self.clock.now_us + 1)
            if self.clock.now_us > deadline:
                raise TimeoutError("axes still moving after %.1f s" % limit_s)
            self.core.tick(self.clock())
        self.core.tick(self.clock())


# -- standalone stdio mode ----------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the simulator over stdin/stdout, one JSON line in, lines out."""
    parser = argparse.ArgumentParser(description="Fake Raspberry Pi Pico for the Weasley Clock.")
    parser.add_argument("--channels", type=int, default=DEFAULT_CHANNELS)
    parser.add_argument("--scale", type=float, default=1.0, help="simulated time multiplier")
    parser.add_argument("--state", type=Path, default=None, help="JSON file to keep positions in")
    parser.add_argument("--reset-after", type=float, default=None, metavar="S")
    parser.add_argument("--drop-rate", type=float, default=0.0)
    parser.add_argument("--garbage-rate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--once", action="store_true", help="read stdin to EOF, then exit")
    args = parser.parse_args(argv)

    pico = SimPico(
        channels=args.channels,
        clock=MonotonicClock(args.scale),
        store=JsonFileStore(args.state) if args.state else MemoryStore(),
        reset_after_s=args.reset_after,
        drop_rate=args.drop_rate,
        garbage_rate=args.garbage_rate,
        seed=args.seed,
    )

    def flush() -> None:
        data = pico.read()
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    flush()
    if args.once:
        for line in sys.stdin.buffer:
            pico.write(line if line.endswith(b"\n") else line + b"\n")
            flush()
        # Let any started move finish so the caller sees the final position.
        for _ in range(200):
            flush()
            if not pico.core.mc.any_moving():
                break
            time.sleep(0.05)
        flush()
        return 0

    # Interactive: a blocking read on stdin would stall motion, so read a line
    # at a time and flush between, which is good enough for typing at it.
    try:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                break
            pico.write(line)
            flush()
    except KeyboardInterrupt:
        pass
    flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
