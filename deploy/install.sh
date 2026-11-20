#!/usr/bin/env bash
#
# Set up the Weasley Clock controller on a Raspberry Pi.
#
#   ./deploy/install.sh
#
# Idempotent: safe to run again after a git pull, and it will tell you what it
# changed. It never writes a secret and never overwrites config/clock.yaml.
#
# What it does:
#   1. checks it is on a sane system and warns about anything odd
#   2. installs the apt packages the controller needs
#   3. creates .venv and installs the controller into it
#   4. creates config/clock.yaml and .env from the examples, if missing
#   5. makes sure the service user can read the Pico's serial port
#   6. installs the systemd unit, persistent journald and the Pi watchdog
#   7. starts the service and tells you where the UI is
#
# It does NOT configure Mosquitto (already running per the build plan), join
# Tailscale (see deploy/tailscale-setup.sh) or add WiFi (deploy/add-wifi.sh).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${CLOCK_USER:-$(id -un)}"
VENV="${REPO_ROOT}/.venv"
UNIT_NAME="clockd.service"

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step()  { printf '%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()    { printf '    %s✓%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn()  { printf '    %s!%s %s\n' "${YELLOW}" "${OFF}" "$*"; }
fail()  { printf '    %sx%s %s\n' "${RED}" "${OFF}" "$*" >&2; }
die()   { fail "$*"; exit 1; }

have()  { command -v "$1" >/dev/null 2>&1; }
sudo_if_needed() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# ---------------------------------------------------------------- 1. checks

step "Checking the system"
[ -f "${REPO_ROOT}/controller/pyproject.toml" ] || die "run this from the HunterClock checkout"
ok "repo at ${REPO_ROOT}"

if ! have python3; then die "python3 is not installed"; fi
PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "${PYTHON_VERSION}" in
  3.1[1-9]|3.[2-9][0-9]) ok "python ${PYTHON_VERSION}" ;;
  *) die "python ${PYTHON_VERSION} is too old; the controller needs 3.11 or newer" ;;
esac

if [ -r /proc/device-tree/model ]; then
  ok "board: $(tr -d '\0' < /proc/device-tree/model)"
else
  warn "not a Raspberry Pi; continuing anyway (sim mode works anywhere)"
fi

if ! have systemctl; then
  warn "systemd not found: the service will not be installed"
  NO_SYSTEMD=1
else
  NO_SYSTEMD=0
fi

# ------------------------------------------------------------- 2. packages

step "Installing packages"
PACKAGES=(python3-venv python3-pip git)
MISSING=()
for package in "${PACKAGES[@]}"; do
  dpkg -s "${package}" >/dev/null 2>&1 || MISSING+=("${package}")
