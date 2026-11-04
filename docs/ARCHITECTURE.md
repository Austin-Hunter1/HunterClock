# Architecture

How a phone's location becomes a hand pointing somewhere on a dial, and why the
pieces are split the way they are.

```
phones (OwnTracks)  ──MQTT──┐
Home Assistant      ──REST──┤
UI overrides        ────────┴──▶  clockd (asyncio, on the Pi)
                                    │
                                    ├── USB serial, JSON lines ──▶ Pico ──▶ 6× A4988 ──▶ 6 steppers
                                    ├── MQTT, retained ──────────▶ 6× ESP32 display nodes
                                    └── HTTP + WebSocket ────────▶ web UI (over Tailscale)
```

---

## The one idea everything else serves

**No hand is driven from a position nobody has confirmed.**

There are no home sensors, no encoders and no end stops in this clock. The only
thing that makes a hand's angle *knowable* is a person standing at the clock
saying "this hand is pointing at 12 now". Everything downstream of that is
dead reckoning.

So the system tracks a `verified` flag per hand, persists it, clears it whenever
anything could have invalidated it (a Pico reboot, an e-stop), and refuses to
drive an unverified hand from location data. The UI says which hands are
unverified, and `/assist` is the path back.

The alternative — assume the stored position is right and drive to the computed
angle — gives you a clock that looks like it is working and is wrong by an
unknown amount. From Boulder, with the clock in Minnesota, that is the worst
possible failure.

---

## Why the work is split across three processors

| | Runs | Does |
|---|---|---|
| **Raspberry Pi 4** | `clockd`, Python 3.11, asyncio | haversine, filtering, state, MQTT, the web UI |
| **Pico (RP2040)** | MicroPython | every STEP pulse, acceleration, the motion watchdog |
| **6× ESP32** | Arduino | one round display each, MQTT only |

Six simultaneous step streams need jitter-free timing that a Linux userspace
process, also serving HTTP and MQTT, cannot promise. Offloading to the Pico
means a GC pause on the Pi cannot stutter a motor.

It also separates failure domains. A Pico crash stops the hands but leaves the
displays and the location pipeline running. A WiFi hiccup stalls one display,
not the motors. A controller restart leaves the hands exactly where they were,
because the Pico holds position and stops them itself if the Pi stays away.

---

## Why a software step loop, not PIO

`docs/hardware-plan.html` sketched one PIO state machine per motor. The firmware
pulses from the main loop against per-axis microsecond deadlines instead.

The numbers decide it. Six hands at the default 800 microsteps/s is 4800
pulses/s in total — one every ~208 µs — while a loop iteration on a 125 MHz
RP2040 costs a couple of microseconds. There is roughly a hundred times the
headroom needed. In exchange, the entire real-time path is ordinary Python that
pytest runs on a laptop, which is what makes `tools/pico_sim.py` worth trusting.

Acceleration also works against PIO here: every step interval differs along a
ramp, so a PIO program would need the CPU to feed it a fresh delay per step
anyway, and most of the complexity would buy nothing.

If a later version wants much higher speeds, `motion.MotionController` already
separates *when is the next step due* from *make a pulse*, so a PIO backend
replaces `Hardware.pulse` alone. See `firmware/pico/main.py`.

---

## One planner, three homes

`firmware/pico/motion.py`, `protocol.py` and `core.py` import nothing but
`math` and `json`. That is deliberate, and it is the reason simulation means
anything here:

* the **Pico** runs them under MicroPython;
* **pytest** runs them under CPython (`controller/tests/test_motion.py`);
* **`tools/pico_sim.py`** runs them with the pins swapped for counters, and the
  controller talks to that in `--sim` mode.

So `--sim` is not a mock of the firmware. It *is* the firmware, minus the GPIO
layer. If the simulator accepts a command, the Pico's parser accepted it; if a
move takes 4.4 seconds in simulation, the planner produced that profile.

`controller/clockd/picolib.py` locates those modules (they live in the firmware
tree because that is what gets copied onto the Pico, where files sit flat in the
filesystem root and cannot be a package). `WEASLEY_PICO_LIB` overrides the path
if a deployment ever needs it.

---

## Angles, steps and the shortest path

The mapping is fixed by `birthday_clock.m` and lives in `clockd/geo.py`:

```
d     = max(haversine(home, person), 1.0)      # km, R = 6371.0
angle = log10(d) / log10(pi * 6371.0) * 360.0  # degrees clockwise from 12
```

A log scale is the whole trick: the dial gives as much room to the first 10 km
as to the last 10 000, so a hand moves visibly when someone drives across town
*and* when they fly to Poland. Each decade of distance is 83.7°. One full turn
is the antipode, 20 015 km.

Positions are tracked **unwrapped** all the way from `geo.shortest_path_target`
through the protocol to `motion.Axis.position`. Nothing is ever reduced modulo
360, because the hands have no cable wrap: going from 324° to 2° is `+38°`, a
short forward move, and reducing either number first would lose that.

The conversion to microsteps happens once, in `ClockConfig.angle_to_steps`,
which also applies the per-person `invert` and `zero_offset_deg`. `invert`
exists because a motor wired with its coil pair reversed turns the wrong way,
and flipping a flag is cheaper than a soldering iron.

---

## The controller, module by module

