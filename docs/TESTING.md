# Testing

Everything here runs with no hardware attached, on Linux, macOS or Windows.

---

## Quick start

```bash
git clone https://github.com/Austin-Hunter1/HunterClock
cd HunterClock
make venv      # creates .venv and installs the controller
make test      # 470 tests, about a minute
make lint      # ruff across the whole repo
make sim       # the UI at http://127.0.0.1:8080
```

### Windows

`make` is not available, so run the same things directly. Everything the
controller does in simulation avoids Linux-only modules and uses `pathlib`, so
this genuinely works:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e "controller[dev]"

cd controller
..\.venv\Scripts\python -m pytest -q

cd ..
.venv\Scripts\python -m clockd.cli --sim --sim-fixtures
```

Then open `http://127.0.0.1:8080`. There is no login in sim mode unless you set
`CLOCK_UI_PASSWORD` or a `password_hash`.

---

## What `make sim` gives you

A fake Pico running the real firmware modules, an in-memory MQTT broker, and
six hands already zeroed and pointing at the spec's reference positions:

```
clockd 0.1.0 starting
  mode     : SIMULATION (no hardware)
  config   : 6 people, home Minnetonka, MN
  pico     : in-process simulator
  mqtt     : in-memory
  web UI   : http://127.0.0.1:8080
```

Useful flags:

| Flag | Effect |
|---|---|
| `--sim-fixtures` | zero the hands and publish reference positions at startup |
| `--sim-time-scale 10` | run simulated time 10× faster, so a sweep finishes quickly |
| `--mqtt-host localhost` | use a real Mosquitto instead of the in-memory broker |
| `--port 8081` | serve the UI somewhere else |
| `--no-web` | engine only |

### Things worth trying in the UI

1. **Dashboard** — six hands on the dial at their real angles.
2. **Simulate → scenarios → "Evan flies home"** over 60 s. Watch his hand sweep
   from 324° down to 12 o'clock. It crosses 12 at the end, which is the move
   most likely to expose a shortest-path or direction bug.
3. **Simulate → fake distance** — set Micah to 10 km and watch the hand jump to
   83.7°, one decade along the log scale.
4. **Control → Test sweep** — one hand, a full turn and back.
5. **Control → E-stop** — every hand goes `unverified`, as it would after a real
   one, and the engine stops driving them from location data.
6. **Assist** — the zeroing wizard, with only the hand being worked on drawn.
7. **Logs** — every action above, timestamped.

### Feeding it a location by hand

With `--mqtt-host localhost` and Mosquitto running:

```bash
mosquitto_pub -h localhost -t clock/location/austin \
  -m '{"lat":40.0150,"lon":-105.2705,"acc":30}'

mosquitto_pub -h localhost -t owntracks/austin/iphone \
  -m '{"_type":"location","lat":40.0150,"lon":-105.2705,"acc":30,"tst":1762204800}'
```

Austin's hand should move to 254.8°, and `clock/state/austin` should update:

```bash
mosquitto_sub -h localhost -t 'clock/#' -v
```

---

## The test suite

```bash
make test        # everything
make test-fast   # unit tests only; skips integration and API (~5 s)
```

Or directly, for one file or one test:

```bash
cd controller
python -m pytest tests/test_motion.py -v
python -m pytest -k "shortest_path"
python -m pytest tests/test_integration.py -x
```

| File | Tests | Covers |
|---|---|---|
| `test_geo.py` | 78 | the mapping vectors from the spec, shortest path, wrapping |
| `test_motion.py` | 38 | the planner: exact arrival, speed and accel limits, six channels at once |
| `test_protocol.py` | 72 | framing, validation, every error code |
| `test_config.py` | 42 | schema, duplicate levels, invert/offset arithmetic, no secrets in the example |
| `test_sources.py` | 55 | each parser against realistic payloads |
| `test_filters_state.py` | 56 | acceptance, deadband, status precedence, the store |
| `test_bus.py` | 23 | topic matching, retain, wildcards, Last Will |
| `test_eventlog.py` | 22 | filtering, bounds, file output |
| `test_integration.py` | 23 | the controller against the simulated Pico |
| `test_api.py` | 61 | auth, every page, every action endpoint, config save, diagnostics |

### The ones that would catch a real mistake

* `test_geo.py::test_spec_vectors` — all six people from the spec table, within
  1 km and 0.1°. If the mapping ever drifts from `birthday_clock.m`, this fails.
* `test_motion.py::test_move_lands_exactly_on_target` — the planner never
  overshoots or stops one microstep short, at any distance.
* `test_integration.py::test_the_commanded_microsteps_match_the_angle` — a fix
  for Boulder produces **−935 microsteps**, going 105° backwards rather than
  255° forwards.
* `test_integration.py::test_an_unverified_hand_is_not_driven` — the hand knows
  where it should go and deliberately does not go there.
* `test_integration.py::test_a_pico_reset_marks_every_hand_unverified`
* `test_api.py::test_the_diagnostic_bundle_carries_no_secrets` — a real
  `MQTT_PASSWORD` and a password hash are both absent from the zip, while the
  variable *names* are still reported.

---

## Screenshots

```bash
make sim                      # one terminal
node tools/ui_screenshots.js  # another
```

Renders every page at 1280 px and 390 px into `docs/img/ui/`, and **exits
non-zero if any page logs a JavaScript error or scrolls sideways**. The pictures
are a by-product; the check is the point.

---

## Testing against real hardware

Nothing below has been run — see the "untested on hardware" section of
`PROGRESS.md`. This is the order to do it in.

### 1. The Pico, with no motors

```bash
pip install mpremote
make firmware-upload PICO_PORT=/dev/ttyACM0
```

Then talk to it by hand, with the controller stopped:

```bash
mpremote connect /dev/ttyACM0 repl
```

Or drive it from a script and watch the JSON:

```bash
python - <<'PY'
import serial, json, time
pico = serial.Serial("/dev/ttyACM0", 115200, timeout=1)
pico.write(json.dumps({"seq": 1, "cmd": "hello"}).encode() + b"\n")
time.sleep(0.5)
print(pico.read(pico.in_waiting).decode())
PY
```

Expect a `boot` event and a `hello` reply with `fw`, `proto` and
`needs_verification: true`.

### 2. One motor

Per `docs/circuit-diagram.html`: set Vref with the motor disconnected, then
power down, connect one motor, power up.

```bash
sudo systemctl start clockd
```

In the UI: **Control → enable drivers → jog Becky +10°**. Her motor (level 1,
STEP GP2) should turn a tenth of a revolution. If it turns the wrong way, set
`invert: true` for her in `config/clock.yaml` rather than rewiring.

Then **Test sweep** on that one hand: a full turn and back at 200 steps/s.

### 3. All six

Add motors one at a time, powered down between each. Then **Move all to 12**,
and watch for the one failure mode that matters on a shared ENABLE line: all six
should hold position when idle unless `idle_disable` is on.

### 4. The whole pipeline

Install OwnTracks on a phone, point it at the broker, and walk outside. The
hand should move within a minute or two, and the Logs page should show the fix
arriving, being accepted, and the move being commanded.

---

## Continuous integration

`make check` is lint plus tests, and is what a CI job should run. The existing
`.github/workflows/build-cad.yml` is for the CAD model and is unrelated.
