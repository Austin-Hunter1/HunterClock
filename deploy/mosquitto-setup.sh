#!/usr/bin/env bash
#
# Configure Mosquitto for the Weasley Clock.
#
#   ./deploy/mosquitto-setup.sh                 the controller and the display nodes
#   ./deploy/mosquitto-setup.sh --with-phones   also a `phone` user, for OwnTracks
#   ./deploy/mosquitto-setup.sh --show          print the usernames and exit
#
# deploy/install.sh deliberately does not do this. A broker is shared
# infrastructure that may already have other things talking to it, so taking
# it over is a separate, deliberate step.
#
# WHAT IT IS FOR
#
# A freshly installed Mosquitto 2.x looks like it is running and is reachable
# by nothing: with no listener configured it binds loopback only and refuses
# anonymous clients. The controller is on localhost so it works; the six ESP32
# display nodes are on the house WiFi and cannot connect at all. This writes a
# listener, two accounts and an ACL that keeps them apart.
#
# It is idempotent. Passwords already in .env are kept, not regenerated, so
# re-running after a git pull does not orphan six display nodes.
#
# It finishes by actually publishing and subscribing as both accounts, and by
# checking that the display account is REFUSED where it should be. A broker
# that starts is not the same as a broker that works.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
CONF_DEST=/etc/mosquitto/conf.d/weasley.conf
ACL_DEST=/etc/mosquitto/weasley.acl
PASSWD_DEST=/etc/mosquitto/weasley.passwd

WITH_PHONES=0
SHOW_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --with-phones) WITH_PHONES=1 ;;
    --show) SHOW_ONLY=1 ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: ${arg}" >&2; exit 2 ;;
  esac
