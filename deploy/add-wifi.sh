#!/usr/bin/env bash
#
# Save a WiFi network on the Pi, using NetworkManager.
#
#   ./deploy/add-wifi.sh SSID PASSWORD [PRIORITY]
#
# Add every network the Pi will ever see BEFORE it travels: your own WiFi in
# Boulder, and the destination house's. The Pi then joins whichever one it finds
# when it is plugged in, with no screen and no keyboard.
#
# PRIORITY is an integer, higher wins when both are in range. The destination
# house should be higher than yours, so that if the clock ever comes back for
# repairs it still prefers home when it goes out again.
#
#   ./deploy/add-wifi.sh "Boulder-House" 'secret'    10
#   ./deploy/add-wifi.sh "Minnetonka-5G" 'other'     50
#
# The password is passed to nmcli and never written to this repo. It does land
# in your shell history, so clear that afterwards if it matters:
#   history -d $((HISTCMD-1))

set -euo pipefail

if [ $# -lt 2 ]; then
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

SSID="$1"
PASSWORD="$2"
PRIORITY="${3:-10}"

command -v nmcli >/dev/null 2>&1 || {
  echo "nmcli not found. Raspberry Pi OS Bookworm uses NetworkManager;" >&2
  echo "on an older image use raspi-config or wpa_supplicant.conf instead." >&2
  exit 1
}

if [ "${#PASSWORD}" -lt 8 ]; then
  echo "A WPA password must be at least 8 characters." >&2
  exit 2
fi

# Replace rather than duplicate, so re-running with a new password just works.
if nmcli -t -f NAME connection show | grep -qx "${SSID}"; then
  echo "Replacing the existing saved network ${SSID}"
  nmcli connection delete "${SSID}" >/dev/null
fi

nmcli connection add \
  type wifi \
  con-name "${SSID}" \
  ifname wlan0 \
  ssid "${SSID}" \
  -- \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "${PASSWORD}" \
  connection.autoconnect yes \
  connection.autoconnect-priority "${PRIORITY}" >/dev/null

echo "Saved ${SSID} (priority ${PRIORITY})."
echo
echo "Saved networks, highest priority first:"
nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show \
  | awk -F: '$2 ~ /wireless/ { printf "  %-28s priority %s\n", $1, $3 }' \
  | sort -k3 -rn
echo
echo "Currently connected to: $(nmcli -t -f NAME,DEVICE connection show --active | grep wlan0 | cut -d: -f1 || echo 'nothing on wlan0')"
echo
echo "To test without rebooting:  nmcli connection up \"${SSID}\""
