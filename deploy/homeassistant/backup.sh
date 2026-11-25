#!/usr/bin/env bash
#
# Copy Home Assistant's /config to ~/HunterClock-backups/ha-config-<time>.tgz.
#
#   ./deploy/homeassistant/backup.sh          keeps the newest 5
#   KEEP=10 ./deploy/homeassistant/backup.sh
#
# The archive holds the iCloud session and every HA credential, so it is made
# mode 600 in a mode 700 directory outside the repo. Do not commit it, email
# it, or leave it on a laptop you lend out. Restore with
#   ./deploy/homeassistant/setup.sh --restore <file>
#
# Safe while Home Assistant is running: the files that matter (.storage/*) are
# small and rewritten whole, and the history database is in memory.

set -euo pipefail
MNT=/srv/homeassistant
DEST="${HOME}/HunterClock-backups"
KEEP="${KEEP:-5}"
sudo_if_needed() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

mountpoint -q "${MNT}" || { echo "${MNT} is not mounted; nothing to back up" >&2; exit 1; }
mkdir -p "${DEST}"; chmod 700 "${DEST}"
OUT="${DEST}/ha-config-$(date +%Y%m%d-%H%M%S).tgz"
sudo_if_needed tar -C "${MNT}" --exclude='./home-assistant_v2.db*' --exclude='./lost+found' -czf "${OUT}" .
sudo_if_needed chown "$(id -u):$(id -g)" "${OUT}"
chmod 600 "${OUT}"
echo "saved ${OUT} ($(du -h "${OUT}" | cut -f1))"

# Prune, newest first.
ls -1t "${DEST}"/ha-config-*.tgz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f -- "${old}"; echo "removed old ${old##*/}"
done
