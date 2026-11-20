"""The Pi <-> Pico wire protocol: newline-delimited JSON.

Pure Python with no hardware imports, so the firmware (``main.py``), the fake
Pico (``tools/pico_sim.py``) and the controller (``clockd.picolink``) all encode
and decode with this one module and cannot drift apart.  ``docs/PROTOCOL.md`` is
generated from the same facts; if you change anything here, change that too.

Shape
-----
Every line is one JSON object, UTF-8, terminated by ``\\n``.  There are three
kinds:

*Command* (Pi to Pico).  ``seq`` is a strictly positive integer chosen by the
Pi; ``cmd`` names the command::

    {"seq": 7, "cmd": "move", "ch": 2, "target": 1600}

*Reply* (Pico to Pi).  Exactly one per command, echoing ``seq`` and ``cmd``::

    {"seq": 7, "cmd": "move", "ok": true, "pos": 420, "target": 1600}
    {"seq": 7, "cmd": "move", "ok": false, "err": "bad_channel",
     "msg": "channel 9 out of range"}

*Event* (Pico to Pi, unsolicited).  No ``seq``; carries ``ev``::

    {"ev": "pos", "t": 1534219, "pos": [0, 0, 0, 0, 0, 0],
     "moving": [false, false, false, false, false, false]}
    {"ev": "fault", "code": "watchdog", "msg": "no ping for 10.0 s"}
    {"ev": "boot", "fw": "0.1.0", "reason": "power-on",
     "needs_verification": true}

A reply is distinguished from an event by the presence of ``seq``; a reply
always has ``ok``.  Unknown keys are ignored so that a newer Pi can talk to an
older Pico.
"""

try:
    import json
except ImportError:  # pragma: no cover - MicroPython always has json
    import ujson as json  # type: ignore

#: Bumped when the wire format changes incompatibly.  The controller refuses to
#: drive a Pico whose major version differs from its own.
PROTOCOL_VERSION = 1

#: Firmware version reported by ``hello``/``info``.  Kept here so the simulator
#: and the real firmware report the same shape of value.
FIRMWARE_VERSION = "0.1.0"

#: Commands the Pico accepts.
COMMANDS = (
    "hello",
    "info",
    "status",
    "move",
    "jog",
    "set_pos",
    "enable",
    "stop",
    "estop",
    "set_params",
    "ping",
)

#: Events the Pico may send unprompted.
EVENTS = ("boot", "pos", "fault", "log")

# Error codes.  Short, stable, machine-readable; ``msg`` carries the detail.
ERR_BAD_JSON = "bad_json"
ERR_NOT_OBJECT = "not_object"
ERR_NO_SEQ = "no_seq"
ERR_BAD_SEQ = "bad_seq"
ERR_NO_CMD = "no_cmd"
ERR_UNKNOWN_CMD = "unknown_cmd"
ERR_BAD_CHANNEL = "bad_channel"
ERR_BAD_ARG = "bad_arg"
ERR_MISSING_ARG = "missing_arg"
ERR_BUSY = "busy"
ERR_NOT_ENABLED = "not_enabled"
ERR_INTERNAL = "internal"

#: Fault codes carried by a ``fault`` event.
FAULT_WATCHDOG = "watchdog"
FAULT_PROTOCOL = "protocol"
FAULT_INTERNAL = "internal"

#: Boot reasons reported by the ``boot`` event and by ``info``.
BOOT_POWER_ON = "power-on"
BOOT_RESET = "reset"
BOOT_WATCHDOG = "watchdog-reset"
BOOT_SOFT = "soft-reset"
BOOT_UNKNOWN = "unknown"


class ProtocolError(Exception):
    """A command that cannot be executed as written.

    Args:
        code: One of the ``ERR_*`` constants.
        msg: Human-readable detail, shown in the UI log.
        seq: Sequence number to echo, when it is known.
    """

    def __init__(self, code, msg="", seq=None):
        Exception.__init__(self, msg or code)
        self.code = code
        self.msg = msg or code
        self.seq = seq


# -- framing ------------------------------------------------------------------


def encode(obj):
    """Serialise one message to a single ``bytes`` line including the newline."""
    return (json.dumps(obj) + "\n").encode("utf-8")


def decode(line):
    """Parse one received line into a dict.

    Args:
        line: ``bytes`` or ``str``, with or without the trailing newline.

    Returns:
        The decoded object.

    Raises:
        ProtocolError: ``bad_json`` if it is not JSON, ``not_object`` if the
            JSON is valid but not an object.
    """
    if isinstance(line, (bytes, bytearray)):
        try:
            line = bytes(line).decode("utf-8")
        except (UnicodeError, ValueError):
            raise ProtocolError(ERR_BAD_JSON, "line is not valid UTF-8")
    line = line.strip()
    if not line:
        raise ProtocolError(ERR_BAD_JSON, "empty line")
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        raise ProtocolError(ERR_BAD_JSON, "not valid JSON")
    if not isinstance(obj, dict):
        raise ProtocolError(ERR_NOT_OBJECT, "top level is not an object")
    return obj


# -- argument helpers ---------------------------------------------------------


def _require(obj, key, seq):
    if key not in obj or obj[key] is None:
        raise ProtocolError(ERR_MISSING_ARG, "missing %r" % key, seq)
    return obj[key]


