# Display nodes

Six ESP32 boards, one per person, each driving a GC9A01 240×240 round display
beside that person's hand. They talk to nothing but MQTT — no wire to the Pi, no
wire to each other — so a dead node is one dark screen and nothing else.

**Build status:** this compiles (`pio run`, espressif32 6.5.0, 1 005 549 bytes
of a 1 310 720 byte app partition). It has **never run on hardware**: no ESP32
and no display were connected when it was written. See `PROGRESS.md`.

---

## What a node does

* joins WiFi and MQTT from `data/config.json` on LittleFS — nothing is compiled in;
* subscribes to `clock/display/<person>`, which the controller **retains**, so a
  node that boots an hour late immediately knows what to show;
* plays a looping animation from `data/loop.wcl`;
* draws an overlay: a ring filled on the same log distance scale as the clock's
  dial, the person's name, a status line (`HOME` or `N km away`) and
  "updated N min ago";
* publishes `clock/node/<person>/status` with RSSI, uptime, firmware version,
  free heap and whether the loop loaded;
* accepts OTA updates;
* brings up a setup access point and a captive portal when it has no usable
  WiFi configuration — the only way to reconfigure a node already inside a
  cabinet.

There is a small dot at the bottom of each screen: green when MQTT is
connected, amber when WiFi is up but the broker is not, red when neither.
Without it a node showing a stale position looks identical to one that is
working, which is the most confusing possible failure on a wall of six.

---

## Wiring

From `docs/circuit-diagram.html`, Diagram 4. Same on all six nodes.

| Display | ESP32 | Dupont colour |
|---|---|---|
| VCC | 3V3 | red |
| BLK (backlight) | 3V3 | red, short |
| GND | GND | black |
| SCL | GPIO18 | yellow |
| SDA | GPIO23 | blue |
| DC | GPIO2 | green |
| CS | GPIO5 | orange |
| RST | GPIO4 | purple |

**GPIO2 and GPIO5 are boot strapping pins** (GPIO2 must be low at boot, GPIO5
high). The display's inputs are high impedance, so this wiring boots normally —
but if an upload ever fails, unplug the DC wire from GPIO2 while flashing.

Each node is powered from the USB charging strip, which is a completely separate
5 V domain from the Pi and the motors.

---

## Flashing one

```bash
cd firmware/esp32
cp data/config.example.json data/config.json
$EDITOR data/config.json          # person, WiFi, MQTT; this is the only per-node file

python ../../tools/make_loop.py evan.mp4 data/loop.wcl --seconds 6 --fps 10

pio run                           # build
pio run -t upload                 # flash the firmware over USB
pio run -t uploadfs               # upload data/ to LittleFS
pio device monitor                # watch it come up
```

`data/config.json` and `data/*.wcl` are gitignored. Never commit a WiFi or MQTT
password.

After the first flash, later updates can go over the air:

```bash
pio run -t upload --upload-port weasley-evan.local
```

---

## Memory, which is the thing to watch

The 240×240 16-bit sprite the overlay is composed into is **115 200 bytes**,
allocated from the heap at boot. The build leaves roughly 258 KB of heap, so it
fits — but WiFi, MQTT and the JPEG decoder all take their share, and this has
not been measured on a real board.

If `createSprite` fails the firmware says `out of memory` on the panel and in
the serial log rather than rendering garbage. If that happens on real hardware,
the options in order of preference are: drop the sprite and draw the JPEG
straight to the panel with the overlay composited per-scanline; use a board with
PSRAM; or halve the colour depth.

---

## The loop file

`tools/make_loop.py` crops a video to a circle, scales it to 240×240, samples it
at about 10 fps and packs the JPEGs into one `.wcl` container. The format is
specified in `tools/loopfile.py`, read by `src/loopfile.h`, and checked byte for
byte by `controller/tests/test_loopfile.py` — if those three ever disagree, a
node boots and shows nothing, which is miserable to debug six boards at a time.

The budget is 1.3 MB, against a LittleFS partition of exactly 1 441 792 bytes.
The packer steps the JPEG quality down until it fits and tells you what it did.

Corners are masked to black because the panel is round: nobody ever sees them,
and flat black costs almost nothing to encode.

---

## Topics

| Topic | Direction | Payload |
|---|---|---|
| `clock/display/<person>` | in, retained | `name`, `status` (`home`/`away`/`stale`/`unknown`), `distance_km`, `fraction`, `updated_iso`, `text`, `color` |
| `clock/node/<person>/status` | out, retained | `person`, `fw`, `uptime_s`, `rssi`, `ip`, `heap`, `frames`, `loop_ok` |

`fraction` is `angle / 360` on the clock's log scale, so the ring and the hand
beside it always agree. The node recomputes it from `distance_km` if an older
controller omits it.

---

## Bring-up order, when the boards arrive

1. Flash one board with **no display attached** and watch the serial log. It
   should report the config it loaded and then complain that it cannot open
   `/loop.wcl` — that proves LittleFS and the config parser.
2. `pio run -t uploadfs`, reboot, confirm it reports the frame count.
3. Attach the display. Expect the loop playing with the overlay on top. If the
   image is mirrored or the colours are inverted, adjust `invert` and
   `rgb_order` in `src/display.h`.
4. Point it at the broker and publish a test payload from the Pi:
   ```bash
   mosquitto_pub -h localhost -u clock -P "$MQTT_PASSWORD" -r \
     -t clock/display/evan \
     -m '{"name":"Evan","status":"away","distance_km":7537,"fraction":0.9014,"text":"7537 km away"}'
   ```
   The ring should fill to 90% and the text should change.
5. Only then build the other five.
