# PROGRESS

Running log for the tracking system. Branches `claude/tracking-system` and,
for the Pi controller work from push 17 on, `claude/pi-controller`.

**Legend**
*Tested in sim* — exercised by pytest or by `make sim` on a Linux container.
*Untested on hardware* — written and reviewed, but no Pico, motor, Pi or ESP32
has ever run it.
*Not done* — not written.

---

## Status: 2026-11-25, push 20

**P0, P1 and P2 are complete.** The fallback hotspot was the last open item.
685 tests pass; `ruff check .` is clean; the ESP32 firmware compiles.

A Raspberry Pi has been powered on and is reachable, but nothing in this
branch has run on it yet. Everything below is still simulation.

---

## Done, and tested in simulation

### The mapping (P0)
`controller/clockd/geo.py` — the log distance-to-angle map from
`birthday_clock.m`. All six vectors in spec §3 pass within 1 km and 0.1°, and
the formula is re-derived independently in a test. Shortest-path helpers: 324°
→ 2° is a +38° forward move.

### Pico firmware (P0) — *simulated only, see below*
* `motion.py` — trapezoidal planner. Six axes step off independent deadlines, so
  no channel blocks another. Moves land exactly on the commanded microstep;
  speed and acceleration are clamped; a mid-flight reversal brakes first.
* `protocol.py` — newline-delimited JSON, `seq`-echoed replies, `pos`/`fault`/
  `boot` events, strict validation with stable error codes.
* `core.py` — command dispatch, watchdog, throttled position reports,
  idle-disable, rate-limited flash persistence.
* `main.py` — the pins and nothing else: twelve STEP/DIR lines and the shared
  active-low ENABLE from `docs/circuit-diagram.html`, a non-wrapping microsecond
  clock, a non-blocking line reader.

### Simulator (P0)
`tools/pico_sim.py` — the firmware's own `core`/`motion`/`protocol` with the
pins swapped for counters. In-process (what `--sim` uses) or over stdio, with
fault injection for reset, dropped and corrupted lines. Not a mock: if it
accepts a command, the Pico's parser accepted it.

### Controller (P0)
Config schema with validation, the store, the MQTT abstraction (real broker or
in-memory), the serial link, the four location sources, filtering, the state
machine and the engine. Retained `clock/state/<person>`,
`clock/display/<person>`, `clock/system/status` with a Last Will, and a
heartbeat.

### Web UI (P0 + P1)
Seven pages: Dashboard, Control, Assist, Simulate, Logs, Config, System. FastAPI
plus vendored plain JavaScript — no build step, no CDN. All seven render at
1280 px and 390 px with no JavaScript errors and no horizontal overflow
(`docs/img/ui/`, re-checkable with `node tools/ui_screenshots.js`).

### Deployment (P0)
`deploy/install.sh` (idempotent; venv, systemd, persistent journald, the Pi
hardware watchdog, dialout, a udev rule for a stable `/dev/weasley-pico`),
`clockd.service` with `Restart=always`, `add-wifi.sh`, `tailscale-setup.sh`,
`uninstall.sh`.

`install.sh` was run end to end against stubbed system commands, which found two
real bugs: it grepped `port:` and picked up the MQTT port (1883) instead of the
serial port, and it wrote the udev rule without creating the directory. Both
fixed; it now reads the YAML properly and runs clean.

### Docs (P0 + P1)
`docs/RUNBOOK.md`, `ARCHITECTURE.md`, `PROTOCOL.md`, `TESTING.md`.
`docs/clock-face.html` is a standalone live dial (also published as an artifact)
whose mapping is checked against `clockd.geo` to three decimals.

### Display nodes (P1)
`firmware/esp32/` — LovyanGFX for the GC9A01, PubSubClient, JPEGDEC. Config from
`data/config.json` on LittleFS, so the six boards are identical and only that
file differs. Subscribes to the retained `clock/display/<person>`; publishes
`clock/node/<person>/status`; OTA; captive-portal setup AP.

**This one compiles.** PlatformIO was available, so `pio run` was actually run:
1 005 549 bytes of a 1 310 720 byte app partition, 69 020 bytes of static RAM.
The LittleFS image builds too, with a real 1.08 MB loop in it.

