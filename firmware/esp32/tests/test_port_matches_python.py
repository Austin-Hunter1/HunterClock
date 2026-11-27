"""The C++ motor core must behave like the Python one it was ported from.

firmware/esp32/src/motion_core.h and motor_core.h are a port of
firmware/pico/motion.py, core.py and protocol.py.  Nothing runs the C++ on a
real board in CI, so this replays one scripted session through both and
compares every STEP pulse and every message.

Needs a C++17 compiler (g++, clang++ or c++ on PATH) and the ArduinoJson
sources, which ``pio run -e display-motors`` downloads under
firmware/esp32/.pio/libdeps.  The test skips itself when either is missing.

The C++ is built with MC_REAL=double so the step times match exactly; the
firmware itself uses float, which can differ by a microsecond here and there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ESP32 = HERE.parent
REPO = ESP32.parents[1]
PICO = REPO / "firmware" / "pico"

sys.path.insert(0, str(PICO))
import core as pico_core  # noqa: E402
import protocol  # noqa: E402


def _compiler():
    for name in ("g++", "clang++", "c++"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


def _arduinojson():
    for src in sorted((ESP32 / ".pio" / "libdeps").glob("*/ArduinoJson/src")):
        return src
    return None


def _script():
    """(kind, now_us, payload) steps: a session that touches every command."""
    steps = []

    def line(now, obj):
        steps.append(("L", now, obj if isinstance(obj, str) else json.dumps(obj)))

    def run(start, stop, every=500):
        for now in range(start, stop, every):
            steps.append(("T", now, ""))

    seq = iter(range(1, 1000))
    cmd = lambda c, **kw: {"seq": next(seq), "cmd": c, **kw}  # noqa: E731

    line(1000, cmd("hello"))
    line(2000, "this is not json")
    line(3000, "[1, 2]")
    line(4000, {"cmd": "ping"})
    line(5000, {"seq": 0, "cmd": "ping"})
    line(6000, cmd("explode"))
    line(7000, cmd("move", ch=0, target=1600))  # not enabled
    line(8000, cmd("enable", on=True))
    line(9000, cmd("move", ch=9, target=1))  # bad channel
    line(10000, cmd("move", ch=0))  # missing target
    line(11000, cmd("move", ch=0, target=True))  # bool is not a number
    line(12000, cmd("move", ch=0, target=1.5))  # not a whole number
    line(13000, cmd("set_params", max_speed=-5))
    line(14000, cmd("set_params", ch=1, max_speed=1200, accel=3000))
    line(15000, cmd("move", ch=0, target=1600.0))  # whole float is fine
    line(16000, cmd("move", ch=1, target=-900))
    run(17000, 400000)
    line(400000, cmd("jog", ch=0, delta=-2000))  # reversal
    run(401000, 700000)
    line(700000, cmd("stop"))
    line(701000, cmd("status"))
    run(702000, 1200000)
    line(1200000, cmd("set_pos", ch=2, pos=0))
    line(1201000, cmd("move", ch=2, target=500))
    line(1202000, cmd("set_pos", ch=2, pos=5))  # busy
    run(1203000, 1500000)
    line(1500000, cmd("estop"))
    line(1501000, cmd("status"))
    line(1502000, cmd("set_params", watchdog_s=2.0, pos_interval_ms=100))
    line(1503000, cmd("move", ch=3, target=4000))
    run(1504000, 5000000)  # no pings: the watchdog trips and stops the hand
    line(5000000, cmd("ping"))
    line(5001000, cmd("enable", on=False))
    run(5002000, 5100000)
    return steps


def _run_python(steps):
    out = []
    core = pico_core.PicoCore(
        channels=6,
        pulse=lambda ch, d: out.append(("P", ch, d)),
        boot_reason=protocol.BOOT_POWER_ON,
        send=lambda m: out.append(("J", m)),
    )
    core.start(0)
    for kind, now, payload in steps:
        if kind == "L":
            core.handle_line(payload, now)
        else:
            core.tick(now)
    return out


def _run_cpp(tmp_path, steps):
    cxx = _compiler()
    ajson = _arduinojson()
    if cxx is None:
        pytest.skip("no C++ compiler on PATH")
    if ajson is None:
        pytest.skip("ArduinoJson sources not found: run `pio run -e display-motors` once")
    exe = tmp_path / ("host_main.exe" if sys.platform == "win32" else "host_main")
    subprocess.run(
        cxx + ["-std=c++17", "-O1", "-DMC_REAL=double", "-I", str(ESP32 / "src"),
               "-I", str(ajson), str(HERE / "host_main.cpp"), "-o", str(exe)],
        check=True,
    )
    script = "\n".join(
        ("L %d %s" % (n, p)) if k == "L" else ("T %d" % n) for k, n, p in steps
    )
    result = subprocess.run([str(exe)], input=script, capture_output=True, text=True, check=True)
    out = []
    for text in result.stdout.splitlines():
        if text.startswith("P "):
            _, ch, d = text.split()
            out.append(("P", int(ch), int(d)))
        elif text.startswith("J "):
            out.append(("J", json.loads(text[2:])))
    return out


#: Keys that legitimately differ: the C++ build names its board, and the Python
#: store reports None where the C++ one says "none".
IGNORED = {"board", "pos_source", "msg"}


def _same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        keys = (set(a) | set(b)) - IGNORED
        return all(_same(a.get(k), b.get(k)) for k in keys)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.02
    return a == b


def test_cpp_core_matches_python_core(tmp_path):
    steps = _script()
    py = _run_python(steps)
    cpp = _run_cpp(tmp_path, steps)

    py_pulses = [x for x in py if x[0] == "P"]
    cpp_pulses = [x for x in cpp if x[0] == "P"]
    assert len(py_pulses) > 1000, "the script should move the hands a good way"
    assert cpp_pulses == py_pulses

    py_msgs = [x[1] for x in py if x[0] == "J"]
    cpp_msgs = [x[1] for x in cpp if x[0] == "J"]
    assert len(cpp_msgs) == len(py_msgs)
    for index, (c, p) in enumerate(zip(cpp_msgs, py_msgs)):
        assert _same(c, p), "message %d differs:\n  c++    %s\n  python %s" % (index, c, p)
