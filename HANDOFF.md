# Handoff — Pi controller, 2026-11-25

Point-in-time note from the cloud session that did the `claude/pi-controller`
work, written for a session running on Austin's laptop with direct access to
the machines. **Delete this file once it has been read and acted on** — it is
a snapshot, not documentation. The durable record is `PROGRESS.md`.

---

## What we are trying to do right now

Get **live locations of the six family members** onto the clock. Everything
from the MQTT broker inward is built and tested; what remains is almost
entirely outside it — a location source, the broker reachable by the display
nodes, and six people's phones.

Austin has chosen the **Home Assistant route** over OwnTracks, because it asks
nothing of the phones. HA talks to Apple's cloud, the Pi reads HA over the
LAN.

**Shortest path to the first real location on the dashboard:** finish the Pi
install → broker configured (done) → stand up HA → connect one Apple Account
→ point one person's `hass_entity` at their `person.` entity → watch the Logs
page. That de-risks the other five.

---

## The machines

| | |
|---|---|
| Pi | Raspberry Pi 4, 8 GB, hostname `weasley-clock`, user `hunterboys` |
| Repo on the Pi | `~/HunterClock`, branch `claude/pi-controller`, remote over SSH |
| Laptop | `Cirdan__Lenovo`, Windows, repo at `D:\03_Projects\Birthday_Clock\` |
| Destination | Minnetonka, MN — so the default `home:` coordinates are already correct |

**The Pi is on an SD card and that card has already corrupted once** — an
empty object in `.git/objects`, which is a lost write. It was recoverable
(everything was on origin). An SSD is on the way. Until it arrives:

* clockd, Mosquitto and journald write very little and are fine on the card;
* **do not install Home Assistant on it.** The HA recorder writes
  continuously and will accelerate exactly this failure. Run HA on the laptop
  in the meantime to test the Apple side, or wait for the SSD.

Docker is installed on the Pi (29.10.1, arm64, daemon running).

---

## Branch state

`claude/pi-controller`, pushed, **685 tests passing, `ruff check .` clean**.
Draft PR is <https://github.com/Austin-Hunter1/HunterClock/pull/2> and its
body is out of date — it predates the Mosquitto work.

Three branches are live and this one has absorbed commits from another:
`claude/pi-controller` (this work), `claude/pico-motors` (firmware/motors),
`claude/tracking-system`. Someone merged `pico-motors` into this branch on the
Pi, so it carries firmware commits this session did not write or review.

### Scope — do not edit these without saying so loudly

* `firmware/**` — another session owns it
* `docs/PROTOCOL.md` — shared contract, the Pico firmware implements the other half
* `build_hands.py`, `build_bezels.py`, `build_cad.py`, `cad/**`,
  `tools/render_front.py`, `tools/preview3d.py`, `docs/build-board.html`,
  `docs/circuit-diagram.html` — an active session is iterating on these

Owned here: `controller/**`, `deploy/**`, `config/**`, `tools/hass_check.py`,
`docs/RUNBOOK.md`, `docs/TESTING.md`, `docs/ARCHITECTURE.md`.

---

## Verified on real hardware (the only things that are)

**The broker setup.** `./deploy/mosquitto-setup.sh` ran clean on the Pi
against its real, already-configured Mosquitto 2.0.21, with all four checks
passing — including `'display' is refused on clock/location/...`, which is the
one that matters. Nothing else in this project has ever run on hardware.

Everything else remains **untested on hardware**: the fallback hotspot in its
entirety, the Pico firmware, motors, the ESP32 display nodes, alert delivery.
`PROGRESS.md` has the full list. Do not let that claim slip.

---

## Open items, in priority order

### 1. The display password is not readable from `.env` (bug, fix pushed but unverified)

`mosquitto-setup.sh` printed `"mqtt_password": ""` in its closing
instructions and **reissued the password on the second run** instead of
keeping it, so `.env` never got a readable `MQTT_DISPLAY_PASSWORD`. That
string is needed to flash six ESP32 boards.

A defensive fix is pushed (guarantee a trailing newline before appending, and
read the value straight back, failing loudly on a mismatch). **The root cause
on that Pi is not confirmed** — its `.env.example` does end with a newline, so
the obvious explanation does not hold. Diagnose with:

```bash
cd ~/HunterClock
grep -n "MQTT" .env | sed 's/=.*/=<redacted>/'
tail -c 1 .env | od -c
ls -l .env
git pull && ./deploy/mosquitto-setup.sh
```

### 2. A broken git ref nags on every pull

```bash
git update-ref -d AUTO_MERGE 2>/dev/null; rm -f .git/AUTO_MERGE
git fsck --full 2>&1 | head
```

Left over from the corrupted merge. Harmless, noisy.

### 3. Is clockd actually running?

Never confirmed. `install.sh` has only ever been run on this Pi once and its
log (`~/install-run1.log`) has not been read — it is the one script on this
branch that has never run outside a stub harness.

```bash
grep password_hash config/clock.yaml    # empty? .venv/bin/python -m clockd.hashpw
sudo systemctl restart clockd
systemctl status clockd --no-pager -l
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:8080/   # 303 is correct
```

### 4. Home Assistant — the actual critical path

Nothing can show a location until this exists. On the laptop for now, not the
Pi's SD card.

**The one unknown that shapes everything:** whether a single Apple Account
surfaces all six people. HA's iCloud integration enumerates *devices on the
account* (Find My → Devices). Find My → *People*, which is how family location
sharing works, is a different mechanism the built-in integration does not
reliably read. HA does offer a family-sharing option at setup, but community
reports on whether it surfaces everyone are inconsistent in both directions.

Resolve it empirically before building on it:

```bash
export HASS_TOKEN='...'          # HA → profile → Security → Long-lived tokens
python tools/hass_check.py --url http://<ha-host>:8123
```

That lists every `person.` and `device_tracker.` entity, whether each carries
coordinates, and where each would put its hand using the clock's own mapping.
Count the names. If fewer than six, each missing person needs their own Apple
Account added as a separate integration entry — and each one is another 2FA
re-prompt to maintain, which is the recurring cost of this route.

**Apple-side prerequisites**, none of which need a computer:

* Find My on for every device (Settings → name → Find My → Find My iPhone)
* Share My Location on for every person
* Decide which Apple Account HA signs into — it ends up holding a full iCloud
  credential, so this is a deliberate choice, not a default
* App-specific passwords do **not** work; it needs the real password + 2FA
* Expect "someone signed into your account" mail from Apple — that is HA

**Point `hass_entity` at `person.<name>`, not `device_tracker.<name>_iphone`.**
The example config already does. A person entity is HA's aggregate and keeps
working when Find My goes quiet; a bare tracker goes stale. The trade: a
person entity carries no `battery_level`, so the UI loses that reading from
this source. Both halves are pinned by tests.

Not yet written: `deploy/homeassistant/` — a pinned compose file and a tuned
`recorder:` config. Waiting on the SD-vs-SSD answer (`findmnt -n -o SOURCE /`).

### 5. Six people participating

The real bottleneck, and no code moves it. Each OwnTracks `user` string must
exactly match `config/clock.yaml` if that route is ever used.

### 6. Zeroing

Austin will do this. Hands stay `unverified` and **deliberately refuse to
move** until someone walks `/assist` at the clock.

---

## Gotchas that have already cost time

**The bench ENABLE bridge.** `firmware/pico/main.py` has an uncommitted (or
stashed, as `bench: ENABLE hardwired low`) change setting `ENABLE_PIN = None`
because GP14 is bridged to ground on the bench. Consequences: the Pico's
`set_enable` is a no-op, so **the Control page reports "drivers disabled"
when all six motors are still energised** — it reports the command it sent,
not the pin. Also `motion.idle_disable` does nothing and six 1.5 A steppers
are held continuously. Fine on a bench, must not ship, and should be a config
flag rather than an edit to the pin table. Written up in `docs/RUNBOOK.md` §4.
It does **not** break zeroing — the wizard jogs with arrow buttons.

**The broker was already configured.** `/etc/mosquitto/conf.d/clock.conf` from
October declares `listener`, `allow_anonymous` and `password_file`. Most
Mosquitto settings are single-valued, so a second declaration is fatal, not an
override. The setup script now detects and inherits. Related trap found the
same way: `max_connections` is *per-listener*, so emitting it in a file that
declares no listener makes Mosquitto create the default listener — a second
bind on 1883 and "Address already in use" from a config with one listener line.

**Broker accounts are split by role.** `clock` (the controller) and `display`
(the six nodes). A node keeps its password in cleartext on its own flash, and
with a shared account anyone who can unscrew a bezel could publish to
`clock/location/<person>` and move every hand. The firmware's
`config.example.json` still says `"mqtt_user": "clock"` — that file belongs to
the firmware session, so it was left alone, but each node must be flashed with
`display`.

**Git on the Pi:** identity is set, remote is SSH, and the key was generated
with no passphrase so it works unattended. That means **anyone holding that SD
card can push to the repo.** Before the clock ships, remove the key or swap it
for a read-only deploy key — the clock only ever needs to pull.

---

## Useful commands

```bash
# broker
./deploy/mosquitto-setup.sh --show         # which accounts exist
journalctl -u mosquitto -f

# controller
systemctl status clockd --no-pager -l
journalctl -u clockd -f
mosquitto_sub -h localhost -u clock -P "$MQTT_PASSWORD" -t 'clock/#' -v

# the fallback hotspot (never run on hardware)
python -m clockd.hotspot --status          # changes nothing, safe on the live clock
python -m clockd.hotspot --demo            # look at the setup page, no Pi needed

# no hardware at all
make sim                                   # fake Pico, in-memory broker, fake locations
make test && make lint
```

`docs/RUNBOOK.md` is the operational reference: §1.5a the broker, §2.2 Find My
end to end, §4 zeroing, §11 the fallback hotspot.
