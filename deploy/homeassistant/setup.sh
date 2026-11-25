#!/usr/bin/env bash
#
# Home Assistant on the clock's own Pi, gentle on an SD card.
#
#   ./deploy/homeassistant/setup.sh                       fresh install, or re-run
#   ./deploy/homeassistant/setup.sh --restore FILE.tgz    rebuild from a backup.sh archive
#   ./deploy/homeassistant/setup.sh --from-container NAME copy /config out of a running
#                                                         container (even one on tmpfs)
#   options: --version 2026.11.3   --size 1G
#
# What it sets up, and why (deploy/homeassistant/README.md has the long version):
#
#   /srv/ha-config.img   a fixed-size ext4 image. HA's /config lives inside it,
#                        so HA can never use more of the card than --size.
#   /srv/homeassistant   where that image is mounted (fstab: loop, noatime,
#                        commit=60, nofail). The empty mountpoint is made
#                        immutable, so an image that fails to mount makes HA
#                        fail loudly instead of starting blank on the bare card.
#   docker.service       gets RequiresMountsFor=/srv/homeassistant, so the
#                        container never starts before the image is mounted.
#   configuration.yaml   recorder in memory (history is lost on restart; the
#                        clock never reads it) and the log at warning.
#   container            "homeassistant", host network, the pinned version,
#                        --restart unless-stopped, docker log capped at 2x10 MB.
#
# Idempotent: every step checks before it acts, so re-running after a partial
# failure picks up where it stopped. It never overwrites an existing config.
#
# Verified on the real Pi 2026-11-25: survived a reboot with the iCloud
# sign-in, six person entities and the clock's token intact.

set -euo pipefail

NAME=homeassistant
IMG=/srv/ha-config.img
MNT=/srv/homeassistant
SIZE=1G
VERSION=2026.11.3
RESTORE=""
FROM=""
DROPIN=/etc/systemd/system/docker.service.d/ha-config-mount.conf

while [ $# -gt 0 ]; do
  case "$1" in
    --restore) RESTORE="$2"; shift 2 ;;
    --from-container) FROM="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --size) SIZE="$2"; shift 2 ;;
    -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "${RESTORE}" ] && [ -n "${FROM}" ] && { echo "--restore and --from-container are exclusive" >&2; exit 2; }
[ -z "${RESTORE}" ] || [ -f "${RESTORE}" ] || { echo "no such backup: ${RESTORE}" >&2; exit 2; }

BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
step() { printf '%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()   { printf '    %s✓%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn() { printf '    %s!%s %s\n' "${YELLOW}" "${OFF}" "$*"; }
die()  { printf '    %sx%s %s\n' "${RED}" "${OFF}" "$*" >&2; exit 1; }
sudo_if_needed() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
# Debian keeps mkfs and friends in /sbin, off a normal user's PATH.
PATH="${PATH}:/usr/sbin:/sbin"

IMAGE="ghcr.io/home-assistant/home-assistant:${VERSION}"

# ------------------------------------------------------------------ checks
step "Checking"
command -v docker >/dev/null || die "docker is not installed"
sudo_if_needed docker info >/dev/null 2>&1 || die "docker is not running"
ok "docker $(sudo_if_needed docker version --format '{{.Server.Version}}')"
if [ -n "${FROM}" ]; then
  sudo_if_needed docker exec "${FROM}" true 2>/dev/null \
    || die "container ${FROM} is not running, so there is nothing to copy out of it"
  ok "will copy /config out of running container ${FROM}"
fi
case "$(findmnt -n -o SOURCE /)" in
  /dev/mmcblk*) warn "root is on the SD card: the history-in-memory setup below matters" ;;
  *) ok "root is on $(findmnt -n -o SOURCE /)" ;;
esac

# ------------------------------------------------------------------ the image
step "A fixed ${SIZE} image for /config"
if [ -f "${IMG}" ]; then
  ok "${IMG} already exists ($(du -h --apparent-size "${IMG}" | cut -f1) cap); kept"
else
  sudo_if_needed fallocate -l "${SIZE}" "${IMG}"
  sudo_if_needed chmod 600 "${IMG}"
  sudo_if_needed mkfs.ext4 -q -L haconfig -m 0 "${IMG}"
  ok "created ${IMG}"
fi
if ! mountpoint -q "${MNT}"; then
  sudo_if_needed mkdir -p "${MNT}"
  sudo_if_needed chattr +i "${MNT}" 2>/dev/null || warn "could not make ${MNT} immutable"
fi
LINE="${IMG} ${MNT} ext4 loop,noatime,commit=60,nofail 0 2"
if grep -qF "${IMG}" /etc/fstab; then
  ok "already in /etc/fstab"
else
  echo "${LINE}" | sudo_if_needed tee -a /etc/fstab >/dev/null
  ok "added to /etc/fstab"
fi
sudo_if_needed systemctl daemon-reload
mountpoint -q "${MNT}" || sudo_if_needed mount "${MNT}"
ok "mounted: $(df -h "${MNT}" | tail -1 | awk '{print $3 " used of " $2}')"

step "Docker waits for it at boot"
sudo_if_needed mkdir -p "$(dirname "${DROPIN}")"
printf '[Unit]\nRequiresMountsFor=%s\n' "${MNT}" | sudo_if_needed tee "${DROPIN}" >/dev/null
sudo_if_needed systemctl daemon-reload
ok "${DROPIN}"

