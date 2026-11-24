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
528 tests pass; `ruff check .` is clean; the ESP32 firmware compiles.

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

---

## Not done

* **Fallback hotspot on the Pi** for on-site setup — P2, the one remaining
  item. The ESP32 nodes have their own captive portal; the Pi does not. In
  practice Tailscale plus two pre-saved WiFi networks covers the same ground,
  and a hotspot the Pi falls back to is another thing that can go wrong
  unattended.
* **Home Assistant source** is now tested end to end against a fake Home
  Assistant serving realistic `device_tracker` payloads: the full route from an
  entity to commanded microsteps, all six spec angles, plus a wrong token, a
  missing entity, HA being down, a phone with no coordinates, and a coarse fix
  being filtered. Still never polled a *real* Home Assistant.

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
* **Push 17** — the motor side: a bench bring-up tool, and five failure modes
  that were silent. Branch `claude/pico-motors`.

  `tools/bench.py` is what to hold when a motor first turns. It speaks the
  ordinary protocol to the ordinary firmware — there is no bench mode on the
  Pico — and covers Solder order step 9 in `docs/build-board.html`: `info`
  with nothing but the Pico on USB, then `check --ch 0` to walk one channel
  through sixteen microsteps, a reversal, a quarter turn, and a full
  revolution against a physical mark, asking after each. It releases the
  drivers on the way out of every path, including Ctrl-C and a crash, because
  ENABLE is one pin for all six and leaving it low cooks six coils. `--port
  sim` runs the whole thing with no hardware, which is how its 27 tests work.

  Looking hard at the failure modes, as asked, cost more than writing the tool
  and caught more:

  * **A starved loop made the hands quietly wrong.** The main loop can be away
    for tens of milliseconds — a GC pass, a flash write, a flood of serial.
    Every step whose deadline passed was then due at once, and `service()`
    emitted them back to back: measured, 40 pulses in one call after a 100 ms
    stall at 400 microsteps/s. No stepper follows a burst, so the motor missed
    most of them while the firmware counted all of them, and with no sensor
    anywhere nothing ever noticed. Position wrong by up to 40 microsteps per
    stall, cumulative, for ever. Lateness is now made up by re-scheduling
    (`motion.MAX_CATCHUP_INTERVALS`): no step is lost, the move just finishes
    late, and `late` in `status` counts it so a starved Pico is visible.
  * **`stop` was not idempotent, and a repeated stop never stopped.**
    `ceil(v²/2a)` rounds the braking distance up, so each fresh `stop` granted
    one more microstep. A caller stopping once per loop iteration — a held UI
    button, or the watchdog below — got a hand crawling at 56 microsteps/s
    indefinitely, reporting `moving: true` with nothing to say it was wrong.
    Found by writing the watchdog test, not by reading the code.
  * **The watchdog could only fire once.** The tripped flag cleared only on a
    `ping`, so on a half-dead link — commands arriving, pings not — a hand
    commanded after the trip was never stopped again. It now re-stops whenever
    the link is quiet and something is moving, and announces on an edge rather
    than once per tick.
  * **`positions.json` was rewritten in place.** The documented fallback path
    (`os.rename` refused onto an existing file, which is the normal case on
    some MicroPython ports) truncated and rewrote the real file, which is
    exactly the half-written file a power cut must survive. It now writes
    beside it, reads the copy back before swapping — a full flash can accept a
    write and keep nothing — and `load()` falls back to the `.tmp` copy if the
    power went during the swap. `pos_source` on `boot`/`info` distinguishes
    "never saved" from "saved and unreadable": both give zeros, only one means
    the flash wants looking at.
  * **A crash left six coils energised.** One 0x03 byte on the wire raised
    `KeyboardInterrupt` in the step loop and dropped to the REPL with ENABLE
    still low and nothing feeding the watchdog for eight seconds.
    `micropython.kbd_intr(-1)`, and a `finally` that releases the drivers
    however the loop ends.

  Two smaller ones: a scoped `set_params` applied correctly and then answered
  with channel 0's limits, so a caller comparing what it asked for against what
  came back concluded the firmware had clamped it; and `poll_lines` had no
  bound, so a host writing faster than the Pico executes could starve the step
  loop for ever.

  The lateness fix also exposed that `tools/pico_sim.py` was not faithful: it
  jumps simulated time and called `tick` once, so at `--scale 20` every pulse
  looked a hundred intervals overdue and the hands crawled. It now walks the
  core through the deadlines, which is what a real Pico's loop does.

  66 new tests in `firmware/pico/tests/` (`make test-firmware`); the 528
  controller tests still pass and `ruff check .` is clean. One existing
  assertion changed: `test_service_respects_its_pulse_budget` asserted
  `emitted == 10`, which was the budget capping the catch-up burst — see the
  comment left in its place.

  **Untested on hardware.** No Pico, driver or motor has ever been attached to
  this project. Everything above was found and fixed in simulation; the bench
  procedures are instructions for the owner to run, not results anyone has
  observed. Nothing here can detect a stalled motor, and nothing can: there is
  no encoder, no home sensor, and the A4988 has no fault output in this design.
  The full-revolution stage of `bench.py check` and the `verified` flag are the
  whole defence.
