"""Wire protocol tests: framing, validation and the reply/event shapes."""

from __future__ import annotations

import json

import protocol as p
import pytest

# -- framing -----------------------------------------------------------------


def test_encode_is_one_newline_terminated_utf8_line():
    raw = p.encode({"seq": 1, "cmd": "ping"})
    assert isinstance(raw, bytes)
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert json.loads(raw.decode("utf-8")) == {"seq": 1, "cmd": "ping"}


def test_round_trip():
    message = {"seq": 42, "cmd": "move", "ch": 3, "target": -12345}
    assert p.decode(p.encode(message)) == message


@pytest.mark.parametrize("line", [b'{"a": 1}\n', '{"a": 1}', '  {"a": 1}  \n', b'{"a": 1}\r\n'])
def test_decode_tolerates_whitespace_and_both_types(line):
    assert p.decode(line) == {"a": 1}


@pytest.mark.parametrize("line", [b"", b"\n", "   "])
def test_decode_rejects_empty_lines(line):
    with pytest.raises(p.ProtocolError) as excinfo:
        p.decode(line)
    assert excinfo.value.code == p.ERR_BAD_JSON


@pytest.mark.parametrize("line", [b"not json", b"{broken", b'{"a": }'])
def test_decode_rejects_garbage(line):
    with pytest.raises(p.ProtocolError) as excinfo:
        p.decode(line)
    assert excinfo.value.code == p.ERR_BAD_JSON


def test_decode_rejects_invalid_utf8():
    with pytest.raises(p.ProtocolError) as excinfo:
        p.decode(b'{"a": "\xff\xfe"}')
    assert excinfo.value.code == p.ERR_BAD_JSON


@pytest.mark.parametrize("line", [b"[1, 2]", b'"hello"', b"42", b"null"])
def test_decode_rejects_non_objects(line):
    with pytest.raises(p.ProtocolError) as excinfo:
        p.decode(line)
    assert excinfo.value.code == p.ERR_NOT_OBJECT


def test_encoded_lines_never_contain_an_embedded_newline():
    """A name with a newline in it must not split into two frames."""
    raw = p.encode({"ev": "log", "msg": "two\nlines"})
    assert raw.count(b"\n") == 1


# -- command validation ------------------------------------------------------


@pytest.mark.parametrize("cmd", p.COMMANDS)
def test_every_documented_command_has_a_parser(cmd):
    """Build a minimal valid command for each verb and parse it."""
    args = {
        "move": {"ch": 0, "target": 100},
        "jog": {"ch": 1, "delta": -16},
        "set_pos": {"ch": 2, "pos": 0},
        "enable": {"on": True},
    }.get(cmd, {})
    parsed = p.parse_command({"seq": 1, "cmd": cmd, **args})
    assert parsed["seq"] == 1
    assert parsed["cmd"] == cmd


def test_move_is_normalised():
    parsed = p.parse_command({"seq": 9, "cmd": "move", "ch": 5, "target": 3200.0})
    assert parsed == {"seq": 9, "cmd": "move", "ch": 5, "target": 3200}


def test_unknown_keys_are_ignored_for_forward_compatibility():
    parsed = p.parse_command({"seq": 1, "cmd": "ping", "future_field": "whatever"})
    assert parsed == {"seq": 1, "cmd": "ping"}


@pytest.mark.parametrize(
    ("obj", "code"),
    [
        ({"cmd": "ping"}, p.ERR_NO_SEQ),
        ({"seq": 0, "cmd": "ping"}, p.ERR_BAD_SEQ),
        ({"seq": -1, "cmd": "ping"}, p.ERR_BAD_SEQ),
        ({"seq": 1.5, "cmd": "ping"}, p.ERR_BAD_SEQ),
        ({"seq": "1", "cmd": "ping"}, p.ERR_BAD_SEQ),
        ({"seq": True, "cmd": "ping"}, p.ERR_BAD_SEQ),
        ({"seq": 1}, p.ERR_NO_CMD),
        ({"seq": 1, "cmd": 7}, p.ERR_NO_CMD),
        ({"seq": 1, "cmd": "fly"}, p.ERR_UNKNOWN_CMD),
        ({"seq": 1, "cmd": "move", "ch": 6, "target": 0}, p.ERR_BAD_CHANNEL),
        ({"seq": 1, "cmd": "move", "ch": -1, "target": 0}, p.ERR_BAD_CHANNEL),
        ({"seq": 1, "cmd": "move", "target": 0}, p.ERR_MISSING_ARG),
        ({"seq": 1, "cmd": "move", "ch": 0}, p.ERR_MISSING_ARG),
        ({"seq": 1, "cmd": "move", "ch": 0, "target": "far"}, p.ERR_BAD_ARG),
        ({"seq": 1, "cmd": "move", "ch": 0, "target": 1.5}, p.ERR_BAD_ARG),
        ({"seq": 1, "cmd": "move", "ch": 0, "target": True}, p.ERR_BAD_ARG),
        ({"seq": 1, "cmd": "jog", "ch": 0}, p.ERR_MISSING_ARG),
        ({"seq": 1, "cmd": "set_pos", "ch": 0}, p.ERR_MISSING_ARG),
        ({"seq": 1, "cmd": "enable"}, p.ERR_MISSING_ARG),
        ({"seq": 1, "cmd": "enable", "on": "maybe"}, p.ERR_BAD_ARG),
        ({"seq": 1, "cmd": "set_params", "max_speed": 0}, p.ERR_BAD_ARG),
        ({"seq": 1, "cmd": "set_params", "accel": -1}, p.ERR_BAD_ARG),
        ({"seq": 1, "cmd": "set_params", "max_speed": "fast"}, p.ERR_BAD_ARG),
    ],
)
def test_invalid_commands_report_the_right_code(obj, code):
    with pytest.raises(p.ProtocolError) as excinfo:
        p.parse_command(obj)
    assert excinfo.value.code == code