| Module | Responsibility |
|---|---|
| `geo.py` | the mapping and the shortest-path helpers. No dependencies, so it cannot drift |
| `config.py` | the pydantic schema, YAML loading, angle↔step conversion |
| `state.py` | `Fix`, `Override`, `PersonState`, and the status precedence |
| `filters.py` | which fixes to believe; whether a change is worth a motor move |
| `store.py` | zero offsets, verified flags and last fixes, atomically on disk |
| `bus.py` | MQTT, with an in-memory broker that implements retain and wildcards |
| `picolink.py` | framing, `seq` matching, timeouts, retries, reconnect, reset detection |
| `sources/` | OwnTracks, generic MQTT, Home Assistant — parsing kept pure and separate |
| `engine.py` | ties it together; everything the UI does, it does through here |
| `web/` | FastAPI, Jinja2 templates, vendored JS |
| `scenarios.py` | scripted playback for the Simulate page |

The **engine is the only thing that moves a hand.** The web layer never touches
the link directly, which is why every hardware action lands in the event log
with a timestamp and why the API tests exercise the same path the UI does.

---

## Two filters, deliberately separate

**Is this fix believable?** (`accept_fix`) Reject accuracy worse than 2 km, fixes
older than the last accepted one, fixes older than a day, and exact duplicates.
A 5 km cell-tower fix would swing a hand across the dial for nothing; a queue of
day-old points flushed when a phone leaves airplane mode would replay a journey
backwards.

**Is this change worth moving for?** (`should_move`) A deadband of 0.5° and a
minimum interval of 5 s. Every move on an open-loop mechanism is a chance to
lose a step, and six motors twitching every thirty seconds would be both noisy
and pointless. The UI's own buttons pass `force=True` — a person asking directly
is not noise.

---

## Status precedence

A hand can be several things at once. `PersonState.status` reduces that to one
headline value, in this order:

1. `disabled` — switched off in config
2. `fault` — cannot be driven (no Pico link, or the firmware reported a fault)
3. `unverified` — nobody has confirmed where it points
4. `override` — a manual fake position is in force
5. `stale` — no fix for 6 hours; the hand holds its last position
6. `ok`

`unverified` outranks `override` and `stale` because it is the one condition
that needs a person *at the clock* to clear. The booleans are all kept
alongside, so the UI can show "stale and unverified" when that is the truth.

The display nodes get a deliberately smaller vocabulary — `home`, `away`,
`stale`, `unknown` — because a 240×240 round screen beside a hand has room for
one word, and "unverified" is the owner's problem, not something to put on a
portrait.

---

## MQTT topics

| Topic | Retained | Payload |
|---|---|---|
| `clock/state/<person>` | yes | `distance_km`, `target_deg`, `actual_deg`, `status`, `last_seen`, `source`, `accuracy_m`, `verified`, `moving`, `level` |
| `clock/display/<person>` | yes | `name`, `status`, `distance_km`, `fraction`, `updated_iso`, `text`, `color` |
| `clock/system/status` | yes | `online`, `sim`, `version`, `uptime_s`, `pico`, with a **Last Will** publishing `online: false` |
| `clock/system/heartbeat` | no | `at`, `uptime_s`, every 5 s |
| `owntracks/<user>/<device>` | — | subscribed |
| `clock/location/<person>` | — | subscribed |

Retained state is what lets a display node that boots an hour later immediately
know what to show, instead of a blank screen until someone moves.

---

## Simulation is a first-class mode

`clockd --sim` swaps three things and nothing else:

* `SimTransport` + `SimPico` instead of `SerialTransport` — in-process, no port;
* `MemoryBus` instead of `MqttBus` — same retain and wildcard semantics;
* `--sim-fixtures` zeroes the hands and publishes the reference positions.

The engine, the filters, the state machine, the UI and the protocol are
identical. It runs on Windows and Linux because the sim paths avoid Linux-only
modules and use `pathlib` throughout.

This is what makes the UI reviewable in Boulder before the hardware exists, and
it is what the integration tests drive.

---

## Remote operation

The clock is in another state and nobody technical is near it. That shapes a lot
of the design:

* **Tailscale** is the only way in. Joined before the Pi travels; key expiry
  disabled, or the clock drops off the tailnet in six months.
* **The UI works with the Pico disconnected** and says so plainly, because that
  is exactly when you need it.
* **The diagnostic bundle** is one click: logs, versions, sanitised config,
  recent MQTT. It is built to be *sent* to someone, so secrets are stripped by
  key name and the environment is reported as names only.
* **Update and rollback** are buttons. The commit before an update is recorded,
  so rollback has somewhere to go.
* **`/assist`** assumes the person reading it has never seen the project.
* **Two watchdogs**: the firmware stops the hands if the Pi goes quiet; systemd
  pets the Pi's hardware watchdog so a kernel lockup reboots the board.

---

## What is deliberately not here

* **No database.** Six people and a handful of flags fit in one JSON file.
* **No build step in the UI.** No Node, no bundler, no CDN. A UI that fetches a
  framework from the internet fails exactly when the network is the problem.
* **No authentication beyond one shared password.** It is one family behind a
  tailnet. The password is PBKDF2-hashed and the controller refuses to start in
  live mode without one.
* **No automatic re-zeroing.** There is no sensor that could justify it.