done
if [ ${#MISSING[@]} -gt 0 ]; then
  if have apt-get; then
    sudo_if_needed apt-get update -qq
    sudo_if_needed apt-get install -y "${MISSING[@]}"
    ok "installed: ${MISSING[*]}"
  else
    warn "no apt-get; install these yourself: ${MISSING[*]}"
  fi
else
  ok "all present"
fi

if ! have mosquitto; then
  warn "mosquitto is not installed. The build plan says it is already running;"
  warn "if not: sudo apt-get install mosquitto mosquitto-clients"
else
  ok "mosquitto present"
fi

# ----------------------------------------------------------- 3. virtualenv

step "Setting up the virtualenv"
if [ ! -d "${VENV}" ]; then
  python3 -m venv "${VENV}"
  ok "created ${VENV}"
else
  ok "${VENV} already exists"
fi
"${VENV}/bin/pip" install --quiet --upgrade pip wheel
"${VENV}/bin/pip" install --quiet -e "${REPO_ROOT}/controller"
ok "controller installed (editable, so git pull is enough to update)"

if "${VENV}/bin/pip" install --quiet mpremote 2>/dev/null; then
  ok "mpremote installed (firmware upload from the System page)"
else
  warn "could not install mpremote; the UI's firmware upload will say so"
fi

# --------------------------------------------------------------- 4. config

step "Configuration"
mkdir -p "${REPO_ROOT}/var" "${REPO_ROOT}/var/log"

if [ ! -f "${REPO_ROOT}/config/clock.yaml" ]; then
  cp "${REPO_ROOT}/config/clock.example.yaml" "${REPO_ROOT}/config/clock.yaml"
  ok "created config/clock.yaml from the example"
  warn "edit it: home coordinates, who is on which hub level, and the UI password hash"
else
  ok "config/clock.yaml already exists (not touched)"
fi

if [ ! -f "${REPO_ROOT}/.env" ]; then
  cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
  chmod 600 "${REPO_ROOT}/.env"
  ok "created .env (mode 600)"
  warn "put MQTT_PASSWORD in it, and a CLOCK_SECRET_KEY:"
  warn "  python3 -c 'import secrets; print(secrets.token_hex(32))'"
else
  chmod 600 "${REPO_ROOT}/.env"
  ok ".env already exists (not touched, mode forced to 600)"
fi

if grep -q 'password_hash: ""' "${REPO_ROOT}/config/clock.yaml" 2>/dev/null; then
  warn "no UI password set yet. The controller will refuse to start without one."
  warn "  ${VENV}/bin/python -m clockd.hashpw"
  warn "then paste the result into config/clock.yaml under web.password_hash"
fi

# --------------------------------------------------------------- 5. serial

step "Serial port access"
if id -nG "${SERVICE_USER}" | tr ' ' '\n' | grep -qx dialout; then
  ok "${SERVICE_USER} is in the dialout group"
else
  sudo_if_needed usermod -a -G dialout "${SERVICE_USER}"
  ok "added ${SERVICE_USER} to dialout"
  warn "that takes effect at next login; the service picks it up on restart"
fi

# Read the real value out of the YAML rather than grepping for "port:", which
# would find mqtt.port (1883) first and tell you the Pico was missing.
read_config() {  # read_config <dotted.key> <default>
  "${VENV}/bin/python" - "$1" "$2" <<'PY' 2>/dev/null || echo "$2"
import sys, yaml, pathlib
key, default = sys.argv[1], sys.argv[2]
try:
    data = yaml.safe_load(pathlib.Path("config/clock.yaml").read_text()) or {}
    for part in key.split("."):
        data = data[part]
    print(data if data not in (None, "") else default)
except Exception:
    print(default)
PY
}

SERIAL_PORT="$(cd "${REPO_ROOT}" && read_config serial.port /dev/ttyACM0)"
if [ -e "${SERIAL_PORT}" ]; then
  ok "${SERIAL_PORT} is present"
else
  warn "${SERIAL_PORT} is not present. Is the Pico plugged in?"
  warn "The controller starts anyway and keeps retrying; the UI shows the link as down."
fi

# Keep the Pico at a stable path even if it enumerates as ttyACM1 after a replug.
UDEV_RULE=/etc/udev/rules.d/99-weasley-pico.rules
sudo_if_needed mkdir -p /etc/udev/rules.d
if [ ! -f "${UDEV_RULE}" ]; then
  sudo_if_needed tee "${UDEV_RULE}" >/dev/null <<'RULE'
# Raspberry Pi Pico (RP2040) running MicroPython, as the Weasley Clock's motor MCU.
# Gives it a stable name so a replug that moves it to ttyACM1 does not break the
# controller: point serial.port at /dev/weasley-pico in config/clock.yaml.
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0005", SYMLINK+="weasley-pico", MODE="0660", GROUP="dialout"
RULE
  sudo_if_needed udevadm control --reload-rules || true
  ok "installed udev rule: /dev/weasley-pico"
  warn "to use it, set serial.port: /dev/weasley-pico in config/clock.yaml"
else
  ok "udev rule already installed"
fi

# -------------------------------------------------------------- 6. systemd

if [ "${NO_SYSTEMD}" -eq 0 ]; then
  step "systemd"

  UNIT_TMP="$(mktemp)"
  sed -e "s|/home/hunterboys/HunterClock|${REPO_ROOT}|g" \
      -e "s|^User=.*|User=${SERVICE_USER}|" \
      -e "s|^Group=.*|Group=${SERVICE_USER}|" \
      "${REPO_ROOT}/deploy/clockd.service" > "${UNIT_TMP}"
  if ! sudo_if_needed cmp -s "${UNIT_TMP}" "/etc/systemd/system/${UNIT_NAME}" 2>/dev/null; then
    sudo_if_needed cp "${UNIT_TMP}" "/etc/systemd/system/${UNIT_NAME}"
    ok "installed /etc/systemd/system/${UNIT_NAME}"
    RELOAD=1
  else
    ok "unit already up to date"
    RELOAD=0
  fi
  rm -f "${UNIT_TMP}"

  sudo_if_needed mkdir -p /etc/systemd/journald.conf.d
  if ! sudo_if_needed cmp -s "${REPO_ROOT}/deploy/journald-persistent.conf" \
       /etc/systemd/journald.conf.d/clockd.conf 2>/dev/null; then
    sudo_if_needed cp "${REPO_ROOT}/deploy/journald-persistent.conf" \
      /etc/systemd/journald.conf.d/clockd.conf
    sudo_if_needed systemctl restart systemd-journald || true
    ok "journald now keeps logs across reboots (capped at 200M)"
  else
    ok "persistent journald already configured"
  fi

  if [ -e /dev/watchdog ]; then
    sudo_if_needed mkdir -p /etc/systemd/system.conf.d
    if ! sudo_if_needed cmp -s "${REPO_ROOT}/deploy/clockd-watchdog.conf" \
         /etc/systemd/system.conf.d/clockd-watchdog.conf 2>/dev/null; then
      sudo_if_needed cp "${REPO_ROOT}/deploy/clockd-watchdog.conf" \
        /etc/systemd/system.conf.d/clockd-watchdog.conf
      ok "hardware watchdog configured (takes effect after a reboot)"
    else
      ok "hardware watchdog already configured"
    fi
  else
    warn "/dev/watchdog not present; add 'dtparam=watchdog=on' to /boot/firmware/config.txt"
  fi

  [ "${RELOAD}" -eq 1 ] && sudo_if_needed systemctl daemon-reload
  sudo_if_needed systemctl enable "${UNIT_NAME}" >/dev/null 2>&1 || true
  ok "clockd enabled at boot"
fi

# ----------------------------------------------------------------- 7. start

step "Starting"
if [ "${NO_SYSTEMD}" -eq 0 ]; then
  if grep -q 'password_hash: ""' "${REPO_ROOT}/config/clock.yaml" 2>/dev/null; then
    warn "not starting: set the UI password first (see above), then:"
    warn "  sudo systemctl start clockd"
  else
    sudo_if_needed systemctl restart "${UNIT_NAME}"
    sleep 2
    if systemctl is-active --quiet "${UNIT_NAME}"; then
      ok "clockd is running"
    else
      fail "clockd did not start. The reason will be in:"
      fail "  journalctl -u clockd -n 50 --no-pager"
      exit 1
    fi
  fi
fi

PORT="$(cd "${REPO_ROOT}" && read_config web.port 8080)"
echo
step "Done"
echo "    UI:        http://$(hostname):${PORT}/   (over Tailscale: http://$(hostname):${PORT}/)"
echo "    Logs:      journalctl -u clockd -f"
echo "    Restart:   sudo systemctl restart clockd"
echo "    Try it:    ${VENV}/bin/python -m clockd.cli --sim --sim-fixtures"
echo
echo "    Next: deploy/tailscale-setup.sh, then docs/RUNBOOK.md."