def test_errors_echo_the_sequence_number_when_it_is_known():
    with pytest.raises(p.ProtocolError) as excinfo:
        p.parse_command({"seq": 77, "cmd": "move", "ch": 99, "target": 0})
    assert excinfo.value.seq == 77
    reply = p.error_to_reply(excinfo.value, "move")
    assert reply["seq"] == 77
    assert reply["ok"] is False
    assert reply["err"] == p.ERR_BAD_CHANNEL


def test_nan_and_infinity_are_rejected():
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(p.ProtocolError) as excinfo:
            p.parse_command({"seq": 1, "cmd": "move", "ch": 0, "target": value})
        assert excinfo.value.code == p.ERR_BAD_ARG


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (1, True), (0, False), ("on", True), ("off", False)],
)
def test_enable_accepts_the_usual_spellings_of_a_boolean(value, expected):
    parsed = p.parse_command({"seq": 1, "cmd": "enable", "on": value})
    assert parsed["on"] is expected


def test_stop_channel_is_optional_meaning_all():
    assert p.parse_command({"seq": 1, "cmd": "stop"})["ch"] is None
    assert p.parse_command({"seq": 1, "cmd": "stop", "ch": 4})["ch"] == 4


def test_set_params_passes_only_the_fields_given():
    parsed = p.parse_command({"seq": 1, "cmd": "set_params", "accel": 900})
    assert parsed == {"seq": 1, "cmd": "set_params", "ch": None, "accel": 900.0}


def test_set_params_full_set():
    parsed = p.parse_command(
        {
            "seq": 1,
            "cmd": "set_params",
            "ch": 2,
            "max_speed": 600,
            "accel": 1200,
            "idle_disable": True,
            "watchdog_s": 15,
            "pos_interval_ms": 250,
        }
    )
    assert parsed["ch"] == 2
    assert parsed["max_speed"] == 600.0
    assert parsed["accel"] == 1200.0
    assert parsed["idle_disable"] is True
    assert parsed["watchdog_s"] == 15.0
    assert parsed["pos_interval_ms"] == 250


def test_channel_count_is_configurable():
    with pytest.raises(p.ProtocolError):
        p.parse_command({"seq": 1, "cmd": "move", "ch": 3, "target": 0}, channels=2)
    assert p.parse_command({"seq": 1, "cmd": "move", "ch": 7, "target": 0}, channels=8)["ch"] == 7


# -- replies and events ------------------------------------------------------


def test_ok_reply_shape():
    reply = p.ok_reply(5, "move", pos=100, target=200)
    assert reply == {"seq": 5, "cmd": "move", "ok": True, "pos": 100, "target": 200}
    assert p.is_reply(reply)
    assert not p.is_event(reply)


def test_err_reply_shape():
    reply = p.err_reply(5, "move", p.ERR_BUSY, "axis is moving")
    assert reply["ok"] is False
    assert reply["err"] == p.ERR_BUSY
    assert reply["msg"] == "axis is moving"
    assert p.is_reply(reply)


def test_event_shape():
    ev = p.event("pos", t=1234, pos=[0] * 6, moving=[False] * 6)
    assert ev["ev"] == "pos"
    assert p.is_event(ev)
    assert not p.is_reply(ev)


def test_boot_event_requests_verification():
    ev = p.event(
        "boot", fw=p.FIRMWARE_VERSION, reason=p.BOOT_POWER_ON, needs_verification=True
    )
    assert p.is_event(ev)
    assert ev["needs_verification"] is True
    assert ev["reason"] in (
        p.BOOT_POWER_ON,
        p.BOOT_RESET,
        p.BOOT_WATCHDOG,
        p.BOOT_SOFT,
        p.BOOT_UNKNOWN,
    )


def test_a_reply_and_an_event_are_never_confused():
    assert not p.is_event({"seq": 1, "ok": True, "ev": "pos"})
    assert not p.is_reply({"ev": "pos"})
    assert not p.is_reply({"seq": 1})
    assert not p.is_event("string")
    assert not p.is_reply(None)


def test_protocol_version_is_an_integer():
    assert isinstance(p.PROTOCOL_VERSION, int)
    assert p.PROTOCOL_VERSION >= 1