* **Push 18** — the planner re-checked against the motors that were actually
  bought, as `tools/motion_check.py` so the numbers can be re-derived rather
  than taken on trust. Every figure is marked *datasheet*, *computed*, *bound*
  or *measured*; rotor inertia is published for neither motor, so it is bounded
  (the whole motor mass as a solid cylinder at the full frame radius) and every
  margin below is a floor.

  **Keep 800 microsteps/s and 1600 microsteps/s².** A 4.45 s full turn, a soft
  start — the first microstep lands 17.7 ms after the command and the first ten
  take 83 ms — and 0.226 mm of tip movement per microstep arriving at 800 Hz,
  which reads as continuous. Nothing in the motors argues for changing them.

  **The NEMA23s and the NEMA17s do not want different limits**, and the reason
  is the opposite of the intuition. Torque is irrelevant here: at the
  firmware's own *ceiling* the acceleration asks 3.2% of the NEMA23's holding
  torque and 2.2% of the NEMA17's, at the reduced 0.8 A the build guide
  recommends, against an inertia bound several times the real figure. The hand
  is 5 g. But per unit of its own inertia the NEMA23 is the *weaker* of the two
  — 3405 rad/s² against 4930 — so if the two types ever do want different
  limits, the big motors want the lower one. Neither is close enough for it to
  matter; a limit will be wanted for a tight tube or a stiff belt, on one
  channel, which is what per-channel `set_params` is for (and which now reports
  itself correctly, see push 17).

  Two things the arithmetic did turn up:

  * **`SPEED_LIMIT = 4000` is inside the plausible resonance band.** The
    rotor-detent pendulum puts the low-speed resonance at ≥0.46 rev/s for the
    NEMA23 and ≥0.56 rev/s for the NEMA17 — floors, because the inertia bound
    is high. The ceiling is 1.25 rev/s. 1/16 stepping suppresses most of it,
    which is what the bridged pins are for, but it is not a speed to go
    looking for. The default at 0.25 rev/s is clear by a factor of two or more.
  * **`main.py`'s "roughly a hundred times the headroom" was too generous.** It
    compared the 208 µs pulse interval against a bare loop iteration, not
    against `take_step`, which does a square root and a dozen float operations
    per step. Measured at 0.82 µs per pulse on CPython here; at 30–100× for
    MicroPython on a 125 MHz RP2040 that is 24–82 µs, so single-digit headroom
    at the default and none at all at the ceiling. The docstring's *conclusion*
    — software step loop, not PIO — still holds at 800 microsteps/s, and the
    docstring now says so with the right number. The factor is an estimate and
    says so; `late` in `status` is how to replace it with a fact.

  Six more tests pin the numbers the recommendation turns on, so a planner
  change that moves them fails rather than quietly invalidating this.

  **Untested on hardware.** Arithmetic about motors cannot find a rubbing tube
  or a tight bearing. `tools/bench.py check` and `late` are how the owner
  checks the three things this cannot: whether anything binds, whether the loop
  keeps up, and where the resonance actually is.
