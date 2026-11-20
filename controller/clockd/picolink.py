"""The link to the Pico: transports, command queue, reconnection, resync.

Two transports implement the same three methods, so nothing above this layer
knows whether it is talking to hardware:

:class:`SerialTransport`
    A real Pico on ``/dev/ttyACM0`` (or ``COM3``), through pyserial.  Blocking
    reads and writes happen on a worker thread via :func:`asyncio.to_thread`,
    which keeps them off the event loop and works the same on Windows.

:class:`SimTransport`
    ``tools/pico_sim.py`` in this process.  No serial port, no subprocess, so
    ``--sim`` runs on a laptop with nothing plugged in.

:class:`PicoLink` sits on top: it frames lines, matches replies to commands by
``seq``, times out, retries, routes unsolicited events, pings so the firmware's
watchdog stays happy, and reconnects when the port disappears.

Resync after a Pico reset
-------------------------
If the Pico resets -- a brownout, someone unplugging USB, a firmware upload --
it comes back with ``needs_verification`` and possibly different positions.  The
link notices through the ``boot`` event (or through an ``info`` after a
reconnect), abandons every in-flight command, and raises the fact to the engine
so every hand is marked unverified.  It does not quietly carry on, because
after a reset the stored angle is a guess and acting on it would drive a hand
the wrong way round the dial.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .picolib import protocol

log = logging.getLogger(__name__)

#: Longest line we will buffer before assuming the stream is corrupt.
MAX_LINE = 4096


class LinkError(Exception):
    """The command could not be delivered or answered."""


class CommandFailed(LinkError):
    """The Pico answered with ``ok: false``.

    Attributes:
        code: The ``err`` code from the firmware.
        reply: The whole reply, for the log.
    """

    def __init__(self, code: str, message: str, reply: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.reply = reply or {}


class Transport:
    """What :class:`PicoLink` needs from a byte pipe."""

    #: Human-readable description for the UI.
    name: str = "none"

    async def open(self) -> None:
        """Open the port.  Raises on failure."""
        raise NotImplementedError

    async def close(self) -> None:
        """Close the port.  Must not raise."""
        raise NotImplementedError

    async def read(self) -> bytes:
        """Return whatever bytes are available, or ``b""`` after a short wait."""
        raise NotImplementedError

    async def write(self, data: bytes) -> None:
        """Write bytes to the port."""
        raise NotImplementedError


class SimTransport(Transport):
    """Transport backed by an in-process :class:`tools.pico_sim.SimPico`."""

    name = "simulator"

    def __init__(self, pico: Any, poll_interval_s: float = 0.01):
        self.pico = pico
        self.poll_interval_s = poll_interval_s
        self.opened = False

    async def open(self) -> None:
        """Nothing to open; the simulator is already running."""
        self.opened = True

    async def close(self) -> None:
        """Nothing to close."""
        self.opened = False

    async def read(self) -> bytes:
        """Drain the simulator's output, waiting briefly if there is none."""
        data = self.pico.read()
        if not data:
            await asyncio.sleep(self.poll_interval_s)
            data = self.pico.read()
        return data

    async def write(self, data: bytes) -> None:
        """Hand bytes to the simulator."""
        self.pico.write(data)

    def describe(self) -> dict[str, Any]:
        """Describe the transport for the UI."""
        return {"kind": "sim", "port": "in-process simulator", "open": self.opened}


