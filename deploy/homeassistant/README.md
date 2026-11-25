# Home Assistant on the clock's Pi

Route 2 in `docs/RUNBOOK.md` §2.2: Find My → iCloud → Home Assistant → clockd.
This directory runs that Home Assistant on the clock's own Pi, in Docker, in a
way an SD card survives.

| | |
|---|---|
| `setup.sh` | install, re-run, restore or migrate. Idempotent. |
| `backup.sh` | archive `/config` to `~/HunterClock-backups/`, keep the newest 5 |

## Why it is built like this

The Pi's SD card has already lost writes twice (`.git/objects`, then a whole
line of `.env`). Home Assistant's recorder writes its history database
continuously, which is exactly the load that kills SD cards. So:

* **History lives in RAM** (`recorder: db_url: "sqlite:///:memory:"`). The
  history graphs in HA reset on every restart. The clock never reads them.
* **The log is at `warning`**, so HA's own log file stays small.
* **`/config` is inside a fixed-size image**, `/srv/ha-config.img` (1 GiB by
  default), mounted at `/srv/homeassistant`. HA cannot use more of the card
  than that, however it misbehaves. In practice it uses a few MB.
* **The empty mountpoint is immutable.** If the image ever fails to mount, HA
  fails to write instead of quietly starting a blank install on the bare card
  -- a blank HA would look healthy and track nobody.
* **Docker waits for the mount at boot** (`RequiresMountsFor`), and the
  container is `--restart unless-stopped`, so a power cut needs no one.
* **The version is pinned.** An HA upgrade is a deliberate `--version` re-run,
  not something that happens to the clock in another state.

Verified on the real Pi on 2026-11-25, including a reboot: back in about a
minute, iCloud still signed in, six people still resolving, clockd polling.

## First time

```bash
./deploy/homeassistant/setup.sh
```

Then, from a browser on the same network, `http://<pi>:8123`:

1. **Create the owner account.** Local to this HA; any name.
2. **Settings → Devices & services → Add integration → Apple iCloud.** Sign in
   with an Apple Account that is **in the family's Family Sharing group**, and
   **turn on the option to include family members' devices.** Without it you
   get only that account's own devices. Real password and a 2FA code; an
   app-specific password does not work.
   Each person must have *Share My Location* with the family switched on.
3. **Settings → People → Add person**, one per clock hand, named exactly as
   the clock expects (`person.becky`, `person.evan`, ...). Assign each their
   **iPhone and Apple Watch only** -- a MacBook, iPad or AirPods sits at home
   and drags the hand back there. The person entity follows whichever
   assigned device reported last.
4. **Profile → Security → Long-lived access tokens → Create.** Put it in the
   Pi's `.env` as `HASS_TOKEN=`, never in `config/clock.yaml`.
5. In `config/clock.yaml`: `sources.hass.enabled: true`,
   `sources.hass.url: "http://localhost:8123"`, and each person's
   `hass_entity`. Restart clockd.
6. `python tools/hass_check.py --url http://localhost:8123` should list six
   person entities, each `OK`.
7. `./deploy/homeassistant/backup.sh`.

## Day to day

* **Apple will ask for 2FA again** every few weeks. HA shows the iCloud
  integration as needing attention; re-authenticate there. The hands go
  `stale` and hold position meanwhile, and pick up on the next poll with no
  restart.
* **Back up after any change** made in the HA UI: `./deploy/homeassistant/backup.sh`.
  The archive holds the iCloud session -- keep it private.
* Logs: `sudo docker logs --tail 50 homeassistant`.

## Restoring

Onto a fresh card, or after the image is lost:

```bash
./deploy/homeassistant/setup.sh --restore ~/HunterClock-backups/ha-config-<time>.tgz
```

It refuses to overwrite a config that is already there.

## Moving to the SSD

The whole Home Assistant state is one file, `/srv/ha-config.img`.

* **SSD becomes the root filesystem by cloning the card:** nothing to do. The
  image, the fstab line and the Docker drop-in all come along.
* **SSD mounted somewhere else** (say `/mnt/ssd`): stop HA, unmount, move the
  file, point the fstab line at it, mount, start.

  ```bash
  sudo docker stop homeassistant && sudo umount /srv/homeassistant
  sudo cp -p /srv/ha-config.img /mnt/ssd/ha-config.img
  sudo sed -i 's|^/srv/ha-config.img |/mnt/ssd/ha-config.img |' /etc/fstab
  sudo systemctl daemon-reload && sudo mount /srv/homeassistant
  sudo docker start homeassistant
  ```

  Keep the old file until the clock has run for a day; then delete it.

Once on an SSD, the history database can come out of RAM (delete the
`recorder:` block and restart HA) if the graphs are wanted.

## Upgrading Home Assistant

```bash
./deploy/homeassistant/backup.sh
./deploy/homeassistant/setup.sh --version 2026.12.1
```

Read HA's release notes for breaking changes to the **iCloud** and **person**
integrations first. If it goes wrong, re-run with the old `--version`.