done

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step()  { printf '%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()    { printf '    %s✓%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn()  { printf '    %s!%s %s\n' "${YELLOW}" "${OFF}" "$*"; }
die()   { printf '    %sx%s %s\n' "${RED}" "${OFF}" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }
sudo_if_needed() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# Read a variable out of .env without sourcing it: .env is a systemd
# EnvironmentFile, not a shell script, and sourcing one with an unquoted `#`
# or a bare `$` in a password does surprising things.
env_get() {
  [ -f "${ENV_FILE}" ] || return 0
  sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1
}

env_set() {  # env_set NAME VALUE
  touch "${ENV_FILE}"; chmod 600 "${ENV_FILE}"
  if grep -q "^$1=" "${ENV_FILE}"; then
    # The value is random hex, but use a delimiter it cannot contain anyway.
    python3 - "$1" "$2" "${ENV_FILE}" <<'PY'
import sys, pathlib
name, value, path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
lines = path.read_text().splitlines()
out = [f"{name}={value}" if line.startswith(f"{name}=") else line for line in lines]
path.write_text("\n".join(out) + "\n")
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "${ENV_FILE}"
  fi
}

if [ "${SHOW_ONLY}" -eq 1 ]; then
  echo "clock    (the controller)   password in .env as MQTT_PASSWORD"
  echo "display  (the ESP32 nodes)  password in .env as MQTT_DISPLAY_PASSWORD"
  echo "phone    (OwnTracks)        password in .env as MQTT_PHONE_PASSWORD, if created"
  echo
  echo "Current ACL: ${ACL_DEST}"
  exit 0
fi

# ------------------------------------------------------------------ checks

step "Checking Mosquitto"
have mosquitto || die "mosquitto is not installed: sudo apt-get install -y mosquitto mosquitto-clients"
have mosquitto_passwd || die "mosquitto_passwd is missing: sudo apt-get install -y mosquitto"
ok "$(mosquitto -h 2>&1 | head -1)"

if ! have mosquitto_pub || ! have mosquitto_sub; then
  warn "mosquitto-clients is not installed, so the round-trip check at the end"
  warn "will be skipped: sudo apt-get install -y mosquitto-clients"
fi

# per_listener_settings changes where password_file and allow_anonymous are
# allowed to live. If something has turned it on, this drop-in is wrong and
# would be rejected, so say so rather than writing a broker that will not boot.
if sudo_if_needed grep -rqsE '^\s*per_listener_settings\s+true' \
     /etc/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/ 2>/dev/null; then
  die "per_listener_settings is true on this broker; this drop-in assumes the
    default (false) and would be rejected. Move the security settings from
    deploy/mosquitto-weasley.conf under the listener by hand."
fi

# -------------------------------------------------------------- passwords

step "Accounts"
CLOCK_PASSWORD="$(env_get MQTT_PASSWORD)"
# The .env.example ships a placeholder; treat it as unset rather than using it.
if [ -z "${CLOCK_PASSWORD}" ] || [ "${CLOCK_PASSWORD}" = "change-me" ]; then
  CLOCK_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  env_set MQTT_PASSWORD "${CLOCK_PASSWORD}"
  ok "generated a password for 'clock' and saved it to .env"
else
  ok "'clock' password already in .env (kept)"
fi

DISPLAY_PASSWORD="$(env_get MQTT_DISPLAY_PASSWORD)"
if [ -z "${DISPLAY_PASSWORD}" ]; then
  # The nodes get this typed into config.json by hand, so avoid characters
  # that are awkward in JSON or easy to mistype off a screen.
  DISPLAY_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
  env_set MQTT_DISPLAY_PASSWORD "${DISPLAY_PASSWORD}"
  ok "generated a password for 'display' and saved it to .env"
else
  ok "'display' password already in .env (kept)"
fi

if [ "${WITH_PHONES}" -eq 1 ]; then
  PHONE_PASSWORD="$(env_get MQTT_PHONE_PASSWORD)"
  if [ -z "${PHONE_PASSWORD}" ]; then
    PHONE_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
    env_set MQTT_PHONE_PASSWORD "${PHONE_PASSWORD}"
    ok "generated a password for 'phone' and saved it to .env"
  else
    ok "'phone' password already in .env (kept)"
  fi
fi

# Build the password file in a temp copy, then move it into place, so a failure
# half way through does not leave the broker with a file it cannot read.
PASSWD_TMP="$(mktemp)"
trap 'rm -f "${PASSWD_TMP}"' EXIT
: > "${PASSWD_TMP}"
mosquitto_passwd -b "${PASSWD_TMP}" clock "${CLOCK_PASSWORD}"
mosquitto_passwd -b "${PASSWD_TMP}" display "${DISPLAY_PASSWORD}"
[ "${WITH_PHONES}" -eq 1 ] && mosquitto_passwd -b "${PASSWD_TMP}" phone "${PHONE_PASSWORD}"

sudo_if_needed install -o root -g root -m 0600 "${PASSWD_TMP}" "${PASSWD_DEST}"
# Mosquitto drops to the `mosquitto` user and must still be able to read it.
if getent passwd mosquitto >/dev/null 2>&1; then
  sudo_if_needed chown root:mosquitto "${PASSWD_DEST}"
  sudo_if_needed chmod 0640 "${PASSWD_DEST}"
fi
ok "wrote ${PASSWD_DEST}"

# ------------------------------------------------------------------ config

step "Configuration"
sudo_if_needed mkdir -p /etc/mosquitto/conf.d

install_if_changed() {  # install_if_changed <src> <dest> <description>
  if sudo_if_needed cmp -s "$1" "$2" 2>/dev/null; then
    ok "$3 already up to date"
    return 1
  fi
  sudo_if_needed install -o root -g root -m 0644 "$1" "$2"
  ok "installed $2"
  return 0
}

CHANGED=0
install_if_changed "${REPO_ROOT}/deploy/mosquitto-weasley.acl" "${ACL_DEST}" "ACL" && CHANGED=1
install_if_changed "${REPO_ROOT}/deploy/mosquitto-weasley.conf" "${CONF_DEST}" "listener" && CHANGED=1

# ----------------------------------------------------------------- restart

step "Restarting Mosquitto"
rollback() {
  # Leaving the broker down is worse than not having configured it. The
  # accounts and the ACL are harmless on their own; it is the listener that
  # can clash with one the distribution or a previous setup already defined,
  # so that is what goes back.
  warn "putting the previous configuration back"
  sudo_if_needed rm -f "${CONF_DEST}"
  if sudo_if_needed systemctl restart mosquitto 2>/dev/null; then
    warn "mosquitto is running again, without this clock's listener"
  else
    fail "mosquitto is still not running. It was already failing before this"
    fail "script touched it: journalctl -u mosquitto -n 40 --no-pager"
  fi
}

if ! sudo_if_needed systemctl restart mosquitto; then
  fail "mosquitto failed to start with this configuration. It said:"
  echo
  sudo_if_needed journalctl -u mosquitto -n 25 --no-pager 2>/dev/null \
    | sed 's/^/      /' || true
  echo
  # Much the commonest cause, and invisible unless you go looking: another
  # listener on the same port, from the distribution's own config or from a
  # previous setup of this broker.
  OTHER="$(sudo_if_needed grep -rlsE '^\s*listener' /etc/mosquitto/mosquitto.conf \
             /etc/mosquitto/conf.d/ 2>/dev/null | grep -v weasley || true)"
  if [ -n "${OTHER}" ]; then
    fail "a listener is already defined in:"
    printf '        %s\n' ${OTHER} >&2
    fail "Two listeners on port 1883 cannot both bind. Either remove the other"
    fail "one, or drop the 'listener 1883' line from deploy/mosquitto-weasley.conf"
    fail "and keep only the accounts and the ACL."
  fi
  rollback
  exit 1
fi
sleep 1
if ! systemctl is-active --quiet mosquitto; then
  fail "mosquitto started and then stopped. It said:"
  sudo_if_needed journalctl -u mosquitto -n 25 --no-pager 2>/dev/null \
    | sed 's/^/      /' || true
  rollback
  exit 1
fi
ok "running"

# ------------------------------------------------------------ verification

step "Checking it actually works"
if ! have mosquitto_pub || ! have mosquitto_sub; then
  warn "skipped: mosquitto-clients is not installed"
else
  TOPIC="clock/display/__setup_check"

  # 1. The controller's account can write the clock tree.
  if mosquitto_pub -h localhost -u clock -P "${CLOCK_PASSWORD}" \
       -t "${TOPIC}" -m '{"check":true}' -r 2>/dev/null; then
    ok "'clock' can publish to clock/display/..."
  else
    die "'clock' could not publish. Check journalctl -u mosquitto."
  fi

  # 2. A display node's account can read it. Retained, so it arrives at once.
  GOT="$(mosquitto_sub -h localhost -u display -P "${DISPLAY_PASSWORD}" \
          -t "${TOPIC}" -C 1 -W 5 2>/dev/null || true)"
  if [ "${GOT}" = '{"check":true}' ]; then
    ok "'display' can read clock/display/... "
  else
    die "'display' could not read its own topic (got: ${GOT:-nothing}).
    The ACL at ${ACL_DEST} is not doing what it should."
  fi

  # 3. The part that matters: a display node must NOT be able to publish a
  #    position. This is the whole reason the roles are split, so it is
  #    checked rather than assumed.
  #
  #    Mosquitto does not fail the publish command on an ACL denial -- it
  #    accepts the packet and silently drops it -- so prove it by subscribing
  #    as the controller and showing nothing arrives.
  BAD_TOPIC="clock/location/__setup_check"
  mosquitto_pub -h localhost -u display -P "${DISPLAY_PASSWORD}" \
    -t "${BAD_TOPIC}" -m '{"lat":0,"lon":0}' -r 2>/dev/null || true
  LEAKED="$(mosquitto_sub -h localhost -u clock -P "${CLOCK_PASSWORD}" \
             -t "${BAD_TOPIC}" -C 1 -W 2 2>/dev/null || true)"
  if [ -z "${LEAKED}" ]; then
    ok "'display' is refused on clock/location/... (cannot move the hands)"
  else
    die "a display node CAN publish a position. The ACL is not being applied:
    check that acl_file is set in ${CONF_DEST} and that mosquitto can read
    ${ACL_DEST}."
  fi

  # Tidy up the retained check messages so they do not sit in the broker.
  mosquitto_pub -h localhost -u clock -P "${CLOCK_PASSWORD}" \
    -t "${TOPIC}" -r -n 2>/dev/null || true
  ok "cleaned up the check topics"
fi

# ---------------------------------------------------------------- what next

echo
step "Done"
echo "    Accounts:"
echo "      clock    the controller          .env MQTT_PASSWORD"
echo "      display  the six ESP32 nodes     .env MQTT_DISPLAY_PASSWORD"
[ "${WITH_PHONES}" -eq 1 ] && \
echo "      phone    OwnTracks               .env MQTT_PHONE_PASSWORD"
echo
echo "    The controller picks up its new password on a restart:"
echo "      sudo systemctl restart clockd"
echo
echo "    For each display node, in firmware/esp32/data/config.json:"
echo "      \"mqtt_user\": \"display\","
echo "      \"mqtt_password\": \"$(env_get MQTT_DISPLAY_PASSWORD)\""
echo
warn "The example config.json still says \"mqtt_user\": \"clock\". Change it to"
warn "\"display\" on each node, or a board in the cabinet carries the password"
warn "that can move every hand on the clock."
echo
echo "    Watch it: mosquitto_sub -h localhost -u clock -P \"\$MQTT_PASSWORD\" -t 'clock/#' -v"
