# Weasley Clock tracking system: build spec

This is the brief for a cloud coding session. Read all of it before writing code.
Everything here is public; never commit passwords, tokens or API keys.

## 1. What we are building

A grandfather clock whose six hands point to how far each of six family members is from home.
Hand angle comes from location data. A web UI lets Austin, in **Boulder, CO**, set up, test and
troubleshoot the clock while the hardware sits at a family home in **Minnesota or Illinois**
(the site is not final: make the location config-driven, nothing hard-coded).
Nobody technical will be at the clock, so remote operation is the main design goal.

Deadline: the code must be on a branch and pushed within about 36 hours. The session's credits
expire on 2026-11-05 at 12:59 AM. Commit and push every 30 to 45 minutes. Unpushed work is lost.

## 2. Hardware facts (do not guess; these are fixed)

- **Raspberry Pi 4**, Raspberry Pi OS Lite 64-bit (Bookworm, Python 3.11), hostname `weasley-clock`,
  user `hunterboys`. Mosquitto 2.x already runs on `localhost:1883`, auth required, user `clock`;
  the password comes from env var `MQTT_PASSWORD` and is never committed.
- **Raspberry Pi Pico** (plain RP2040, not a W), MicroPython, connected to the Pi by USB.
  It appears as `/dev/ttyACM0` (USB serial). It generates all stepper pulses.
- **Six A4988 drivers**, MS1/MS2/MS3 tied high (1/16 microstepping), RESET and SLEEP tied high.
  All six ENABLE pins share one line, **active low**.
- **Steppers**: 1.8 degree motors, 200 full steps per rev, so **3200 microsteps per rev**.
  Open loop. There are **no homing sensors**: the position after power-up is unknown until a person
  confirms it.
- **Pico pins** (from `docs/circuit-diagram.html`, Diagram 2):

| Level | Person | STEP | DIR | Motor |
|---|---|---|---|---|
| 1 | Becky | GP2 | GP3 | NEMA23 |
| 2 | Evan | GP4 | GP5 | NEMA23 |
| 3 | Elijah | GP6 | GP7 | NEMA17 |
| 4 | Caleb | GP8 | GP9 | NEMA17 |
| 5 | Austin | GP10 | GP11 | NEMA17 |
| 6 | Micah | GP12 | GP13 | NEMA17 |

  ENABLE (all six): **GP14**. Person-to-level assignment must be configurable.
- **Six ESP32 display nodes** (30-pin dev boards), each driving one GC9A01 240x240 round display
  over SPI: SCL=GPIO18, SDA=GPIO23, DC=GPIO2, CS=GPIO5, RST=GPIO4, BLK and VCC=3V3.
  One node per person, on WiFi, MQTT only.
- Read `docs/hardware-plan.html`, `docs/circuit-diagram.html`, `birthday_clock.m` and
  `build_cad.py` in this repo for context.

## 3. The mapping (must match birthday_clock.m exactly)

```
d     = max(haversine(home, person), 1.0)      # km, R = 6371.0
angle = log10(d) / log10(pi * 6371.0) * 360.0   # degrees clockwise from 12 o'clock
```

Home: lat 44.9109, lon -93.5017 (configurable). People (configurable):

| Person | lat | lon | expected distance | expected angle |
|---|---|---|---|---|
| Becky | 44.9109 | -93.5017 | 1 km (clamped) | 0.0 |
| Elijah | 44.9537 | -93.0900 | 33 km | 126.8 |
| Caleb | 41.8661 | -88.1070 | 552 km | 229.5 |
| Austin | 40.0150 | -105.2705 | 1107 km | 254.8 |
| Micah | 44.5646 | -123.2620 | 2338 km | 281.9 |
| Evan | 52.2297 | 21.0122 | 7537 km | 324.5 |

Unit-test these (distance within 1 km, angle within 0.1 degree).
The tubes and shafts have no cable wrap, so hands may turn without limit. Always move
by the **shortest path** (track an unwrapped angle; going from 324 to 2 degrees is a short forward move).

## 4. Architecture

```
phones (OwnTracks, or Home Assistant) --MQTT--> controller (clockd, asyncio, on the Pi)
controller --USB serial, JSON lines--> Pico --STEP/DIR--> A4988 --> motors
controller --MQTT retained state--> ESP32 display nodes
controller --HTTP + WebSocket--> web UI (Austin, over Tailscale)
```

Repo layout to create (keep existing files; do not edit `docs/` pages except to add links):

```
controller/        python package `clockd` + pyproject.toml + tests/
firmware/pico/     MicroPython: main.py, motion.py, protocol.py (planner testable on desktop CPython)
firmware/esp32/    PlatformIO project for the display nodes
tools/             make_loop.py (video to frames), pico_sim.py (fake Pico), replay scenarios
deploy/            install.sh, systemd units, wifi/tailscale scripts
config/            clock.example.yaml (real config is gitignored: config/clock.yaml, .env)
docs/RUNBOOK.md ARCHITECTURE.md PROTOCOL.md TESTING.md   (new files; do not touch existing html)
PROGRESS.md        running log: done / next / untested-on-hardware
```

