# Pi ↔ Pico serial protocol

The controller on the Raspberry Pi and the firmware on the Pico talk over USB
serial (`/dev/ttyACM0`, 115200 baud, 8N1) in newline-delimited JSON.

This document and the code are kept in step by construction: `firmware/pico/protocol.py`
is the only implementation, and the firmware, `tools/pico_sim.py` and the
controller all import it. There is no second copy to drift.

---

## Framing

One JSON object per line, UTF-8, terminated by `\n`. Lines are at most 512 bytes
in each direction; the firmware drops an over-long line rather than buffering it
forever, which is what keeps a wedged host from exhausting the Pico's RAM.

There are three kinds of message.

### Command — Pi to Pico

```json
{"seq": 7, "cmd": "move", "ch": 2, "target": 1600}
```

`seq` is a positive integer chosen by the Pi and increasing within a connection.
`cmd` is one of the verbs below.

### Reply — Pico to Pi

Exactly one per command, echoing `seq` and `cmd`:

```json
{"seq": 7, "cmd": "move", "ok": true, "ch": 2, "pos": 420, "target": 1600}
{"seq": 7, "cmd": "move", "ok": false, "err": "bad_channel", "msg": "channel 9 out of range"}
```

### Event — Pico to Pi, unsolicited

No `seq`; carries `ev`:

```json
{"ev": "pos", "t": 1534219, "pos": [0,0,0,0,0,0], "moving": [false,false,false,false,false,false]}
```

A reply always has both `seq` and `ok`; an event never has `seq`. That is how
the receiver tells them apart, and it is why no event will ever gain a `seq`
field.

Unknown keys are ignored in both directions, so a newer Pi can talk to an older
Pico. Unknown *commands* are an error, because silently accepting one would let
the Pi believe a hand had moved when it had not.

---

## Units and conventions

| Thing | Unit |
|---|---|
| Position, target, delta | microsteps, signed, **unwrapped** |
| Speed | microsteps per second |
| Acceleration | microsteps per second squared |
| Times in replies and events (`t`, `uptime_ms`) | milliseconds since the Pico booted |
| `watchdog_s` | seconds |

**3200 microsteps is one full revolution** of a hand: 200 full steps at the
A4988's 1/16 microstepping, and every hand is mounted directly on its motor
shaft with no gearing (`docs/hardware-plan.html`).

Positions are **unwrapped**: there are no end stops and no cable wrap, so a hand
that has turned forward four times sits at `12800`, not `0`. The Pico never
normalises a position, and the controller relies on that to compute shortest-path
moves. Choosing the short way round is the **controller's** job; the Pico simply
goes where it is told.

Channels are `0`–`5` and map to hub levels 1–6 — see the pin table in
`firmware/pico/main.py` and `docs/circuit-diagram.html`.

---

## Commands

### `hello` / `info`

Identify the firmware. `hello` is sent on every connection.

```json
{"seq": 1, "cmd": "hello"}
```

Reply fields: `fw`, `proto`, `boot_reason`, `uptime_ms`, `channels`, `enabled`,
`needs_verification`, `pos`, plus everything `set_params` reports.

