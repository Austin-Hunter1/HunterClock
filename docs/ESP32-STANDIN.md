# Running the motors from an ESP32

The Pico on the bench was damaged when an A4988 failed (12 V reached its logic
pins).  Until a replacement arrives, an ESP32 can drive the six A4988s.  There
are two ways, and **both use the same wiring**.

| Build | Language | Use it when |
|---|---|---|
| `display-motors` | C++ (PlatformIO) | One of the six display-node ESP32s must also run the motors, because every board is needed for a screen. |
| MicroPython | `firmware/pico/main.py` | A spare ESP32 with no screen. |

Either way the Pi side is unchanged: same JSON protocol over serial
(`docs/PROTOCOL.md`), same `tools/bench.py`, same `clockd`.  Only the serial
device differs (`/dev/ttyUSB0` instead of `/dev/ttyACM0`).

Diagrams and a pre-power checklist: [esp32-motor-board.html](esp32-motor-board.html).

## Pin map (silkscreen labels on the 30-pin board)

| Channel | Person | STEP | DIR |
|---|---|---|---|
| 0 | Becky  | D25 | D26 |
| 1 | Evan   | D27 | D33 |
| 2 | Elijah | D32 | D13 |
| 3 | Caleb  | D14 | RX2 (GPIO16) |
| 4 | Austin | TX2 (GPIO17) | D19 |
| 5 | Micah  | D21 | D22 |
| all | ENABLE (active low) | D15 | |

Power and ground: ESP32 `3V3` to every driver's VDD (and the MS1-3, RESET,
SLEEP ties, as before); ESP32 `GND` to the common ground star point.  Do not
use `VIN`.

None of these is a display pin (the screen uses D2, D4, D5, D18, D23), a
strapping pin (D0, D2, D5, D12, D15 is used for ENABLE on purpose: it is high at
boot, which means "drivers released"), a flash pin, the console UART (TX0/RX0)
or an input-only pin (D34 to D39).

**Put a 330 ohm to 1 kohm resistor in series with every STEP, DIR and ENABLE
line**, close to the ESP32.  A failed driver can then not push real current
into the board.

`tools/bench.py` still prints Pico pin names in its channel labels; use the
table above on the ESP32.

## `display-motors` (C++, screen and motors on one board)

The display node and the motor firmware are one program.  The stepper loop runs
on core 0 as its own task; the display, MQTT and OTA stay on core 1, so drawing
a frame does not stall the step pulses.

**What to know**

* UART0 (the USB-serial port) carries the motor protocol, so this build prints
  **no log** to the serial monitor.  Use MQTT status or the screen to debug the
  display half.
* The board's own task watchdog resets it if the step loop hangs, and ENABLE
  goes high on reset, which releases the drivers.
* It reports `"board": "esp32"` in `hello`.
* **Written and compiled, never run on hardware.**  The step planner and the
  protocol are a line-for-line port of the Python ones, and
  `firmware/esp32/tests/test_port_matches_python.py` replays a session through
  both and compares every pulse and message.  That test needs a C++ compiler and
  has not been run yet; run it before trusting the port.

**Flash it** (on the PC or the Pi, with `pio` installed):

```bash
cd firmware/esp32
cp data/config.example.json data/config.json     # person, WiFi, MQTT
pio run -e display-motors -t upload
pio run -e display-motors -t uploadfs
```

Unplug the display's DC wire (GPIO2) while flashing if an upload fails; see the
display README.

**Test it** (clockd stopped, 12 V off for the first run):

```bash
sudo systemctl stop clockd
.venv/bin/python tools/bench.py --port /dev/ttyUSB0 info
```

Opening the port toggles DTR/RTS, which resets an ESP32.  Expect a reboot each
time a tool connects, and wait a couple of seconds before the first command.
`boot reason` reads power-on or reset; the controller re-verifies after any reset
anyway.

## MicroPython (spare ESP32, no screen)

Flash MicroPython once:

```bash
~/clock-env/bin/pip install esptool
# Download the ESP32_GENERIC .bin from https://micropython.org/download/ESP32_GENERIC/
~/clock-env/bin/esptool.py --chip esp32 --port /dev/ttyUSB0 erase_flash
~/clock-env/bin/esptool.py --chip esp32 --port /dev/ttyUSB0 --baud 460800 \
    write_flash -z 0x1000 ESP32_GENERIC-*.bin
```

If it cannot connect, hold the `BOOT` button while it says "Connecting...".
Then:

```bash
source ~/clock-env/bin/activate
PICO_PORT=/dev/ttyUSB0 make firmware-upload
```

After a reset the firmware leaves Ctrl-C working for 1.5 s (`BOOT_GRACE_MS`)
before it turns it off, so a reset followed at once by an upload gets in without
wiping the board.
