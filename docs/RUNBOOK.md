# Runbook

The clock is in a house in another state. Nobody there is technical. This is how
to install it, check on it, and fix it from Boulder.

* **UI:** `http://weasley-clock:8080/` over Tailscale
* **Shell:** `ssh hunterboys@weasley-clock`
* **Service:** `clockd`, under systemd, `Restart=always`
* **Logs:** `journalctl -u clockd -f`

---

## 1. First-time install

Do all of this **before the Pi travels.** Once it is in Minnesota there is no
second way in.

### 1.1 Flash and reach the Pi

Raspberry Pi OS Lite 64-bit (Bookworm). In Raspberry Pi Imager's advanced
options set the hostname to `weasley-clock`, the user to `hunterboys`, and
enable SSH. That gets you in without a screen.

### 1.2 Save every WiFi network it will ever see

```bash
./deploy/add-wifi.sh "Your-Boulder-WiFi"   'password'  10
./deploy/add-wifi.sh "Destination-House"   'password'  50
```

Higher priority wins when both are in range. Get the destination network's name
and password from whoever lives there **now**, not when the clock arrives.

### 1.3 Join Tailscale

```bash
./deploy/tailscale-setup.sh
```

Then, in the Tailscale admin console, open `weasley-clock` and **disable key
expiry**. Without that it drops off the tailnet in about six months and you lose
the clock.

### 1.4 Install the controller

```bash
git clone https://github.com/Austin-Hunter1/HunterClock
cd HunterClock
./deploy/install.sh
```

It creates `.venv`, installs the controller, copies `config/clock.example.yaml`
to `config/clock.yaml` and `.env.example` to `.env`, adds `hunterboys` to
`dialout`, installs the systemd unit, turns on persistent journald and the Pi's
hardware watchdog. It is safe to run again after any `git pull`.

### 1.5 Secrets

```bash
.venv/bin/python -m clockd.hashpw        # paste the hash into config/clock.yaml
nano .env                                # MQTT_PASSWORD and CLOCK_SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"   # for CLOCK_SECRET_KEY
```

`.env` is mode 600 and gitignored. Nothing secret ever goes in
`config/clock.yaml` except the password *hash*.

If Mosquitto does not yet have the `clock` user:

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd clock
sudo systemctl restart mosquitto
```

### 1.6 Edit the config

In `config/clock.yaml`:

* `home.lat` / `home.lon` — **the destination house**, not Minnetonka, unless
  that is where it is going. Everything on the dial is measured from here.
* `site` — free text, shows in the UI header.
* `people` — who is on which hub level. Level 1 is the front motor (STEP GP2).
* `people[].owntracks.user` — must match the phone's OwnTracks username.

### 1.7 Start it

```bash
sudo systemctl restart clockd
journalctl -u clockd -f
```

Open `http://weasley-clock:8080/`. Every hand will say **unverified** — that is
correct, and section 4 is how it is fixed.

### 1.8 Phones

Install OwnTracks on each phone. Mode: MQTT. Host `weasley-clock`, port 1883,
username `clock`, the password from `.env`, device ID anything, **username must
match `owntracks.user` in the config**. It publishes to
`owntracks/<user>/<device>`, which is what the controller subscribes to.

---

## 2. The ten-minute remote health check

Do this monthly, or whenever someone says the clock looks wrong.

1. **Open the UI.** If it does not load, go to section 6.
2. **The banner.** `Pico connected · fw 0.1.0 · Drivers enabled · MQTT connected`.
   Anything red is the answer.
3. **The dial.** Six hands. Compare against where you know people are.
4. **The table.** `Last seen` under a few hours for anyone whose phone is on.
   `Status` should be `ok`. Anyone `stale` has a phone that has stopped
   reporting — usually OwnTracks killed in the background, not the clock.
5. **System page.** CPU under 70 °C, disk over 1 GB free, `clockd: active`,
   `mosquitto: active`, Tailscale `Running`.
6. **Logs page.** Filter to `error`. A steady trickle of rejected payloads from
   one person means their phone is misconfigured; a trickle of serial timeouts
   means the USB link is unhappy.
