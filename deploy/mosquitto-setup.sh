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

# mosquitto and mosquitto_passwd live in /usr/sbin, which Debian leaves off a
# non-root user's PATH. Without this the script reports "mosquitto is not
# installed" on a Pi where the broker is running -- found on the real Pi.
PATH="${PATH}:/usr/sbin:/sbin"

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

# Mosquitto 2.x warns about a password or ACL file that is world readable or
# not owned by the user it drops to, and says plainly:
#   "Future versions will refuse to load this file."
# A warning today is a broker that will not start after some future apt
# upgrade, which on a clock in another state is a long way to drive. So set
# them the way Mosquitto asks for now.
secure_for_mosquitto() {  # secure_for_mosquitto <path>
  [ -e "$1" ] || return 0
  if getent passwd mosquitto >/dev/null 2>&1; then
    sudo_if_needed chown mosquitto:mosquitto "$1"
  fi
  sudo_if_needed chmod 0600 "$1"
}

# ---- env helpers (controller/tests/test_env_helpers.py runs these on their own)

# Refuse a .env that contains NUL bytes. This is what a lost write on an SD
# card looks like: the file keeps its new length but the data never reached
# the card, so it reads back as zeros. On the real Pi it left
#     \0\0\0...\0MQTT_DISPLAY_PASSWORD=<hex>
# as one line. env_get only matches a line that *starts* with the name, so it
# read nothing, the script issued a new password on every run, and each one
# was glued onto the end of the zeros -- the broker had a display password
# that .env could not give back. Do not guess at a repair: the zeros may have
# eaten another variable too, and a person should look before anything is
# rewritten.
env_check_intact() {
  [ -f "${ENV_FILE}" ] || return 0
  local nuls
  nuls="$(tr -cd '\000' < "${ENV_FILE}" | wc -c | tr -d ' ')"
  [ "${nuls}" -eq 0 ] && return 0
  die "${ENV_FILE} contains ${nuls} NUL bytes -- a write that never reached the
    SD card. Nothing has been changed. Recover it by hand:
        cp -p .env .env.bak && tr -d '\\000' < .env.bak > .env && chmod 600 .env
    then check every variable in it is still there (a zeroed line is lost, not
    hidden) and run this again. If a password is missing, this script will
    issue a new one; re-flash anything that used the old one."
}

# Read a variable out of .env without sourcing it: .env is a systemd
# EnvironmentFile, not a shell script, and sourcing one with an unquoted `#`
# or a bare `$` in a password does surprising things.
env_get() {
  [ -f "${ENV_FILE}" ] || return 0
  sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1
}

env_set() {  # env_set NAME VALUE
  touch "${ENV_FILE}"; chmod 600 "${ENV_FILE}"
  # Make sure the file ends with a newline before appending to it. Without
  # this, a .env whose last line has no newline silently gets the new
  # variable glued onto the end of the old one --
  #     NTFY_URL=MQTT_DISPLAY_PASSWORD=a1b2c3
  # -- which reads back as nothing, so the password is regenerated on every
  # run and never recoverable. (Suspected on the real Pi, but its .env turned
  # out to have NUL bytes instead -- see env_check_intact. This guard stays
  # because a hand-edited .env can still lose its last newline.) Compare the
  # byte with od, not $(tail -c 1): bash drops NULs from a command
  # substitution, so a file ending in one would look like it ended cleanly.
  if [ -s "${ENV_FILE}" ] \
     && [ "$(tail -c 1 "${ENV_FILE}" | od -An -tx1 | tr -d ' \n')" != "0a" ]; then
    printf '\n' >> "${ENV_FILE}"
  fi
  if grep -q "^$1=" "${ENV_FILE}"; then
    # The value is random hex, but use a delimiter it cannot contain anyway.
    python3 - "$1" "$2" "${ENV_FILE}" <<'PY'
import sys, pathlib
name, value, path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
lines = path.read_text().splitlines()
out = [f"{name}={value}" if line.startswith(f"{name}=") else line for line in lines]
path.write_bytes(("\n".join(out) + "\n").encode())
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "${ENV_FILE}"
  fi
  # The SD card has already lost one write to this file. A password that only
  # ever reached the page cache is the same as no password after a power cut.
  sync "${ENV_FILE}" 2>/dev/null || sync
}

# ---- end env helpers

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
env_check_intact

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

# What does this broker already say?
#
# A broker that is "already running" is usually already configured, and most of
# Mosquitto's security settings are single-valued: declaring password_file a
# second time is not an override, it is
#     Error: Duplicate password_file value in configuration
# and the broker refuses to start at all. So read what is there and add only
# what is missing, rather than assuming a stock install.
# Both of these return nothing, successfully, when the directive is absent.
# `grep` exits 1 on no match, and under `set -euo pipefail` that would take
# the whole script down -- on exactly the stock broker it is meant to handle.
declared() {  # declared <directive> -> prints the file it is declared in
  { sudo_if_needed grep -rlsE "^[[:space:]]*$1[[:space:]]" \
      /etc/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/ 2>/dev/null \
      || true; } | { grep -v "${CONF_DEST}" || true; } | head -1
}

declared_value() {  # declared_value <directive> -> prints its value
  { sudo_if_needed grep -rhsE "^[[:space:]]*$1[[:space:]]" \
      /etc/mosquitto/mosquitto.conf /etc/mosquitto/conf.d/ 2>/dev/null \
      || true; } | { grep -v "^[[:space:]]*#" || true; } | head -1 \
    | awk '{print $2}'
}