`needs_verification` is `true` after any boot. It means "my positions were
restored from flash, not confirmed by a person" — see
[Verification](#verification-and-why-it-matters).

### `status`

Live motion state: `pos`, `target`, `moving`, `vel`, `enabled`,
`needs_verification`, `uptime_ms`, `steps`, `errors`, `watchdog_tripped`.

### `move`

```json
{"seq": 8, "cmd": "move", "ch": 0, "target": -935}
```

Absolute target in microsteps. Fails with `not_enabled` if the drivers are off,
because a move with ENABLE high would silently do nothing.

Absolute, not relative, so **a retry after a timeout is safe** — this is why the
controller may retry any command.

### `jog`

```json
{"seq": 9, "cmd": "jog", "ch": 0, "delta": -89}
```

Relative to the current *target*, not the current position, so two quick jogs
add up rather than racing.

### `set_pos`

```json
{"seq": 10, "cmd": "set_pos", "ch": 3, "pos": 0}
```

Declare where the hand is without moving it. This is how zeroing works. Fails
with `busy` if that channel is moving — stop it first.

### `enable`

```json
{"seq": 11, "cmd": "enable", "on": true}
```

Drives the shared ENABLE line on GP14, which is **active low**: `on: true`
writes 0. Off releases all six drivers, so the hands lose holding torque and can
be turned by hand.

### `stop`

With `ch`, decelerates one channel; without, all six. Uses the configured
acceleration, so the hand stops without slipping and the position stays true.

### `estop`

Immediate halt, no deceleration. Sets `needs_verification`, because stopping at
speed can slip the rotor and the reported position is then a guess.

### `set_params`

Any subset of `max_speed`, `accel`, `idle_disable`, `watchdog_s`,
`pos_interval_ms`, optionally scoped to one `ch`. Values are clamped to
`SPEED_LIMIT` (4000 microsteps/s) and `ACCEL_LIMIT` (20000 microsteps/s²) in
`firmware/pico/motion.py` — the firmware's own ceiling, below which the
controller applies its lower configured one.

### `ping`

Feeds the watchdog. Reply carries `t` (uptime in ms).

---

## Events

### `boot`

```json
{"ev": "boot", "fw": "0.1.0", "proto": 1, "reason": "power-on",
 "channels": 6, "needs_verification": true, "pos": [0,0,0,0,0,0]}
```

Sent once at startup. `reason` is one of `power-on`, `reset`, `watchdog-reset`,
`soft-reset`, `unknown`, from `machine.reset_cause()`.

**A `boot` event arriving mid-session means the Pico restarted underneath the
controller.** The controller abandons every in-flight command and marks every
hand unverified. It does not quietly carry on.

### `pos`

```json
{"ev": "pos", "t": 1534219, "pos": [0,-935,0,0,0,0], "moving": [false,true,false,false,false,false]}
```

Throttled to `pos_interval_ms` (default 200 ms) while moving. At rest it is sent
only when a position actually changed, so an idle clock is silent on the wire.

### `fault`

```json
{"ev": "fault", "code": "watchdog", "msg": "no ping for 10.1 s; decelerating"}
```

Codes: `watchdog`, `protocol`, `internal`.

### `log`

Free-text diagnostics. The controller shows these on the Logs page and does
nothing else with them.

---

## Error codes

| Code | Meaning |
|---|---|
| `bad_json` | Not valid JSON, or not valid UTF-8 |
| `not_object` | Valid JSON, but not an object |
| `no_seq` / `bad_seq` | `seq` missing, or not a positive integer |
| `no_cmd` / `unknown_cmd` | `cmd` missing, not a string, or unrecognised |
| `bad_channel` | `ch` outside 0–5 |
| `missing_arg` / `bad_arg` | A required argument is absent or the wrong type |
| `busy` | The channel is moving and the command needs it at rest |
| `not_enabled` | The drivers are off, so the move would do nothing |
| `internal` | A bug in the firmware; the message carries the exception |

A malformed line never kills the reader: the firmware replies with an error (or
with `seq: null` when `seq` could not be read) and carries on.

---

## The watchdog

If the Pico sees no `ping` for `watchdog_s` (default 10 s) it decelerates every
hand to a stop and emits a `fault`. Stored positions are kept, so nothing is
lost — the hands simply stop where they are until the Pi comes back.

This is the behaviour that matters when the Pi reboots, the USB cable is
knocked, or the controller crashes: the hands stop in a known place rather than
running until something jams.

The controller pings every `serial.ping_interval_s` (default 3 s), which gives
three missed pings of margin.

Separately, `main.py` starts the RP2040's own hardware watchdog at 8 s, which
catches the firmware itself hanging.

---

## Verification, and why it matters

There are no home sensors anywhere in this clock. After a power cut, the Pico
restores its positions from flash — but a hand that was nudged while the power
was off, or a motor that slipped, makes that number a guess, and the Pico cannot
tell the difference.

So:

* every boot sets `needs_verification`;
* the controller marks every hand unverified on a `boot` event;
* **an unverified hand is never driven from location data**;
* the only thing that clears it is a person at the clock going through
  `/assist`, lining the hand up with 12 and tapping the button, which sends
  `set_pos`.

Refusing to move is the deliberate choice. Driving to a computed angle from a
guessed origin produces a clock that looks like it is working and is wrong by an
unknown amount — much worse to diagnose from a thousand miles away than a hand
that has visibly not moved and a UI that says why.

---

## Worked exchange

Controller connects, zeroes one hand, and sends it to Boulder's angle.

```
<- {"ev":"boot","fw":"0.1.0","proto":1,"reason":"power-on","channels":6,
    "needs_verification":true,"pos":[0,0,0,0,0,0]}
-> {"seq":1,"cmd":"hello"}
<- {"seq":1,"cmd":"hello","ok":true,"fw":"0.1.0","proto":1,"boot_reason":"power-on",
    "uptime_ms":312,"channels":6,"enabled":false,"needs_verification":true,
    "pos":[0,0,0,0,0,0],"max_speed":800.0,"accel":1600.0,"idle_disable":false,
    "watchdog_s":10.0,"pos_interval_ms":200,"speed_limit":4000.0,"accel_limit":20000.0}
-> {"seq":2,"cmd":"enable","on":true}
<- {"seq":2,"cmd":"enable","ok":true,"enabled":true}
-> {"seq":3,"cmd":"set_pos","ch":4,"pos":0}          # person at the clock said "this is 12"
<- {"seq":3,"cmd":"set_pos","ok":true,"ch":4,"pos":0}
-> {"seq":4,"cmd":"move","ch":4,"target":-935}       # 254.8 deg, the short way round
<- {"seq":4,"cmd":"move","ok":true,"ch":4,"pos":0,"target":-935}
<- {"ev":"pos","t":1200,"pos":[0,0,0,0,-64,0],"moving":[false,false,false,false,true,false]}
<- {"ev":"pos","t":1400,"pos":[0,0,0,0,-192,0],"moving":[false,false,false,false,true,false]}
...
<- {"ev":"pos","t":5300,"pos":[0,0,0,0,-935,0],"moving":[false,false,false,false,false,false]}
-> {"seq":5,"cmd":"ping"}
<- {"seq":5,"cmd":"ping","ok":true,"t":5400}
```

Austin is 1107 km from home, which is 254.8° clockwise from 12. The hand is at
0°, so the shortest path is **105.2° backwards**, which is −935 microsteps —
not the 2265 microsteps that going forward would take.

---

## Testing against this document

`controller/tests/test_protocol.py` covers the framing and every error code.
`controller/tests/test_integration.py` drives the whole exchange above against
`tools/pico_sim.py`. If you change the protocol, those tests and this file both
have to change, and the simulator gets the change for free because it imports
the same module the Pico runs.
