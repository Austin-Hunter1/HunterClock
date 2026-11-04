# PROGRESS

Running log for the tracking system. Branch `claude/tracking-system`.

**Legend**
*Tested in sim* — exercised by pytest or by `make sim` on a Linux container.
*Untested on hardware* — written and reviewed, but no Pico, motor, Pi or ESP32
has ever run it.
*Not done* — not written.

---

## Status: 2026-11-04, push 8

**P0 and P1 are complete. P2 is done except the fallback hotspot.**
512 tests pass; `ruff check .` is clean; the ESP32 firmware compiles.

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

---

## Not done

* **Fallback hotspot on the Pi** for on-site setup — P2, the one remaining
  item. The ESP32 nodes have their own captive portal; the Pi does not. In
  practice Tailscale plus two pre-saved WiFi networks covers the same ground,
  and a hotspot the Pi falls back to is another thing that can go wrong
  unattended.
* **Home Assistant source** is written and unit-tested, but has never polled a
  real Home Assistant.

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