EXISTING_PASSWD_IN="$(declared password_file)"
EXISTING_ACL_IN="$(declared acl_file)"
EXISTING_LISTENER_IN="$(declared listener)"

if [ -n "${EXISTING_PASSWD_IN}" ]; then
  PASSWD_DEST="$(declared_value password_file)"
  ok "this broker already has a password file: ${PASSWD_DEST}"
  ok "  (declared in ${EXISTING_PASSWD_IN}; accounts will be added to it)"
fi
if [ -n "${EXISTING_LISTENER_IN}" ]; then
  ok "this broker already has a listener (${EXISTING_LISTENER_IN}); keeping it"
fi
if [ -n "${EXISTING_ACL_IN}" ]; then
  fail "this broker already has an acl_file, declared in:"
  fail "    ${EXISTING_ACL_IN}  ->  $(declared_value acl_file)"
  fail ""
  fail "Two acl_file lines is a duplicate-value error and the broker will not"
  fail "start. Merge the rules from deploy/mosquitto-weasley.acl into that file"
  fail "by hand, then re-run with the accounts only."
  exit 1
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
  # Read it straight back. A password written to .env but not readable from
  # it is worse than no password: the broker gets one the nodes can never be
  # told, and every run quietly issues another.
  if [ "$(env_get MQTT_DISPLAY_PASSWORD)" != "${DISPLAY_PASSWORD}" ]; then
    die "wrote MQTT_DISPLAY_PASSWORD to ${ENV_FILE} but could not read it back.
    Check that file by hand before going further."
  fi
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
CONF_TMP="$(mktemp)"
trap 'rm -f "${PASSWD_TMP}" "${CONF_TMP}"' EXIT
# Start from whatever is already there. The broker may carry accounts this
# clock knows nothing about, and the `clock` account's existing password is
# the one already in .env and already working -- rewriting the file from
# scratch would lock out anything else using this broker.
if sudo_if_needed test -f "${PASSWD_DEST}"; then
  sudo_if_needed cat "${PASSWD_DEST}" > "${PASSWD_TMP}"
  EXISTING_USERS="$(cut -d: -f1 < "${PASSWD_TMP}" | paste -sd, -)"
  ok "keeping the accounts already in ${PASSWD_DEST}: ${EXISTING_USERS:-none}"
else
  : > "${PASSWD_TMP}"
fi
mosquitto_passwd -b "${PASSWD_TMP}" clock "${CLOCK_PASSWORD}"
mosquitto_passwd -b "${PASSWD_TMP}" display "${DISPLAY_PASSWORD}"
[ "${WITH_PHONES}" -eq 1 ] && mosquitto_passwd -b "${PASSWD_TMP}" phone "${PHONE_PASSWORD}"

sudo_if_needed install -o root -g root -m 0600 "${PASSWD_TMP}" "${PASSWD_DEST}"
secure_for_mosquitto "${PASSWD_DEST}"
ok "wrote ${PASSWD_DEST}"

# ------------------------------------------------------------------ config

step "Configuration"
sudo_if_needed mkdir -p /etc/mosquitto/conf.d

install_if_changed() {  # install_if_changed <src> <dest> <description>
  # cmp through sudo: the destination may be 0600 and owned by mosquitto.
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
secure_for_mosquitto "${ACL_DEST}"

# Build the drop-in from the committed file, commenting out any directive the
# broker already declares elsewhere. Mosquitto treats a second password_file,
# acl_file or allow_anonymous as a fatal duplicate rather than an override --
#     Error: Duplicate password_file value in configuration
# -- and a second listener on the same port cannot bind. So on a broker that
# somebody has already set up, shipping the committed file verbatim stops it
# booting, which is exactly what happened the first time this was run on the
# real Pi.
cp "${REPO_ROOT}/deploy/mosquitto-weasley.conf" "${CONF_TMP}"

SKIPPED=""
for directive in listener allow_anonymous password_file max_connections \
                 max_queued_messages; do
  WHERE="$(declared "${directive}")"
  [ -n "${WHERE}" ] || continue
  "${REPO_ROOT}/deploy/_comment_out.py" "${CONF_TMP}" "${directive}" "${WHERE}"
  SKIPPED="${SKIPPED} ${directive}"
done

# max_connections is a *per-listener* option, and that is a sharper edge than
# it looks. Mosquitto attaches it to whatever listener is current, so meeting
# one in a file that declares no listener makes it create the DEFAULT
# listener -- a second bind on 1883, and
#     Error: Address already in use
# from a config with exactly one `listener` line in it. So when we are
# inheriting somebody else's listener, the listener's own settings are theirs
# to make, and we say nothing about them.
if [ -n "${EXISTING_LISTENER_IN}" ]; then
  for directive in max_connections max_queued_messages; do
    case " ${SKIPPED} " in
      *" ${directive} "*) continue ;;
    esac
    "${REPO_ROOT}/deploy/_comment_out.py" "${CONF_TMP}" "${directive}" \
      "${EXISTING_LISTENER_IN}'s listener"
    SKIPPED="${SKIPPED} ${directive}"
  done
fi

# acl_file is the one directive that must be added rather than inherited; an
# existing one is refused further up, because merging somebody else's rules
# is not something to do behind their back.
{
  printf '\n# ---- added by deploy/mosquitto-setup.sh ----\n'
  printf 'acl_file %s\n' "${ACL_DEST}"
} >> "${CONF_TMP}"

if [ -n "${SKIPPED}" ]; then
  ok "inheriting from this broker's own config:${SKIPPED}"
fi
install_if_changed "${CONF_TMP}" "${CONF_DEST}" "drop-in" && CHANGED=1

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
