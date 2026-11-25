#!/usr/bin/env bash
#
# Remove the systemd service and the drop-ins install.sh added.
# Leaves the checkout, the virtualenv, config/clock.yaml, .env and var/ alone.
#
#   ./deploy/uninstall.sh

set -euo pipefail
sudo_if_needed() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

sudo_if_needed systemctl disable --now clockd.service 2>/dev/null || true
sudo_if_needed systemctl disable --now clock-hotspot.service 2>/dev/null || true
sudo_if_needed rm -f /etc/systemd/system/clockd.service
sudo_if_needed rm -f /etc/systemd/system/clock-hotspot.service
sudo_if_needed rm -f /etc/NetworkManager/dnsmasq-shared.d/weasley-captive.conf
sudo_if_needed rm -f /etc/systemd/system.conf.d/clockd-watchdog.conf
sudo_if_needed rm -f /etc/systemd/journald.conf.d/clockd.conf
sudo_if_needed rm -f /etc/udev/rules.d/99-weasley-pico.rules
# The broker may be shared with other things, so only this clock's drop-in
# goes; the accounts in weasley.passwd are left for you to remove by hand if
# you want them gone.
sudo_if_needed rm -f /etc/mosquitto/conf.d/weasley.conf
sudo_if_needed systemctl daemon-reload
sudo_if_needed udevadm control --reload-rules 2>/dev/null || true

# Leave no access point behind: a Pi stranded as an AP with nothing serving
# the page is reachable by nobody at all.
if command -v nmcli >/dev/null 2>&1; then
  sudo_if_needed nmcli connection down id weasley-setup-ap 2>/dev/null || true
  sudo_if_needed nmcli connection delete id weasley-setup-ap 2>/dev/null || true
fi

echo "Removed both services, their drop-ins and the setup access point profile."
echo "Left alone: the checkout, .venv, config/clock.yaml, .env, var/, and the"
echo "            broker accounts in /etc/mosquitto/weasley.passwd."
if [ -f /etc/mosquitto/conf.d/weasley.conf ] || [ -f /etc/mosquitto/weasley.acl ]; then
  echo "Note: mosquitto was reconfigured; restart it to drop the listener:"
  echo "  sudo systemctl restart mosquitto"
fi
