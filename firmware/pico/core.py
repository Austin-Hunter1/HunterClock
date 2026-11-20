"""Command dispatch for the Pico: everything except the pins.

Like :mod:`motion` and :mod:`protocol` this module is pure Python with no
hardware imports, so the real firmware (``main.py``) and the fake Pico
(``tools/pico_sim.py``) share one implementation of what every command *means*.
The only difference between the two is which callbacks get handed in.

The core owns:

* the six-axis :class:`motion.MotionController`;
* the watchdog -- no ``ping`` for ``watchdog_s`` and every axis decelerates to a
  stop and a ``fault`` event goes out, while the stored positions are kept;
* throttled ``pos`` events;
* idle-disable of the shared ENABLE line, which is a config option because
  disabling the drivers removes the holding torque;
* position persistence, written only at rest and rate limited, so the flash
  survives;
* ``needs_verification``, set at boot and after an e-stop, because with no home
  sensors a position that was not confirmed by a person is a guess.
"""

import motion
import protocol


class NullStore:
    """Position store that forgets everything -- the default in tests."""

    def load(self):
        """Return the saved positions, or ``None`` when there are none."""
        return None

    def save(self, positions):
        """Persist ``positions`` (a list of ints)."""


class PicoCore:
    """The firmware's brain.

    Args:
        channels: Number of axes (6 on the clock).
        pulse: ``pulse(channel, direction)`` -- emit one STEP pulse.
        set_enable: ``set_enable(on)`` -- drive the shared active-low ENABLE.
        store: Object with ``load()``/``save(positions)`` for flash persistence.
        boot_reason: One of the ``protocol.BOOT_*`` constants.
        send: ``send(message_dict)`` -- optional; when given, replies and events
            are pushed through it as well as returned.
    """

    #: Shortest gap between two ``pos`` events, in ms.
    DEFAULT_POS_INTERVAL_MS = 200
    #: Watchdog timeout in seconds.  The controller pings every few seconds.
    DEFAULT_WATCHDOG_S = 10.0
    #: Never write flash more often than this, in ms.
    FLASH_MIN_INTERVAL_MS = 10_000
    #: How long an axis must be at rest before its position is written, in ms.
    FLASH_SETTLE_MS = 1_000

    def __init__(
        self,
        channels=6,
        pulse=None,
        set_enable=None,
        store=None,
        boot_reason=protocol.BOOT_UNKNOWN,
        send=None,
    ):
        self.channels = channels
        self.mc = motion.MotionController(channels, pulse=pulse, set_enable=set_enable)
        self.store = store or NullStore()
        self.boot_reason = boot_reason
        self._send = send

        self.watchdog_s = self.DEFAULT_WATCHDOG_S
        self.pos_interval_ms = self.DEFAULT_POS_INTERVAL_MS
        self.idle_disable = False
        self.needs_verification = True
        self.started_us = 0
        self.last_ping_us = None
        self.last_pos_us = None
        self.watchdog_tripped = False
        self.commands_handled = 0
        self.errors = 0
        self._last_flash_us = None
        self._rest_since_us = None
        self._flashed_positions = None
        self._last_reported = None

        restored = self.store.load()
        if restored:
            for index, value in enumerate(restored[:channels]):
                self.mc.axis(index).set_position(int(value))
            self._flashed_positions = list(self.mc.positions())

    # -- lifecycle -------------------------------------------------------

    def start(self, now_us=0):
        """Record the boot time and return the ``boot`` event to send."""
        self.started_us = now_us
        self.last_ping_us = now_us
        self._rest_since_us = now_us
        return self._emit(
            protocol.event(
                "boot",
                fw=protocol.FIRMWARE_VERSION,
                proto=protocol.PROTOCOL_VERSION,
                reason=self.boot_reason,
                channels=self.channels,
                needs_verification=self.needs_verification,
                pos=self.mc.positions(),
            )
        )

    def uptime_ms(self, now_us):
        """Milliseconds since :meth:`start`."""
        return int((now_us - self.started_us) / 1000)

    def _emit(self, message):
        if self._send is not None:
            self._send(message)
        return message

    # -- incoming lines --------------------------------------------------

    def handle_line(self, line, now_us):
        """Decode, validate and execute one received line.

        Never raises: a malformed line produces an error reply, because the
        serial reader must not die on a stray byte.

        Args:
            line: Raw ``bytes`` or ``str`` from the serial port.
            now_us: Current monotonic microseconds.

        Returns:
            The reply message as a dict.
        """
        try:
            obj = protocol.decode(line)
        except protocol.ProtocolError as error:
            self.errors += 1
            return self._emit(protocol.error_to_reply(error))
        try:
            command = protocol.parse_command(obj, self.channels)
        except protocol.ProtocolError as error:
            self.errors += 1
            return self._emit(protocol.error_to_reply(error, obj.get("cmd")))
        try:
            reply = self.execute(command, now_us)
        except protocol.ProtocolError as error:
            self.errors += 1
            error.seq = command["seq"]
            return self._emit(protocol.error_to_reply(error, command["cmd"]))
        except Exception as exc:  # pragma: no cover - last-ditch safety net
            self.errors += 1
            return self._emit(
                protocol.err_reply(
                    command["seq"], command["cmd"], protocol.ERR_INTERNAL, str(exc)
                )
            )
        self.commands_handled += 1
        return self._emit(reply)

    def execute(self, command, now_us):
        """Run an already-validated command and build its reply."""
        cmd = command["cmd"]
        seq = command["seq"]

        if cmd in ("hello", "info"):
            return protocol.ok_reply(seq, cmd, **self.info(now_us))

        if cmd == "ping":
            self.last_ping_us = now_us
            if self.watchdog_tripped:
                self.watchdog_tripped = False
            return protocol.ok_reply(seq, cmd, t=self.uptime_ms(now_us))

        if cmd == "status":
            return protocol.ok_reply(seq, cmd, **self.status(now_us))

        if cmd == "move":
            axis = self.mc.axis(command["ch"])
            self._require_enabled()
            self.mc.move_to(command["ch"], command["target"], now_us)
            return protocol.ok_reply(
                seq, cmd, ch=command["ch"], pos=axis.position, target=axis.target
            )

        if cmd == "jog":
            axis = self.mc.axis(command["ch"])
            self._require_enabled()
            self.mc.jog(command["ch"], command["delta"], now_us)
            return protocol.ok_reply(
                seq, cmd, ch=command["ch"], pos=axis.position, target=axis.target
            )

        if cmd == "set_pos":
            axis = self.mc.axis(command["ch"])
            if axis.is_moving():
                raise protocol.ProtocolError(
                    protocol.ERR_BUSY, "channel %d is moving; stop it first" % command["ch"]
                )
            axis.set_position(command["pos"])
            self._rest_since_us = now_us
            return protocol.ok_reply(seq, cmd, ch=command["ch"], pos=axis.position)

        if cmd == "enable":
            self.mc.set_enable(command["on"])
            return protocol.ok_reply(seq, cmd, enabled=self.mc.enabled)

        if cmd == "stop":
            self.mc.stop(now_us, command["ch"])
            return protocol.ok_reply(seq, cmd, ch=command["ch"], pos=self.mc.positions())

        if cmd == "estop":
            self.mc.estop()
            self.needs_verification = True
            return protocol.ok_reply(
                seq, cmd, pos=self.mc.positions(), needs_verification=True
            )

        if cmd == "set_params":
            if "max_speed" in command or "accel" in command:
                self.mc.set_params(
                    command.get("max_speed"), command.get("accel"), command["ch"]
                )
            if "idle_disable" in command:
                self.idle_disable = command["idle_disable"]
            if "watchdog_s" in command:
                self.watchdog_s = max(0.0, command["watchdog_s"])
            if "pos_interval_ms" in command:
                self.pos_interval_ms = max(0, command["pos_interval_ms"])
            return protocol.ok_reply(seq, cmd, **self.params())

        raise protocol.ProtocolError(protocol.ERR_UNKNOWN_CMD, "unhandled command %r" % cmd)

    def _require_enabled(self):
        """A move with the drivers off would silently do nothing: say so."""
        if not self.mc.enabled:
            raise protocol.ProtocolError(
                protocol.ERR_NOT_ENABLED, "drivers are disabled; send enable first"
            )

    # -- reports ---------------------------------------------------------

    def params(self):
        """The tunables, as reported by ``set_params`` and ``info``."""
        axis = self.mc.axis(0)
        return {
            "max_speed": axis.max_speed,
            "accel": axis.accel,
            "idle_disable": self.idle_disable,
            "watchdog_s": self.watchdog_s,
            "pos_interval_ms": self.pos_interval_ms,
            "speed_limit": motion.SPEED_LIMIT,
            "accel_limit": motion.ACCEL_LIMIT,
        }

    def info(self, now_us):
        """Firmware identity and configuration."""
        out = {
            "fw": protocol.FIRMWARE_VERSION,
            "proto": protocol.PROTOCOL_VERSION,
            "boot_reason": self.boot_reason,
            "uptime_ms": self.uptime_ms(now_us),
            "channels": self.channels,
            "enabled": self.mc.enabled,
            "needs_verification": self.needs_verification,
            "pos": self.mc.positions(),
        }
        out.update(self.params())
        return out

    def status(self, now_us):
        """Live motion state."""
        return {
            "pos": self.mc.positions(),
            "target": [a.target for a in self.mc.axes],
            "moving": [a.is_moving() for a in self.mc.axes],
            "vel": [round(a.velocity, 2) for a in self.mc.axes],
            "enabled": self.mc.enabled,
            "needs_verification": self.needs_verification,
            "uptime_ms": self.uptime_ms(now_us),
            "steps": self.mc.step_count,
            "errors": self.errors,
            "watchdog_tripped": self.watchdog_tripped,
        }

    # -- periodic work ---------------------------------------------------

    def tick(self, now_us):
        """Do the time-based housekeeping.  Call this from the main loop.

        Emits STEP pulses that are due, then the watchdog, ``pos`` events, idle
        disable and flash persistence.

        Args:
            now_us: Current monotonic microseconds.

        Returns:
            A list of event messages to send (possibly empty).
        """
        events = []
        self.mc.service(now_us)

        moving = self.mc.any_moving()

        # Watchdog: the controller pings; losing it means the USB link or the Pi
        # has gone away, so bring the hands to a controlled stop.
        if self.watchdog_s > 0 and self.last_ping_us is not None:
            silence_s = (now_us - self.last_ping_us) / 1_000_000.0
            if silence_s > self.watchdog_s and not self.watchdog_tripped:
                self.watchdog_tripped = True
                self.mc.stop(now_us)
                events.append(
                    self._emit(
                        protocol.event(
                            "fault",
                            code=protocol.FAULT_WATCHDOG,
                            msg="no ping for %.1f s; decelerating" % silence_s,
                            pos=self.mc.positions(),
                        )
                    )
                )

        # Throttled position reports.
        if self._should_report(now_us, moving):
            self.last_pos_us = now_us
            self._last_reported = list(self.mc.positions())
            events.append(
                self._emit(
                    protocol.event(
                        "pos",
                        t=self.uptime_ms(now_us),
                        pos=self._last_reported,
                        moving=[a.is_moving() for a in self.mc.axes],
                    )
                )
            )

        # Rest tracking, for idle-disable and flash writes.
        if moving:
            self._rest_since_us = None
        elif self._rest_since_us is None:
            self._rest_since_us = now_us

        if (
            self.idle_disable
            and not moving
            and self.mc.enabled
            and self._at_rest_for_ms(now_us, self.FLASH_SETTLE_MS)
        ):
            self.mc.set_enable(False)

        self._maybe_flash(now_us, moving)
        return events

    def _should_report(self, now_us, moving):
        if self.pos_interval_ms <= 0:
            return False
        if self.last_pos_us is None:
            return True
        due = (now_us - self.last_pos_us) >= self.pos_interval_ms * 1000
        if not due:
            return False
        # While moving, report on the interval.  At rest, only when something
        # changed, so an idle clock is silent on the wire.
        if moving:
            return True
        return self._last_reported != self.mc.positions()

    def _at_rest_for_ms(self, now_us, ms):
        if self._rest_since_us is None:
            return False
        return (now_us - self._rest_since_us) >= ms * 1000

    def _maybe_flash(self, now_us, moving):
        """Write positions to flash, only at rest and rate limited."""
        if moving or not self._at_rest_for_ms(now_us, self.FLASH_SETTLE_MS):
            return
        positions = self.mc.positions()
        if positions == self._flashed_positions:
            return
        if (
            self._last_flash_us is not None
            and (now_us - self._last_flash_us) < self.FLASH_MIN_INTERVAL_MS * 1000
        ):
            return
        try:
            self.store.save(positions)
        except Exception:  # pragma: no cover - a full or worn flash must not crash
            return
        self._flashed_positions = list(positions)
        self._last_flash_us = now_us
