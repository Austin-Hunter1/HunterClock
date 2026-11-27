# ESP32 stand-in for the Pico

The Pico on the bench was damaged when an A4988 failed (12 V reached its logic
pins).  Until a replacement arrives, one of the display-node ESP32 dev boards
(30-pin, "Aideepen") can run the same motor firmware.  `firmware/pico/main.py`
picks its pin table from `sys.platform`, so the file is identical on both.

Everything else is unchanged: same JSON protocol over serial, same
`tools/bench.py`, same `clockd`.  Only the serial device differs
(`/dev/ttyUSB0` instead of `/dev/ttyACM0`).

## Pin map (silkscreen labels on the 30-pin board)

| Channel | Person | STEP | DIR |
|---|---|---|---|
| 0 | Becky  | D25 | D26 |
| 1 | Evan   | D27 | D33 |
| 2 | Elijah | D32 | D13 |
| 3 | Caleb  | D18 | D19 |
| 4 | Austin | D21 | D22 |
| 5 | Micah  | D23 | D4  |
| all | ENABLE (active low) | RX2 (GPIO 16) | |

Power and ground: ESP32 `3V3` to every driver's VDD (and the MS1-3, RESET,
SLEEP ties, as before); ESP32 `GND` to the common ground star point.  Do not
use `VIN`.  Power the board from the Pi over USB.

Avoid the strapping pins (D0, D2, D5, D12, D15), the console (TX0/RX0) and the
input-only pins (D34, D35, VP, VN).  `tools/bench.py` still prints Pico pin
names in its channel labels; use the table above on the ESP32.

**Put a 330 ohm to 1 kohm resistor in series with every STEP, DIR and ENABLE
line**, close to the ESP32.  A failed driver can then not push real current
into the board.

## Flash MicroPython (once)

On the Pi, with the ESP32 plugged in (it appears as `/dev/ttyUSB0`):

```bash
~/clock-env/bin/pip install esptool
# Download the ESP32_GENERIC .bin from https://micropython.org/download/ESP32_GENERIC/
~/clock-env/bin/esptool.py --chip esp32 --port /dev/ttyUSB0 erase_flash
~/clock-env/bin/esptool.py --chip esp32 --port /dev/ttyUSB0 --baud 460800 \
    write_flash -z 0x1000 ESP32_GENERIC-*.bin
```

If it cannot connect, hold the `BOOT` button while it says "Connecting...".

## Upload the firmware

```bash
source ~/clock-env/bin/activate
PICO_PORT=/dev/ttyUSB0 make firmware-upload
```

A freshly flashed board has no `main.py`, so the REPL is free.  Later, after a
reset the firmware leaves Ctrl-C working for `BOOT_GRACE_MS` (1.5 s) before it
turns it off, so a reset followed at once by an upload gets in without wiping
the board.

## Talk to it

```bash
sudo systemctl stop clockd
.venv/bin/python tools/bench.py --port /dev/ttyUSB0 info
```

Opening the port toggles DTR/RTS, which resets an ESP32.  Expect a reboot each
time a tool connects, and `boot reason` to read as a reset or power-on.  That
is harmless on the bench; the controller re-verifies after any reset anyway.