The controller must run **on Windows and on the Pi** in `--sim` mode (fake Pico, in-memory or local
broker, fake locations) so Austin can try the UI on a laptop. Use pathlib and avoid Linux-only modules
in sim paths.

## 5. Components

### 5.1 Pico firmware (MicroPython)
- Six channels moving **simultaneously**. Trapezoidal acceleration; default max speed 800 steps/s,
  accel 1600 steps/s^2, both configurable and clamped to safe limits. STEP pulse width at least 5 us.
- Put the motion planner in a pure-Python module with no `machine` imports so it runs under CPython tests.
  Hardware access is a thin layer. Prefer a timer or PIO approach that keeps channels from blocking each other;
  explain the choice in ARCHITECTURE.md. Never block the serial reader.
- Serial protocol: newline-delimited JSON, every command has `seq`, every reply echoes it with `ok` or `err`.
  Commands: `hello`/`info` (firmware version, boot reason, uptime), `status`, `move` (channel, absolute target in
  microsteps), `jog`, `set_pos` (declare current position, used for zeroing), `enable` (on/off), `stop` (one or all,
  with deceleration), `estop` (immediate), `set_params`, `ping`. Unsolicited events: `pos` (throttled), `fault`.
- Watchdog: if no `ping` for N seconds (default 10), decelerate and stop; keep the stored position.
- Persist positions to flash only at rest, rate-limited. On boot after a power loss or reset, report
  `needs_verification` so the controller marks every hand "unverified" until a person confirms it.
- Disabling the drivers (ENABLE high) means no holding torque; make idle-disable a config option.
- Document PROTOCOL.md precisely enough that the simulator and the firmware agree.

### 5.2 Controller (`clockd`, Python 3.11, asyncio)
- Config in YAML with schema validation (pydantic): home, people (name, level, lat/lon fallback,
  invert direction, steps/rev scale, zero offset), motion limits, stale timeout, deadband, sources, MQTT, web.
- **Location sources** (each can be enabled or disabled from the UI):
  1. **OwnTracks over MQTT**: topic `owntracks/<user>/<device>`; JSON `{"_type":"location","lat":..,"lon":..,"acc":..,"tst":..,"batt":..}`;
     ignore other `_type` values; map user/device to a person in config.
  2. **Generic MQTT**: `clock/location/<person>` with `{"lat":..,"lon":..,"ts":..,"acc":..}`.
  3. **Home Assistant REST** (optional, lower priority): poll `GET /api/states/<entity>` with a long-lived token from env;
     read latitude/longitude attributes.
  4. **Manual override from the UI** (fake location or fake distance, with an expiry and a visible badge).
- Filtering: drop fixes with accuracy worse than a configurable limit (default 2000 m) or older than the last accepted fix;
  deadband (default 0.5 degree); minimum interval between moves; **stale** status after a configurable time
  (default 6 h) that keeps the last position and shows a flag.
- State machine per person: `ok`, `stale`, `override`, `unverified`, `fault`, `disabled`.
- MQTT output (retained): `clock/state/<person>` {distance_km, target_deg, actual_deg, status, last_seen, source, accuracy_m};
  `clock/display/<person>` for the screens {name, status (home|away|stale|unknown), distance_km, updated_iso, text};
  `clock/system/status` (with Last Will for offline) and `clock/system/heartbeat`.
- Persistence: last accepted fix, per-hand zero offset and verified flag, in a small SQLite or JSON file under `var/`.
- Robust serial link: auto-reconnect, command queue with timeouts, retries, resync after a Pico reset.

### 5.3 Web UI (served by the controller, default port 8080)
No Node build step. FastAPI plus plain JS (htmx or Alpine from a CDN, or vendored), mobile friendly, light and dark.
Auth: a single shared password (hash in config, session cookie), because it will be reachable over Tailscale.
Pages:
1. **Dashboard**: SVG clock face with all six hands (colored per person; solid = actual, ghost = target), a table of
   distance, target and actual angle, last seen, source, accuracy and status badges; a system banner
   (Pico link, MQTT, drivers enabled, firmware version, uptime).
2. **Control**: per hand: jog buttons (+/-1, 10, 90 degrees and a custom value), move to an angle, **Set zero here**
   (declare that the hand is currently at 12), mark verified; enable/disable drivers; a large **STOP ALL**;
   speed and acceleration sliders within limits; **Test sweep** (each hand 0 to 360 and back, slowly); **Move all to 12**.
   Confirm anything that moves every hand at once.
3. **Assist** (`/assist`, phone friendly, big buttons, for a non-technical person standing at the clock): a **guided zeroing wizard**.
   For each hand in turn: show its name and color, let them nudge it with big arrows until it points at 12 o'clock, then
   **Set zero**. Include a one-tap "all hands to their live positions" check at the end.
4. **Simulate**: set a fake location or distance for any person; scenario playback (for example "Evan flies home" over 60 s);
   switch location sources on and off.