`tools/make_loop.py` and `tools/loopfile.py` — ffmpeg crops a video to a circle,
scales to 240×240 at 10 fps and packs the JPEGs into one `.wcl` container that
`src/loopfile.h` reads. Run end to end here on a generated clip.

### Alerts (P2)
`controller/clockd/alerts.py` — healthchecks.io as a dead-man's switch and
ntfy.sh for push notifications, with debounce and per-condition cooldown so a
flapping USB cable is one message rather than hundreds. Both off by default and
configured by environment variable only.

### Extras beyond the spec
* A udev rule so a replug that moves the Pico to `ttyACM1` does not break it.
* `--sim-time-scale`, so a 60 s scenario can be watched in 6 s.
* `deploy/uninstall.sh`.

---

## Tests

470 passing, ~55 s.

| File | Tests | Covers |
|---|---|---|
| `test_geo.py` | 78 | the spec's mapping vectors, shortest path, wrapping |
| `test_motion.py` | 38 | exact arrival, limits, six channels at once |
| `test_protocol.py` | 72 | framing, validation, every error code |
| `test_config.py` | 42 | schema, duplicate levels, invert/offset, no secrets in the example |
| `test_sources.py` | 55 | each parser against realistic payloads |
| `test_filters_state.py` | 56 | acceptance, deadband, status precedence, the store |
| `test_bus.py` | 23 | topic matching, retain, wildcards, Last Will |
| `test_eventlog.py` | 22 | filtering, bounds, file output |
| `test_integration.py` | 23 | the controller against the simulated Pico |
| `test_api.py` | 61 | auth, pages, actions, config save, diagnostics |
| `test_loopfile.py` | 22 | the `.wcl` container, byte for byte, and the ffmpeg filter |
| `test_alerts.py` | 20 | debounce, cooldown, recovery, and the engine's conditions |
| `test_hass_integration.py` | 16 | the Find My route, against a fake Home Assistant |

Checks worth naming:

* An OwnTracks fix for Boulder commands **−935 microsteps** — 105° backwards
  rather than 255° forwards, which is the shortest path.
* All six spec vectors arrive within 0.6° end to end through MQTT.
* An unverified hand knows its target and deliberately does not go there.
* A Pico reset unverifies every hand, and the controller recovers afterwards.
* The diagnostic bundle contains neither a real `MQTT_PASSWORD` nor a password
  hash, while still reporting which variables were set.
* Live mode with no UI password refuses to start.
* Ten link up/down cycles in a minute produce **no** alert at all.

---

## UNTESTED ON HARDWARE

Nothing in this branch has run on a Pico, a motor, a Pi or an ESP32. No
hardware was connected in the session that wrote it. Specifically:

* **`firmware/pico/main.py`** — the GPIO layer. The pin numbers are transcribed
  from `docs/circuit-diagram.html` and the logic is tested under CPython, but no
  STEP pulse has ever reached an A4988. The 5 µs pulse width, the DIR setup
  time, `machine.WDT`, `machine.reset_cause()` and the USB CDC reader are all
  unverified against real silicon.
* **Motor direction** per channel. `invert` exists for this; which hands need it
  is unknown until they turn.
* **`motion.max_speed = 800`** (a quarter turn per second) is a judgement, not a
  measurement. The brass tube stack may need it lower, especially the inner
  levels; too fast on an open-loop stepper loses steps silently.
* **Flash persistence** on the Pico — the write path is rate limited and
  reviewed, never executed on real flash.
* **Serial reconnection** against a real `/dev/ttyACM0`, including the Pi's
  enumeration behaviour after a replug.
* **The udev rule** — the vendor/product IDs (`2e8a:0005`) are the documented
  ones for a Pico running MicroPython, not ones read off this board.
* **Firmware upload via mpremote** from the System page.
* **The Pi hardware watchdog** drop-in, and whether `dtparam=watchdog=on` is
  already set on the image.
* **Tailscale and WiFi scripts** — written against current `nmcli` and
  `tailscale` behaviour, never run on the actual Pi.
* **Mosquitto with real auth.** The MQTT client is exercised against the
  in-memory broker in tests; `--mqtt-host localhost` works but was not run
  against a password-protected broker.