class SerialTransport(Transport):
    """Transport backed by a real serial port through pyserial."""

    name = "serial"

    def __init__(self, port: str, baudrate: int = 115200, read_timeout_s: float = 0.05):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout_s = read_timeout_s
        self._serial: Any = None

    async def open(self) -> None:
        """Open the port on a worker thread."""
        import serial

        def _open():
            return serial.Serial(
                self.port,
                baudrate=self.baudrate,
                timeout=self.read_timeout_s,
                write_timeout=2.0,
            )

        self._serial = await asyncio.to_thread(_open)
        # A Pico that has just enumerated can emit a partial line; drop it.
        await asyncio.to_thread(self._serial.reset_input_buffer)

    async def close(self) -> None:
        """Close the port, ignoring errors from an already-gone device."""
        handle, self._serial = self._serial, None
        if handle is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(handle.close)

    async def read(self) -> bytes:
        """Read whatever is waiting, blocking at most ``read_timeout_s``."""
        handle = self._serial
        if handle is None:
            raise LinkError("serial port is not open")

        def _read():
            waiting = getattr(handle, "in_waiting", 0)
            if waiting:
                return handle.read(waiting)
            return handle.read(1)

        return await asyncio.to_thread(_read)

    async def write(self, data: bytes) -> None:
        """Write bytes, blocking at most the write timeout."""
        handle = self._serial
        if handle is None:
            raise LinkError("serial port is not open")
        await asyncio.to_thread(handle.write, data)

    def describe(self) -> dict[str, Any]:
        """Describe the transport for the UI."""
        return {
            "kind": "serial",
            "port": self.port,
            "baudrate": self.baudrate,
            "open": self._serial is not None,
        }