5. **Logs**: live serial traffic, MQTT in and out, events, with filters; a rotating on-disk log; download.
6. **Config**: view and edit the YAML with validation, a diff before saving, automatic backup, and apply/restart.
7. **System**: Pico info, upload Pico firmware (via mpremote) with a confirm, CPU temperature, WiFi signal, disk, uptime,
   service status, Tailscale status, restart controller / reboot Pi, **Update from GitHub** (git pull, restart, one-click rollback),
   and **Download diagnostic bundle** (logs, versions, sanitized config) so one click gives everything needed to debug remotely.
Every action that moves hardware is logged with a timestamp. The UI must work with the Pico disconnected (it says so clearly).

### 5.4 Remote operation and deployment (`deploy/`)
- `install.sh`: idempotent; creates the venv, installs dependencies, installs systemd units (`clockd.service` with
  `Restart=always`), sets persistent journald, enables the Pi hardware watchdog, and checks serial permissions.
- Tailscale instructions: join the tailnet **before** the Pi travels; UI at `http://weasley-clock:8080`; optional Tailscale SSH.
- WiFi: `deploy/add-wifi.sh SSID PASSWORD [priority]` using NetworkManager (`nmcli`) so several networks (Boulder, the destination home)
  are saved in advance. Optional fallback hotspot for on-site setup (lower priority).
- Alerts (optional): ping a healthchecks.io URL and/or push to ntfy.sh from env vars when the controller or Pico faults or the heartbeat stops.
- RUNBOOK.md: first-time install, how to log in remotely, the 10-minute remote health check, what each status means,
  how to recover from a brownout or lost position, how to roll back an update, and how to reach someone on site.

### 5.5 ESP32 display nodes (`firmware/esp32/`, lower priority than 5.1 to 5.4)
- PlatformIO, Arduino-ESP32, TFT_eSPI or LovyanGFX for the GC9A01 with the pins above.
- WiFi and MQTT settings from a `config.json` on LittleFS (not compiled in); a setup access point and captive portal when it cannot connect.
- Subscribe to `clock/display/<person>`. Play a looping MJPEG frame sequence from LittleFS, with an overlay: a ring that shows the distance fraction (log scale,
  same mapping as 3), the name, a status line (HOME or "N km away") and "updated N min ago". Distinct states: home, away, stale, disconnected.
- Publish `clock/node/<person>/status` (RSSI, uptime, firmware version); support OTA updates.
- `tools/make_loop.py`: ffmpeg crops a video to a circle, resizes to 240x240, about 10 fps, JPEG-encodes frames into one file,
  and reports the size against a budget (about 1.5 MB). Reduce quality automatically if over budget.
- You cannot test on hardware. If PlatformIO can build in this environment, build it; otherwise say plainly that it is uncompiled.

## 6. Testing (required)

- pytest for: geo and mapping vectors (section 3), motion planner (reaches the target exactly, respects speed and acceleration, shortest path, multi-channel),
  protocol encode and decode, each location source parser with realistic sample payloads, filtering and the state machine,
  config validation.
- An integration test that runs the controller against `tools/pico_sim.py` (it speaks the same protocol, using the same planner code)
  and a real local Mosquitto if available, else an in-memory broker abstraction: publish OwnTracks fixes and assert the commanded steps,
  published state, and display topics.
- API tests (httpx) for auth, jog, set zero, stop, config validation, simulate.
- `make test`, `make lint` (ruff), and `make sim` (starts controller + simulator + UI with fake data). Give exact commands in TESTING.md.
- If a headless browser is available, take screenshots of the Dashboard, Control and Assist pages at desktop and phone width and save them under `docs/img/ui/`.
- Clearly separate in PROGRESS.md and the PR what is **tested in simulation**, what is **untested on hardware**, and what is **not done**.

## 7. Priorities (do in this order; stop adding scope when time or credits run low)

- **P0**: mapping and tests; Pico firmware plus simulator; controller with OwnTracks and generic MQTT sources; web UI with Dashboard, Control,
  Simulate and Logs; install script, systemd unit, RUNBOOK.
- **P1**: Assist wizard; Config and System pages (firmware upload, update and rollback, diagnostic bundle); persistence and stale handling polish;
  ESP32 node firmware and `make_loop.py`.
- **P2**: Home Assistant REST source; ntfy or healthchecks alerts; fallback hotspot; UI screenshots; extra scenarios.

## 8. Working rules

- Work on branch `claude/tracking-system`. Commit and push **every 30 to 45 minutes** with clear messages. Open a pull request into `main` early
  as a draft and keep it updated. Never push to `main` directly.
- Keep `PROGRESS.md` current after each push.
- Do not invent hardware results. Anything that needs the real Pico, motors or ESP32 is "untested on hardware".
- Never commit secrets (MQTT password, Home Assistant token, UI password hash, WiFi credentials). Use `.env` (gitignored) and the example files.
- Location data is private to the family: do not log raw coordinates to any external service; the UI is for Tailscale, not the open internet.
- Prefer boring, readable code with docstrings and type hints. If a requirement here conflicts with the hardware facts or the repo files, follow the repo and note it in PROGRESS.md.
- Finish with a pull request whose description lists: what works (tested how), what is untested on hardware, and the exact steps to deploy on the Pi.
