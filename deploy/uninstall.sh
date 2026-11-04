#!/usr/bin/env bash
#
# Remove the systemd service and the drop-ins install.sh added.
# Leaves the checkout, the virtualenv, config/clock.yaml, .env and var/ alone.
#
#   ./deploy/uninstall.sh

set -euo pipefail
sudo_if_needed() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

sudo_if_needed systemctl disable --now clockd.service 2>/dev/null || true
sudo_if_needed rm -f /etc/systemd/system/clockd.service
sudo_if_needed rm -f /etc/systemd/system.conf.d/clockd-watchdog.conf
sudo_if_needed rm -f /etc/systemd/journald.conf.d/clockd.conf
sudo_if_needed rm -f /etc/udev/rules.d/99-weasley-pico.rules
sudo_if_needed systemctl daemon-reload
sudo_if_needed udevadm control --reload-rules 2>/dev/null || true

echo "Removed the service and its drop-ins."
echo "Left alone: the checkout, .venv, config/clock.yaml, .env and var/."
