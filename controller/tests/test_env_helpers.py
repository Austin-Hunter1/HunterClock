"""The .env helpers in ``deploy/mosquitto-setup.sh``, run on their own.

The script as a whole needs root and a real broker, so these tests cut out
just the block between ``# ---- env helpers`` and ``# ---- end env helpers``
and drive it with bash against a scratch file. That block decides whether the
six display nodes' password survives a re-run, which is worth pinning: on the
real Pi a lost SD-card write left NUL bytes in front of
``MQTT_DISPLAY_PASSWORD=``, the helpers read nothing, and every run issued a
new password that .env could not give back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "mosquitto-setup.sh"

needs_bash = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("od")),
    reason="needs bash and od (the Pi, CI, or Git Bash on Windows)",
)


def helpers() -> str:
    text = SCRIPT.read_text()
    start = text.index("# ---- env helpers")
    end = text.index("# ---- end env helpers")
    return text[start:end]


def run(env_file: Path, body: str) -> subprocess.CompletedProcess[str]:
    # env_set shells out to python3. Point that at the interpreter running the
    # tests: on Windows `python3` is often the Microsoft Store placeholder.
    # A function, not a PATH entry: a Windows path's drive colon splits PATH.
    prelude = (
        f'python3() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
        "set -euo pipefail\n"
        f"ENV_FILE='{env_file.as_posix()}'\n"
        "die() { printf 'DIE %s\\n' \"$*\" >&2; exit 1; }\n"
    )
    return subprocess.run(
        ["bash", "-c", prelude + helpers() + "\n" + body],
        capture_output=True, text=True, timeout=20, env=dict(os.environ),
    )


@needs_bash
class TestNulBytes:
    def test_a_file_with_nul_bytes_is_refused(self, tmp_path):
        env = tmp_path / ".env"
        env.write_bytes(b"MQTT_PASSWORD=abc\n" + b"\0" * 40 + b"MQTT_DISPLAY_PASSWORD=d00d\n")
        done = run(env, "env_check_intact")
        assert done.returncode != 0
        assert "40 NUL bytes" in done.stderr

    def test_refusing_changes_nothing(self, tmp_path):
        env = tmp_path / ".env"
        original = b"A=1\n" + b"\0" * 8 + b"B=2\n"
        env.write_bytes(original)
        run(env, "env_check_intact")
        assert env.read_bytes() == original

    def test_a_clean_file_passes(self, tmp_path):
        env = tmp_path / ".env"
        env.write_bytes(b"MQTT_PASSWORD=abc\nNTFY_URL=\n")
        assert run(env, "env_check_intact").returncode == 0

    def test_a_missing_file_passes(self, tmp_path):
        assert run(tmp_path / ".env", "env_check_intact").returncode == 0

    def test_the_line_the_pi_had_really_does_read_back_as_nothing(self, tmp_path):
        # Pins the failure the check exists for, so nobody "simplifies" it away.
        env = tmp_path / ".env"
        env.write_bytes(b"\0" * 40 + b"MQTT_DISPLAY_PASSWORD=d00d\n")
        done = run(env, "printf '[%s]' \"$(env_get MQTT_DISPLAY_PASSWORD)\"")
        assert done.stdout == "[]"


@needs_bash
class TestSetThenGet:
    def test_appends_to_a_file_with_no_trailing_newline(self, tmp_path):
        env = tmp_path / ".env"
        env.write_bytes(b"NTFY_URL=")
        done = run(env, "env_set MQTT_DISPLAY_PASSWORD d00d; env_get MQTT_DISPLAY_PASSWORD")
        assert done.stdout.strip() == "d00d"
        assert env.read_bytes() == b"NTFY_URL=\nMQTT_DISPLAY_PASSWORD=d00d\n"

    def test_fills_in_an_empty_placeholder_from_the_example(self, tmp_path):
        env = tmp_path / ".env"
        env.write_bytes(b"MQTT_DISPLAY_PASSWORD=\nHASS_URL=\n")
        done = run(env, "env_set MQTT_DISPLAY_PASSWORD d00d; env_get MQTT_DISPLAY_PASSWORD")
        assert done.stdout.strip() == "d00d"
        assert env.read_bytes() == b"MQTT_DISPLAY_PASSWORD=d00d\nHASS_URL=\n"

    def test_a_kept_value_is_not_rewritten(self, tmp_path):
        env = tmp_path / ".env"
        env.write_bytes(b"MQTT_DISPLAY_PASSWORD=d00d\n")
        assert run(env, "env_get MQTT_DISPLAY_PASSWORD").stdout.strip() == "d00d"
        assert env.read_bytes() == b"MQTT_DISPLAY_PASSWORD=d00d\n"