def _as_int(value, key, seq):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(ERR_BAD_ARG, "%r must be a number" % key, seq)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ProtocolError(ERR_BAD_ARG, "%r must be finite" % key, seq)
        if value != int(value):
            raise ProtocolError(ERR_BAD_ARG, "%r must be a whole number of microsteps" % key, seq)
    return int(value)


def _as_float(value, key, seq):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(ERR_BAD_ARG, "%r must be a number" % key, seq)
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ProtocolError(ERR_BAD_ARG, "%r must be finite" % key, seq)
    return value


def _as_bool(value, key, seq):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if value in ("on", "off", "true", "false"):
        return value in ("on", "true")
    raise ProtocolError(ERR_BAD_ARG, "%r must be a boolean" % key, seq)


def parse_command(obj, channels=6):
    """Validate a decoded command and normalise its arguments.

    Args:
        obj: The dict from :func:`decode`.
        channels: How many channels exist, for range-checking ``ch``.

    Returns:
        A new dict with ``seq``, ``cmd`` and the normalised arguments for that
        command.  Missing optional arguments are absent rather than ``None``.

    Raises:
        ProtocolError: with the matching ``ERR_*`` code.
    """
    if "seq" not in obj:
        raise ProtocolError(ERR_NO_SEQ, "command has no seq")
    seq = obj["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        raise ProtocolError(ERR_BAD_SEQ, "seq must be a positive integer")
    if "cmd" not in obj:
        raise ProtocolError(ERR_NO_CMD, "command has no cmd", seq)
    cmd = obj["cmd"]
    if not isinstance(cmd, str):
        raise ProtocolError(ERR_NO_CMD, "cmd must be a string", seq)
    if cmd not in COMMANDS:
        raise ProtocolError(ERR_UNKNOWN_CMD, "unknown command %r" % cmd, seq)

    out = {"seq": seq, "cmd": cmd}

    def channel(required=True):
        if "ch" not in obj or obj["ch"] is None:
            if required:
                raise ProtocolError(ERR_MISSING_ARG, "missing 'ch'", seq)
            return None
        ch = _as_int(obj["ch"], "ch", seq)
        if not 0 <= ch < channels:
            raise ProtocolError(ERR_BAD_CHANNEL, "channel %d out of range" % ch, seq)
        return ch

    if cmd == "move":
        out["ch"] = channel()
        out["target"] = _as_int(_require(obj, "target", seq), "target", seq)
    elif cmd == "jog":
        out["ch"] = channel()
        out["delta"] = _as_int(_require(obj, "delta", seq), "delta", seq)
    elif cmd == "set_pos":
        out["ch"] = channel()
        out["pos"] = _as_int(_require(obj, "pos", seq), "pos", seq)
    elif cmd == "enable":
        out["on"] = _as_bool(_require(obj, "on", seq), "on", seq)
    elif cmd == "stop":
        out["ch"] = channel(required=False)
    elif cmd == "set_params":
        out["ch"] = channel(required=False)
        if "max_speed" in obj and obj["max_speed"] is not None:
            speed = _as_float(obj["max_speed"], "max_speed", seq)
            if speed <= 0:
                raise ProtocolError(ERR_BAD_ARG, "max_speed must be positive", seq)
            out["max_speed"] = speed
        if "accel" in obj and obj["accel"] is not None:
            accel = _as_float(obj["accel"], "accel", seq)
            if accel <= 0:
                raise ProtocolError(ERR_BAD_ARG, "accel must be positive", seq)
            out["accel"] = accel
        if "idle_disable" in obj and obj["idle_disable"] is not None:
            out["idle_disable"] = _as_bool(obj["idle_disable"], "idle_disable", seq)
        if "watchdog_s" in obj and obj["watchdog_s"] is not None:
            out["watchdog_s"] = _as_float(obj["watchdog_s"], "watchdog_s", seq)
        if "pos_interval_ms" in obj and obj["pos_interval_ms"] is not None:
            out["pos_interval_ms"] = _as_int(obj["pos_interval_ms"], "pos_interval_ms", seq)
    # hello, info, status, estop, ping take no arguments.
    return out


# -- builders -----------------------------------------------------------------


def ok_reply(seq, cmd, **data):
    """Build a success reply for ``seq``/``cmd`` with extra fields."""
    reply = {"seq": seq, "cmd": cmd, "ok": True}
    reply.update(data)
    return reply


def err_reply(seq, cmd, code, msg=""):
    """Build a failure reply.  ``seq`` may be ``None`` when it was unparseable."""
    return {"seq": seq, "cmd": cmd, "ok": False, "err": code, "msg": msg}


def error_to_reply(error, cmd=None):
    """Turn a :class:`ProtocolError` into the reply to send back."""
    return err_reply(error.seq, cmd, error.code, error.msg)


def event(name, **data):
    """Build an unsolicited event message."""
    ev = {"ev": name}
    ev.update(data)
    return ev


def is_reply(obj):
    """True when a received message is a reply to a command."""
    return isinstance(obj, dict) and "seq" in obj and "ok" in obj


def is_event(obj):
    """True when a received message is an unsolicited event."""
    return isinstance(obj, dict) and "ev" in obj and "seq" not in obj