* **The ESP32 display nodes.** The firmware compiles and the LittleFS image
  builds, but no board has run it. Specifically unverified: the LovyanGFX panel
  settings (`invert`, `rgb_order`, 40 MHz SPI), whether the 115 200-byte 240×240
  sprite actually fits alongside WiFi, MQTT and the JPEG decoder at runtime (the
  build leaves ~258 KB of heap, so it should, and the firmware says "out of
  memory" on the panel rather than rendering garbage if it does not), the
  captive portal, and OTA.
* **Alert delivery.** The logic is tested against a fake HTTP client; no real
  request has been made to healthchecks.io or ntfy.sh.
* **The fallback hotspot**, all of it. `nmcli` is exercised only against a
  simulator. Unverified on hardware: whether the Pi's `brcmfmac` firmware will
  do AP mode on this board at all (the assumption the whole feature rests on),
  whether the captive-portal popup fires on a given iOS or Android version,
  binding port 80 as root, whether the systemd hardening lets it reach
  NetworkManager over D-Bus, and whether 5 / 10 / 3 minutes are the right
  timings.
* **The broker configuration.** The listener, accounts and ACL are tested
  against a real mosquitto 2.0.18 *here*, which is further than most of this
  branch gets, but `deploy/mosquitto-setup.sh` has not been run on the Pi and
  no ESP32 has ever connected to the broker.

---

## Not done

* **Nothing from P0–P2.** The fallback hotspot, which was the last open item
  and is described above, is built.
* **Home Assistant source** is now tested end to end against a fake Home
  Assistant serving realistic `device_tracker` payloads: the full route from an
  entity to commanded microsteps, all six spec angles, plus a wrong token, a
  missing entity, HA being down, a phone with no coordinates, and a coarse fix
  being filtered. Still never polled a *real* Home Assistant.

---

## Done since push 16

### The fallback hotspot (P2, the last open item)
`controller/clockd/hotspot/` — its own systemd unit, its own small config, and
no import of `clockd.config` anywhere (a test walks the ASTs to keep it that
way), because the failures most likely to need a way in are the ones that stop
`clockd` starting.

The objection recorded against building this at all was that a hotspot is
another thing that can go wrong unattended. That was right, and it shaped the
design: the Pi has one radio, so **while the access point is up the clock is
offline**. The hotspot is therefore never a destination. Up after five minutes
offline, down again after ten with nobody on the page, three minutes letting
NetworkManager retry the saved networks, then back up. A long power cut now
fixes itself with nobody touching anything.

### The broker (`deploy/mosquitto-setup.sh`)
`install.sh` skipped Mosquitto because the build plan said it was already
running. Running is not reachable: Mosquitto 2.x with no listener binds
loopback only. The controller is on localhost so it works, and the six display
nodes would never have connected — six dark bezels and no error anywhere.
Accounts are split by role, because a node keeps its password in cleartext on
its own flash and a shared `clock` account would let anyone who can unscrew a
bezel move every hand.

### Find My, worked out end to end
Apple still has no public API, so the real question was which Home Assistant
entity a hand should follow, and the shipped config had all six on the fragile
answer. Now `person.<name>`, which falls back when Find My goes quiet.

---

## Notes and deviations from the spec

* **Branch.** The spec and the request both say `claude/tracking-system`; the
  session harness suggested a different name. Followed the explicit instruction.
* **No gearing.** `docs/hardware-plan.html` mounts each hand directly on its
  motor shaft, so 3200 microsteps = 360° of hand travel. `steps_per_rev` is
  still per-person configurable.
* **A software step loop, not PIO.** `docs/hardware-plan.html` sketched one PIO
  state machine per motor. Six hands at 800 microsteps/s is 4800 pulses/s, one
  every ~208 µs, against a ~2 µs loop iteration on a 125 MHz RP2040 — about a
  hundred times the headroom needed. In exchange the whole real-time path is
  plain Python that pytest can run, which is what makes the simulator worth
  trusting. Acceleration means every step interval differs anyway, so PIO would
  still need the CPU feeding it per step. `MotionController` separates "when is
  the next step due" from "make a pulse", so a PIO backend can replace
  `Hardware.pulse` alone. Written up in `docs/ARCHITECTURE.md`.
* **An extra firmware module.** The spec lists `main.py`, `motion.py`,
  `protocol.py`. Added `core.py` so command *semantics* are shared with the
  simulator too, not just the planner and the codec.
* **`estop` marks every hand unverified.** Stopping at speed can slip the rotor,
  so the reported position becomes a guess. Saying so costs a trip to the clock;
  not saying so costs a clock that is quietly wrong.
* **Sim mode binds to 127.0.0.1** by default rather than `0.0.0.0`, so a laptop
  demo with no password is not served to the whole network.
* **Windows is written for, not tested.** The spec asks for `--sim` on Windows.
  The sim paths avoid Linux-only modules, use `pathlib`, fall back when
  `loop.add_signal_handler` is missing, and wrap every `/proc` and `/sys` read.
  But this session ran only on Linux, so Windows is reasoned-about rather than
  exercised. `make` targets are Linux/macOS only; `docs/TESTING.md` lists the
  direct equivalents, including that Windows PowerShell 5.1 rejects `&&`.
* **Source on/off switches do not persist.** Switching a source off to test
  something should not quietly survive a restart and leave the clock blind next
  month. The Config page is how you make it permanent.
* **The loop budget is 1.3 MB, not the spec's 1.5 MB.** Measured from a real
  build: `default.csv` gives LittleFS 0x160000 = 1 441 792 bytes, so 1.5 MB
  does not fit. 1.3 MB leaves ~140 KB for `config.json` and block overhead.

---

## History

* **Push 1** — mapping, planner, protocol, 188 tests.
* **Push 2** — firmware hardware layer, simulator, config schema, auth.
* **Push 3** — location sources; `docs/clock-face.html` live dial.
* **Push 4** — Pico link, event log, engine. Two bugs found by testing against
  the simulator: `hello` was sent before the reader task existed so every reply
  timed out, and the lifecycle `stop()` was shadowed by the motor `stop()`
  wrapper, so shutting the link down silently sent a motor stop.
* **Push 5** — the seven-page web UI, scenarios, the `clockd` entry point.
* **Push 6** — the rest of the test suite (470), clean lint, `deploy/`, the
  Makefile, and the four docs. Running `install.sh` against stubbed system
  commands found two real bugs: it grepped `port:` and picked up the MQTT port
  as the serial port, and it wrote a udev rule without creating the directory.
* **Push 7** — ESP32 display nodes and the frame-loop packer. The real
  PlatformIO build caught that the spec's "about 1.5 MB" loop budget does not
  fit: LittleFS gets exactly 0x160000 = 1 441 792 bytes, so a 1.5 MB loop
  builds and then fails to upload. Also caught that the circular mask was
  building an alpha channel and then dropping it, since JPEG has no alpha, so
  the corners were never masked at all.
* **Push 8** — alerting, with debounce and cooldown; final screenshots.
* **Push 9** — the ambigram hands, the clock-face verification, the cabinet
  depth conflict and option D, and the print plates.
* **Push 10** — carved display bezels. `build_bezels.py` grew five styles, of
  which `ornate` is now the default: a Louis XV gilt-frame moulding with a
  24-lobe scalloped outline, flutes across the round, and a plain cabochon at
  each quarter. Two things were learned the hard way. Revolving on an XZ
  workplane takes the axis in the *local* frame, so `(0,0,1)` spins about
  global Y and every style came out lying on its side — the reload-and-check
  step caught it. And ornament that is *fused on* does not export: each OCC
  sphere left one cracked edge where its seam met the fuse boundary, 56 of
  them, with the in-memory solid reporting perfectly healthy throughout. The
  ornament is carved instead, which meshes cleanly and is how the real thing
  is made. The `beaded` style was removed rather than kept broken.

  **Untested on hardware.** No bezel has been printed and no display module
  has been test-fitted; the dimensions still come from a product listing, not
  a part in hand.
* **Push 11** — the MK4 plates now carry the bezels as well as the hands.
  `build_hands.py` gained `--tag`, `--extra` and `--extra-count`, so the pair
  is reproducible from the committed script instead of a one-off: three hands
  and three bezels per plate, 172 × 162 and 172 × 177 mm, 12 parts in two
  files. `docs/PRINT-REQUEST.md` has the intake-form answers and the
  description to paste.
* **Push 12** — `tools/render_front.py` renders the clock front square on with
  the hands at four sets of distances, built from the real parts: the measured
  dial plate, the option-D hands and the ornate bezels. `preview3d` gained a
  multi-part scene renderer and a projection helper so captions land on the
  features they name. Two findings came out of looking at it. The displays do
  not fit beside the dial on the 15 in board — 55.5 mm of stile against a
  53.5 mm bezel — so the renders add a 75 mm wing each side. And ordinary
  distances bunch: 2 to 14 km puts four hands inside a 70 degree arc, where
  they overlap enough to be hard to read.
* **Push 13** — a third hand style, `spear`, and the new default: every hand
  15 mm wide, 115 mm long and pointed, with the name in relief at a 12 mm cap
  height. The blade no longer follows the word; the word is set to a fixed
  height inside a fixed blade, so a short name stays short instead of being
  blown up to fill the hand. Measured rather than assumed: the thinnest
  letter stroke is 1.82 mm, well over the 0.8 mm printable minimum, and the
  hands now weigh 5.2–5.3 g each where they ranged 4.2 to 9.7. The MK4 plates
  shrank to 172 × 126 mm.
* **Push 14** — the spear was wrong: a plain bar loses the thing that makes
  these hands worth making. `pointed` keeps the word as the outline and
  regularises it instead — one letter height (18 mm), one width (21.6 mm),
  one length (115 mm), and a concave tapered point taking up whatever the
  word does not. Spear is gone.
* **Push 15** — the hands redrawn from an antique serpentine hand Austin sent:
  round boss with the grub tab behind it as a tail, a barbed collar, the name
  as the pierced ornament, then a waisted neck with one barb running out to a
  fine tip. The ornament moved inboard, which is where that shape puts it; the
  earlier version had the word at the tip and the plain run at the hub, which
  was backwards. Tip stops at 1.0 mm wide because a nozzle cannot print a
  knife edge.
* **Push 16** — each level's name sits 5 mm further out than the one behind
  it, so no two hands carry their word at the same radius. It reduces the
  overlap rather than removing it: 291 mm of word will not fit in 115 mm of
  radius however it is arranged. 5 mm is the most the level ordering allows
  before Micah's neck drops under 22 mm, and `build_hand` now refuses rather
  than trusting that arithmetic.
* **Push 17** — the fallback hotspot. Tested against a fake NetworkManager
  that parses real `nmcli` argv and refuses what the real one refuses, which
  immediately caught that the error translation missed nmcli's actual wording
  for a missing network (`No suitable network with SSID 'x' found`), so the
  family would have been shown raw nmcli text. Three traps the design had to
  dodge, all of which would otherwise have been found on a Pi 2000 km away:
  `ipv4.method shared` makes the interface look `connected` with an address,
  so counting it as online would have left the clock serving a setup page for
  ever; an AP profile that autoconnects can win the radio at boot, which is
  the exact failure the feature exists to prevent; and a failed join leaves a
  profile NetworkManager retries indefinitely in preference to a working
  network. Also found that `StartLimitIntervalSec=0` had been sitting in
  `[Service]` in `clockd.service`, where systemd ignores it — the "do not give
  up" comment above it had never been true.

* **Push 18** — Find My pointed at `person.` entities rather than
  `device_tracker.`, so a hand keeps moving when iCloud stops answering. Not a
  free win, and the tests say so: a person entity carries no `battery_level`,
  because it has no single device to have the battery of. `hass_check.py` now
  lists person entities first and says when somebody is wired to a tracker a
  person entity already covers; it also grew 19 tests of its own against a
  real HTTP server, including that it never prints the token. Runbook 2.2
  rewritten against what Home Assistant's own docs say — and *not* against a
  third-party README claiming the official integration went cache-only, which
  the official docs contradict.

* **Push 19** — `install.sh --no-hotspot`, because installing a thing that
  deliberately takes the Pi off the WiFi, for the first time, on a Pi you are
  reaching over that WiFi, is a way to lose the Pi.

* **Push 20** — the broker. Twenty-one tests start a **real** mosquitto 2.0.18
  with the committed config and ACL and check what actually arrives, which
  caught a wrong assumption: a subscription to `clock/#` is *accepted* and
  then filtered per topic on delivery, so "the subscribe is refused" was never
  the property to test. The real one is that a display node can ask for
  everything and still see nothing it should not. The setup script proves the
  same thing on the Pi before it finishes, because a broker that starts is not
  a broker that works.