7. **Move something.** Control → jog any hand ±1°, watch `Actual` change, then
   **All to live positions**. If a hand moves on the dial but not in the room,
   that is a mechanical or wiring problem, not software.

---

## 3. What each status means

| Status | Means | Do |
|---|---|---|
| `ok` | Recent fix, hand verified and driven | nothing |
| `stale` | No fix for 6 h. The hand holds its last position | check that person's phone, not the clock |
| `override` | A manual fake position is in force | Simulate → Clear, if you forgot about it |
| `unverified` | Nobody has confirmed where this hand points | section 4 |
| `fault` | Cannot be driven — no Pico link, or the firmware reported one | sections 6 and 7 |
| `disabled` | Switched off in `config/clock.yaml` | nothing, if intended |

`stale` almost always means a phone, not a clock. iOS suspends OwnTracks
aggressively; `docs/hardware-plan.html` notes the same about Home Assistant's
iCloud integration, which is why it is the lowest-priority source here.

---

## 4. Zeroing the hands (needs someone at the clock)

Every hand starts `unverified`, and goes back to `unverified` after a power cut,
a firmware upload or an e-stop. Until it is cleared, that hand **will not move**
from location data. That is deliberate: see `docs/ARCHITECTURE.md`.

Send whoever is at the house this link:

```
http://weasley-clock:8080/assist
```

They need to be on the tailnet, or on the house WiFi with the Pi's local address.
The page is written for someone who has never seen the project: big buttons,
one hand at a time, plain language. It takes about a minute per hand.

What they do: find the hand in the colour shown, tap the arrows until it points
straight up at 12, tap **This hand is at 12**. Repeat for all six, then tap
**Put every hand where it belongs**.

You can watch the whole thing from Boulder on the Dashboard while they do it.

If they cannot get to the page, you can do it yourself over the phone: Control →
jog, while they tell you which way the hand is pointing. Slower, but it works.

---

## 5. Recovering from a power cut or brownout

What happens on its own:

1. The Pi boots, systemd starts `clockd`.
2. The Pico boots and sends `needs_verification`.
3. The controller marks every hand `unverified` and stops driving them.
4. The last fixes, zero offsets and verified flags are read back from
   `var/state.json` — but the verified flags are then cleared, because a hand
   could have been knocked while the power was off.

So: **the clock comes back by itself, but the hands stay put until someone
re-zeros them.** Section 4.

If the hands are only slightly off and you trust them, you can mark them
verified without a trip: Control → **Mark verified** per hand. Only do this if
the dial visually agrees with the table — you are vouching for the position.

---

## 6. The UI will not load

In order:

1. **Is the Pi on the tailnet?** `tailscale status` from your laptop. If
   `weasley-clock` is absent, it is powered off, off the WiFi, or its key
   expired (section 1.3).
2. **Can you ssh?** `ssh hunterboys@weasley-clock`. If yes, the Pi is fine and
   the problem is `clockd`.
3. **Is the service running?**
   ```bash
   systemctl status clockd
   journalctl -u clockd -n 80 --no-pager
   ```
4. **Common causes in the log:**
   * `Refusing to start: No UI password is set` — section 1.5.
   * `Configuration error:` — a bad edit. `git checkout config/clock.yaml`, or
     restore the `.bak` the Config page made.
   * `Address already in use` — an older copy is still running: `sudo systemctl restart clockd`.
5. **Nothing in the log at all?** The Pi may have rebooted mid-write.
   `sudo systemctl restart clockd`.

If you cannot reach the Pi at all, you need someone at the house to power-cycle
it. The hardware watchdog should have done that already if the kernel hung.

---

## 7. The Pico link is down

Banner says `Pico NOT CONNECTED`.

```bash
ls -l /dev/ttyACM*          # is it there at all?
groups hunterboys           # must include dialout
journalctl -u clockd | grep -i pico | tail -20
```

* **No `/dev/ttyACM0`** — the USB cable, or the Pico has lost its firmware.
  Someone at the house can reseat the cable. If it enumerates as `ttyACM1`
  after a replug, switch `serial.port` to `/dev/weasley-pico` (the udev rule
  `install.sh` adds gives it a stable name).