class PicoLink:
    """Framed, sequenced, self-healing access to the Pico.

    Args:
        transport_factory: Called with no arguments to build a fresh
            :class:`Transport` on every connection attempt.
        command_timeout_s: How long to wait for a reply before retrying.
        max_retries: Extra attempts after the first timeout.
        ping_interval_s: How often to ping so the firmware watchdog is fed.
        reconnect_interval_s: Delay between reconnection attempts.
        settle_s: How long to read and absorb whatever the device is already
            saying before introducing ourselves.  A Pico that has just
            enumerated has a ``boot`` event waiting, and reading it first means
            the ``hello`` is not cancelled by our own device's arrival.
        on_event: Called for every unsolicited event from the firmware.
        on_connection_change: Called with ``True``/``False`` as the link comes
            and goes, so the UI banner and the engine can react.
        channels: How many channels the hub has.
    """

    def __init__(
        self,
        transport_factory: Callable[[], Transport],
        *,
        command_timeout_s: float = 3.0,
        max_retries: int = 2,
        ping_interval_s: float = 3.0,
        reconnect_interval_s: float = 2.0,
        settle_s: float = 0.25,
        on_event: Callable[[dict], Awaitable[None] | None] | None = None,
        on_connection_change: Callable[[bool], Awaitable[None] | None] | None = None,
        channels: int = 6,
    ):
        self.transport_factory = transport_factory
        self.command_timeout_s = command_timeout_s
        self.max_retries = max_retries
        self.ping_interval_s = ping_interval_s
        self.reconnect_interval_s = reconnect_interval_s
        self.settle_s = settle_s
        self.on_event = on_event
        self.on_connection_change = on_connection_change
        self.channels = channels

        self.transport: Transport | None = None
        self.connected = False
        self.info: dict[str, Any] = {}
        self.positions: list[int] = [0] * channels
        self.moving: list[bool] = [False] * channels
        self.enabled = False
        self.needs_verification = True
        self.last_fault: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.boot_count = 0
        self.connect_count = 0
        self.commands_sent = 0
        self.timeouts = 0
        self.last_reply_at: float | None = None
        self.last_boot_at: float | None = None

        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._buffer = bytearray()
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        self._running = False
        self._reset_notice: Callable[[], Awaitable[None] | None] | None = None

    # -- lifecycle -------------------------------------------------------

    def set_reset_handler(self, handler: Callable[[], Awaitable[None] | None]) -> None:
        """Register what to do when the Pico is found to have reset."""
        self._reset_notice = handler

    async def start(self) -> None:
        """Start the supervisor, which connects and keeps the link up."""
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._supervise(), name="pico-supervise"),
            asyncio.create_task(self._ping_loop(), name="pico-ping"),
        ]

    async def close(self) -> None:
        """Shut the link down and close the port.

        Named ``close`` rather than ``stop`` on purpose: :meth:`stop` is the
        *motor* stop command, and the two must never be confused by a caller
        trying to bring the clock down safely.
        """
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        await self._drop_connection("shutting down")

    async def _supervise(self) -> None:
        """Connect, read until the link breaks, wait, repeat."""
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Pico connect failed: %s", exc)
                await asyncio.sleep(self.reconnect_interval_s)
                continue
            # The reader has to be running before anything is sent, or the
            # reply has nobody to receive it and every command times out.
            reader = asyncio.create_task(self._read_loop(), name="pico-read")
            try:
                await self._introduce()
            except asyncio.CancelledError:
                reader.cancel()
                raise
            except Exception as exc:
                log.warning("Pico hello failed: %s", exc)
            try:
                await reader
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("Pico link lost: %s", exc)
            finally:
                reader.cancel()
            await self._drop_connection(self.last_error or "link closed")
            if self._running:
                await asyncio.sleep(self.reconnect_interval_s)

    async def _connect(self) -> None:
        """Open a transport and introduce ourselves."""
        transport = self.transport_factory()
        await transport.open()
        self.transport = transport
        self.connected = True
        self.connect_count += 1
        self.last_error = None
        self._buffer.clear()
        log.info("Pico link up (%s)", transport.name)
        await self._notify_connection(True)
        await self._settle()

    async def _introduce(self) -> None:
        """Ask the freshly connected Pico who it is.

        Called once the reader is running.  A failure is logged rather than
        fatal: a Pico that is mid-boot may miss the first ``hello``, and the
        next ``ping`` or UI action will pick the identity up anyway.
        """
        info = await self.command("hello", timeout_s=self.command_timeout_s)
        await self._absorb_info(info)

    async def _settle(self) -> None:
        """Absorb whatever the device is already saying, before we speak.

        A Pico that just powered up has already written its ``boot`` event, and
        a Pico that has been running for a week has written nothing.  Either
        way, reading for a moment first means the boot event is handled as what
        it is -- the device announcing itself -- rather than as a reset that
        cancels the ``hello`` we would otherwise have in flight.
        """
        transport = self.transport
        if transport is None or self.settle_s <= 0:
            return
        deadline = time.monotonic() + self.settle_s
        while time.monotonic() < deadline and self.connected:
            try:
                data = await asyncio.wait_for(
                    transport.read(), max(0.01, deadline - time.monotonic())
                )
            except (TimeoutError, Exception):
                return
            if not data:
                continue
            self._buffer.extend(data)
            while b"\n" in self._buffer:
                line, _, rest = bytes(self._buffer).partition(b"\n")
                self._buffer = bytearray(rest)
                if line.strip():
                    await self._handle_line(line)

    async def _drop_connection(self, reason: str) -> None:
        """Tear down the current connection and fail everything in flight."""
        was_connected = self.connected
        self.connected = False
        transport, self.transport = self.transport, None
        if transport is not None:
            await transport.close()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(LinkError(f"link dropped: {reason}"))
        self._pending.clear()
        if was_connected:
            await self._notify_connection(False)

    async def _notify_connection(self, up: bool) -> None:
        if self.on_connection_change is None:
            return
        try:
            result = self.on_connection_change(up)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("connection-change handler failed")

    # -- reading ---------------------------------------------------------

    async def _read_loop(self) -> None:
        """Read bytes, split lines, dispatch."""
        while self._running and self.connected:
            transport = self.transport
            if transport is None:
                return
            data = await transport.read()
            if data:
                self._buffer.extend(data)
                if len(self._buffer) > MAX_LINE and b"\n" not in self._buffer:
                    log.warning("Pico sent %d bytes with no newline; discarding", len(self._buffer))
                    self._buffer.clear()
                while b"\n" in self._buffer:
                    line, _, rest = bytes(self._buffer).partition(b"\n")
                    self._buffer = bytearray(rest)
                    if line.strip():
                        await self._handle_line(line)

    async def _handle_line(self, line: bytes) -> None:
        """Decode one line and route it to a waiter or to the event handler."""
        try:
            message = protocol.decode(line)
        except protocol.ProtocolError as exc:
            # A corrupt line is normal right after a reset; do not drop the link.
            log.debug("ignoring unparseable line from Pico: %s", exc.msg)
            return

        if protocol.is_reply(message):
            self.last_reply_at = time.time()
            future = self._pending.pop(message.get("seq"), None)
            if future is not None and not future.done():
                future.set_result(message)
            return

        if protocol.is_event(message):
            await self._handle_event(message)

    async def _handle_event(self, event: dict) -> None:
        """Absorb a ``pos``, ``fault`` or ``boot`` event."""
        kind = event.get("ev")
        if kind == "pos":
            positions = event.get("pos")
            if isinstance(positions, list):
                self.positions = [int(v) for v in positions]
            moving = event.get("moving")
            if isinstance(moving, list):
                self.moving = [bool(v) for v in moving]
        elif kind == "fault":
            self.last_fault = dict(event)
            log.warning("Pico fault: %s %s", event.get("code"), event.get("msg"))
        elif kind == "boot":
            # The Pico restarted underneath us.
            self.boot_count += 1
            self.last_boot_at = time.time()
            self.needs_verification = bool(event.get("needs_verification", True))
            self.enabled = False
            positions = event.get("pos")
            if isinstance(positions, list):
                self.positions = [int(v) for v in positions]
            self.info.update(
                {
                    "fw": event.get("fw"),
                    "proto": event.get("proto"),
                    "boot_reason": event.get("reason"),
                }
            )
            log.warning(
                "Pico booted (%s); every hand is now unverified", event.get("reason")
            )
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(LinkError("Pico reset mid-command"))
            self._pending.clear()
            if self._reset_notice is not None:
                result = self._reset_notice()
                if asyncio.iscoroutine(result):
                    await result

        if self.on_event is not None:
            try:
                result = self.on_event(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("Pico event handler failed")

    async def _absorb_info(self, info: dict) -> None:
        """Record what ``hello``/``info``/``status`` told us."""
        for key in (
            "fw",
            "proto",
            "boot_reason",
            "uptime_ms",
            "channels",
            "max_speed",
            "accel",
            "speed_limit",
            "accel_limit",
            "idle_disable",
            "watchdog_s",
            "pos_interval_ms",
        ):
            if key in info:
                self.info[key] = info[key]
        if "enabled" in info:
            self.enabled = bool(info["enabled"])
        if "needs_verification" in info:
            self.needs_verification = bool(info["needs_verification"])
        if isinstance(info.get("pos"), list):
            self.positions = [int(v) for v in info["pos"]]
        if isinstance(info.get("moving"), list):
            self.moving = [bool(v) for v in info["moving"]]
        version = self.info.get("proto")
        if isinstance(version, int) and version != protocol.PROTOCOL_VERSION:
            log.warning(
                "Pico speaks protocol v%s, controller speaks v%s; upload matching firmware",
                version,
                protocol.PROTOCOL_VERSION,
            )

    # -- sending ---------------------------------------------------------

    def next_seq(self) -> int:
        """Allocate the next sequence number."""
        self._seq += 1
        return self._seq

    async def command(
        self, cmd: str, *, timeout_s: float | None = None, retries: int | None = None, **args: Any
    ) -> dict:
        """Send a command and wait for its reply.

        Args:
            cmd: One of :data:`protocol.COMMANDS`.
            timeout_s: Per-attempt timeout; defaults to the configured one.
            retries: Extra attempts after a timeout; defaults to the configured
                value.  Retrying is safe because every command is idempotent in
                effect: ``move`` is absolute, ``enable`` is a level, ``stop`` is
                a state.  There is no "step once more" command for this reason.
            **args: Command arguments.

        Returns:
            The reply dict.

        Raises:
            LinkError: if the link is down or every attempt timed out.
            CommandFailed: if the Pico answered with an error.
        """
        timeout = self.command_timeout_s if timeout_s is None else timeout_s
        attempts = (self.max_retries if retries is None else retries) + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            if not self.connected or self.transport is None:
                raise LinkError("Pico is not connected")
            seq = self.next_seq()
            message = {"seq": seq, "cmd": cmd}
            message.update(args)
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[seq] = future
            try:
                async with self._write_lock:
                    await self.transport.write(protocol.encode(message))
                self.commands_sent += 1
                reply = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                self._pending.pop(seq, None)
                self.timeouts += 1
                last_error = LinkError(
                    f"no reply to {cmd} (seq {seq}) in {timeout:.1f} s"
                )
                log.warning("%s; attempt %d of %d", last_error, attempt + 1, attempts)
                continue
            except LinkError as exc:
                self._pending.pop(seq, None)
                last_error = exc
                continue
            except Exception as exc:
                self._pending.pop(seq, None)
                raise LinkError(f"writing to the Pico failed: {exc}") from exc

            if not reply.get("ok", False):
                raise CommandFailed(
                    reply.get("err", "unknown"), reply.get("msg", ""), reply
                )
            return reply

        raise last_error or LinkError(f"command {cmd} failed")

    # -- convenience wrappers --------------------------------------------

    async def hello(self) -> dict:
        """Ask the firmware to identify itself and absorb the answer."""
        info = await self.command("hello")
        await self._absorb_info(info)
        return info

    async def status(self) -> dict:
        """Fetch live motion state and absorb it."""
        reply = await self.command("status")
        await self._absorb_info(reply)
        return reply

    async def set_enable(self, on: bool) -> dict:
        """Enable or disable all six drivers."""
        reply = await self.command("enable", on=bool(on))
        self.enabled = bool(reply.get("enabled", on))
        return reply

    async def move(self, channel: int, target_steps: int) -> dict:
        """Move one hand to an absolute microstep position."""
        return await self.command("move", ch=int(channel), target=int(target_steps))

    async def jog(self, channel: int, delta_steps: int) -> dict:
        """Move one hand by a relative number of microsteps."""
        return await self.command("jog", ch=int(channel), delta=int(delta_steps))

    async def set_pos(self, channel: int, steps: int) -> dict:
        """Declare a hand's current position without moving it."""
        reply = await self.command("set_pos", ch=int(channel), pos=int(steps))
        if 0 <= channel < len(self.positions):
            self.positions[channel] = int(steps)
        return reply

    async def stop(self, channel: int | None = None) -> dict:
        """Decelerate one hand, or all of them.

        This is the motion command.  To shut the link down, call :meth:`close`.
        """
        if channel is None:
            return await self.command("stop")
        return await self.command("stop", ch=int(channel))

    async def estop(self) -> dict:
        """Stop every hand immediately; marks everything unverified."""
        reply = await self.command("estop", retries=0)
        self.needs_verification = True
        return reply

    async def set_params(
        self,
        *,
        max_speed: float | None = None,
        accel: float | None = None,
        channel: int | None = None,
        idle_disable: bool | None = None,
        watchdog_s: float | None = None,
        pos_interval_ms: int | None = None,
    ) -> dict:
        """Change motion parameters, globally or on one channel."""
        args: dict[str, Any] = {}
        if max_speed is not None:
            args["max_speed"] = float(max_speed)
        if accel is not None:
            args["accel"] = float(accel)
        if channel is not None:
            args["ch"] = int(channel)
        if idle_disable is not None:
            args["idle_disable"] = bool(idle_disable)
        if watchdog_s is not None:
            args["watchdog_s"] = float(watchdog_s)
        if pos_interval_ms is not None:
            args["pos_interval_ms"] = int(pos_interval_ms)
        reply = await self.command("set_params", **args)
        await self._absorb_info(reply)
        return reply

    # -- background ------------------------------------------------------

    async def _ping_loop(self) -> None:
        """Keep the firmware watchdog fed for as long as we are connected."""
        while self._running:
            await asyncio.sleep(self.ping_interval_s)
            if not self.connected:
                continue
            try:
                await self.command("ping", retries=0, timeout_s=self.command_timeout_s)
            except asyncio.CancelledError:
                raise
            except LinkError as exc:
                log.warning("ping failed: %s", exc)

    def describe(self) -> dict[str, Any]:
        """Everything the UI's system banner needs about the link."""
        transport = self.transport
        return {
            "connected": self.connected,
            "transport": transport.describe() if hasattr(transport, "describe") else None,
            "firmware": self.info.get("fw"),
            "protocol": self.info.get("proto"),
            "controller_protocol": protocol.PROTOCOL_VERSION,
            "boot_reason": self.info.get("boot_reason"),
            "uptime_ms": self.info.get("uptime_ms"),
            "enabled": self.enabled,
            "needs_verification": self.needs_verification,
            "positions": list(self.positions),
            "moving": list(self.moving),
            "commands_sent": self.commands_sent,
            "timeouts": self.timeouts,
            "connects": self.connect_count,
            "boots": self.boot_count,
            "last_fault": self.last_fault,
            "last_error": self.last_error,
            "last_reply_at": self.last_reply_at,
        }
