#!/usr/bin/env bash
#
# Join the Pi to your tailnet, so you can reach the clock from Boulder.
#
#   ./deploy/tailscale-setup.sh
#
# DO THIS BEFORE THE PI TRAVELS. Once the clock is in Minnesota or Illinois
# there is no other way in: no port forwarding, no dynamic DNS, and nobody on
# site who can read an IP address off a screen.
#
# After this, the UI is at http://weasley-clock:8080 from any device on your
# tailnet, and ssh works the same way.

set -euo pipefail

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
step() { printf '%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()   { printf '    %s✓%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn() { printf '    %s!%s %s\n' "${YELLOW}" "${OFF}" "$*"; }

step "Installing Tailscale"
if command -v tailscale >/dev/null 2>&1; then
  ok "already installed: $(tailscale version | head -1)"
else
  curl -fsSL https://tailscale.com/install.sh | sh
  ok "installed"
fi

step "Setting the hostname"
CURRENT="$(hostname)"
if [ "${CURRENT}" != "weasley-clock" ]; then
  warn "hostname is '${CURRENT}', not 'weasley-clock'"
  warn "to change it:  sudo hostnamectl set-hostname weasley-clock && sudo reboot"
else
  ok "hostname is weasley-clock"
fi

step "Joining the tailnet"
if tailscale status >/dev/null 2>&1 && [ "$(tailscale status --json | grep -o '"BackendState":"[^"]*"' | cut -d'"' -f4)" = "Running" ]; then
  ok "already connected"
else
  echo "    A login URL will be printed. Open it on your laptop or phone and"
  echo "    approve this machine."
  echo
  # --ssh lets you get a shell over Tailscale without exposing port 22.
  # --accept-dns=false keeps the Pi using the house's DNS, which avoids a
  # chicken-and-egg problem if the tailnet is ever unreachable.
  sudo tailscale up --ssh --hostname=weasley-clock --accept-dns=false
  ok "joined"
fi

step "Status"
tailscale status || true
echo
IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
echo "    Tailscale IP: ${IP:-unknown}"
echo "    UI:           http://weasley-clock:8080/"
echo "    SSH:          ssh $(id -un)@weasley-clock"
echo
warn "In the Tailscale admin console, disable key expiry for this machine."
warn "Otherwise it drops off the tailnet in ~6 months and you lose the clock."
warn "  https://login.tailscale.com/admin/machines  ->  weasley-clock  ->  Disable key expiry"