# ------------------------------------------------------------------ the config
step "Config"
have_config() { sudo_if_needed test -e "${MNT}/.storage/core.config_entries"; }
clear_image() {
  sudo_if_needed find "${MNT}" -mindepth 1 -maxdepth 1 ! -name lost+found -exec rm -rf {} +
}
if have_config; then
  [ -n "${RESTORE}${FROM}" ] && die "${MNT} already holds a config; refusing to overwrite it.
    Move it aside by hand first if you really mean to replace it."
  ok "existing config kept"
elif [ -n "${RESTORE}" ]; then
  clear_image
  sudo_if_needed tar -C "${MNT}" -xpzf "${RESTORE}"
  have_config || die "${RESTORE} did not contain a Home Assistant config"
  ok "restored from ${RESTORE}"
elif [ -n "${FROM}" ]; then
  clear_image
  # Not `docker cp`: it reads the container's image layers and cannot see a
  # tmpfs mount, so it copies the empty directory underneath. Found on the Pi.
  sudo_if_needed docker exec "${FROM}" tar -C /config \
      --exclude='./home-assistant_v2.db*' --exclude='./home-assistant.log*' -cf - . \
    | sudo_if_needed tar -C "${MNT}" -xpf -
  for f in auth core.config_entries; do
    sudo_if_needed test -s "${MNT}/.storage/${f}" || die "copy is missing .storage/${f}; ${FROM} left running"
  done
  ok "copied $(sudo_if_needed du -sh "${MNT}" | cut -f1) out of ${FROM}"
else
  ok "no config yet: Home Assistant will start its onboarding page"
fi

# Home Assistant writes its own configuration.yaml on first start, so seed the
# same one it would, with the two SD-card settings already in it.
CFG="${MNT}/configuration.yaml"
if ! sudo_if_needed test -e "${CFG}"; then
  sudo_if_needed tee "${CFG}" >/dev/null <<'YAML'
# Loads default set of integrations. Do not remove.
default_config:

# Load frontend themes from the themes folder
frontend:
  themes: !include_dir_merge_named themes

automation: !include automations.yaml
script: !include scripts.yaml
scene: !include scenes.yaml
YAML
  echo "[]" | sudo_if_needed tee "${MNT}/automations.yaml" >/dev/null
  sudo_if_needed touch "${MNT}/scripts.yaml"
  echo "[]" | sudo_if_needed tee "${MNT}/scenes.yaml" >/dev/null
  ok "seeded configuration.yaml"
fi
if sudo_if_needed grep -q 'sqlite:///:memory:' "${CFG}"; then
  ok "recorder already in memory"
else
  sudo_if_needed grep -q '^recorder:' "${CFG}" \
    && die "configuration.yaml already has a recorder: block; set db_url: \"sqlite:///:memory:\" by hand"
  sudo_if_needed grep -q '^logger:' "${CFG}" \
    && die "configuration.yaml already has a logger: block; set default: warning by hand"
  sudo_if_needed tee -a "${CFG}" >/dev/null <<'YAML'

# Weasley Clock: keep the SD card quiet. History lives in RAM and is lost on
# restart; the clock never reads it. Only warnings reach home-assistant.log.
recorder:
  db_url: "sqlite:///:memory:"
logger:
  default: warning
YAML
  ok "recorder in memory, log at warning"
fi
sudo_if_needed rm -f "${MNT}"/home-assistant_v2.db*

# ------------------------------------------------------------------ container
step "Container"
if [ -n "${FROM}" ] && [ "${FROM}" != "${NAME}" ]; then
  sudo_if_needed docker rm -f "${FROM}" >/dev/null
  ok "removed ${FROM}"
fi
CURRENT="$(sudo_if_needed docker inspect "${NAME}" --format '{{.Config.Image}}' 2>/dev/null || true)"
if [ "${CURRENT}" = "${IMAGE}" ] && [ "$(sudo_if_needed docker inspect "${NAME}" --format '{{.State.Running}}')" = "true" ]; then
  ok "${NAME} already running ${IMAGE}"
else
  [ -n "${CURRENT}" ] && { sudo_if_needed docker rm -f "${NAME}" >/dev/null; ok "replaced ${NAME} (was ${CURRENT})"; }
  sudo_if_needed docker pull -q "${IMAGE}" >/dev/null
  sudo_if_needed docker run -d --name "${NAME}" --restart unless-stopped --network host \
    -v "${MNT}":/config -e TZ="$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)" \
    --log-opt max-size=10m --log-opt max-file=2 \
    "${IMAGE}" >/dev/null
  ok "started ${NAME} (${IMAGE})"
fi

step "Waiting for it to answer"
code=000
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/ || true)"
  [ "${code}" = "200" ] && break
  sleep 5
done
[ "${code}" = "200" ] || die "no answer on :8123 after 5 minutes: sudo docker logs ${NAME}"
if [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/api/onboarding)" = "404" ]; then
  ok "up, and already set up (onboarding finished)"
else
  ok "up, waiting for onboarding at http://$(hostname -I | awk '{print $1}'):8123"
fi
ok "image: $(df -h "${MNT}" | tail -1 | awk '{print $3 " used of " $2}')"

echo
echo "  Next, if this is a fresh install: deploy/homeassistant/README.md, 'First time'."
echo "  Back it up after any change in the HA UI:  ./deploy/homeassistant/backup.sh"