* **Present but no replies** — the firmware is not running. Re-upload it:
  System page → **Upload firmware to the Pico**, or
  `make firmware-upload PICO_PORT=/dev/ttyACM0`. Every hand becomes unverified
  afterwards.
* **Timeouts under load** — look for `watchdog` faults in the log. If the Pi
  stops pinging for 10 s the firmware stops the hands on purpose; that is the
  safety net working, and the cause is usually the Pi being busy or the USB
  link dropping.

The UI keeps working throughout and says what is wrong. The hands just stop.

---

## 8. A hand is in the wrong place

| Symptom | Likely cause | Fix |
|---|---|---|
| Points at the mirror angle | wrong direction | `invert: true` for that person |
| Consistently off by the same amount | zero drifted | re-zero on `/assist` |
| Moves then drifts further every time | losing steps | lower `motion.max_speed`, check the A4988 Vref, check the tube for binding |
| Does not move at all, others do | that channel's wiring, or `enabled: false` | Control → jog it; check STEP/DIR against `docs/circuit-diagram.html` |
| All six stopped | drivers released, or the watchdog tripped | Control → enable drivers |
| Moves in the UI but not in the room | motor, driver or coupling | mechanical; the software did its part |

**Test sweep** on the Control page is the quickest triage: one turn and back,
slowly. It exercises wiring, direction, current limit and the tube stack in
about thirty seconds.

---

## 9. Updating

```
System page → Update from GitHub → (wait) → Restart controller
```

It records the commit it was on first, so **Roll back** has somewhere to go.
Or from a shell:

```bash
cd ~/HunterClock
git pull
./deploy/install.sh          # picks up new dependencies
sudo systemctl restart clockd
```

### Rolling back

System page → **Roll back**, then **Restart controller**. Or:

```bash
cd ~/HunterClock
git checkout --detach "$(cat var/rollback-commit)"
sudo systemctl restart clockd
```

To get back onto the branch afterwards: `git checkout main`.

**Check after any update:** the banner is green, the Pico firmware version is
what you expect, and one hand still jogs.

---

## 10. Getting help, and sending the evidence

System page → **Download diagnostic bundle**. One zip with:

* `snapshot.json` — the full live state
* `events.log` — everything the controller has done
* `system.json` — host health, versions, services, Tailscale
* `config.sanitised.yaml` — the config with every secret-looking value removed
* `store.json`, `mqtt-recent.json`, `environment.txt` (variable **names** only)

It is built to be sent to someone, so it is safe to send. A test asserts that a
real MQTT password and a real password hash are both absent from it.

---

## 11. Reaching someone on site

Keep this filled in:

| Who | Phone | Notes |
|---|---|---|
| | | can power-cycle the Pi |
| | | can open `/assist` on a phone |
| | | knows the WiFi password |

What to ask them for, in order of how often it helps:

1. "Is the little green light on the Pi on?"
2. "Can you unplug the Pi for ten seconds and plug it back in?"
3. "Can you open this link on your phone and tap the buttons?" (`/assist`)
4. "Is the USB cable between the two small boards still plugged in at both ends?"

Do not ask them to type commands. If it needs a shell, it needs Tailscale, and
if Tailscale is down, nothing they can type will help.

---

## 12. Quick reference

```bash
# status and logs
systemctl status clockd
journalctl -u clockd -f
journalctl -u clockd --since "1 hour ago" | grep -i error

# restart
sudo systemctl restart clockd

# watch MQTT
mosquitto_sub -h localhost -u clock -P "$MQTT_PASSWORD" -t 'clock/#' -v

# send a fake location
mosquitto_pub -h localhost -u clock -P "$MQTT_PASSWORD" \
  -t clock/location/austin -m '{"lat":40.0150,"lon":-105.2705,"acc":30}'

# is the Pico there
ls -l /dev/ttyACM* /dev/weasley-pico

# re-upload the firmware
cd ~/HunterClock && make firmware-upload

# try it with no hardware at all
.venv/bin/python -m clockd.cli --sim --sim-fixtures --port 8081
```
