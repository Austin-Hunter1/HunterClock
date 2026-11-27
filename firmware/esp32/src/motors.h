// Six A4988 stepper drivers on the same ESP32 that runs a display node.
//
// This is the board glue for motor_core.h / motion_core.h: pins, the serial
// port, the position file and the task that owns all of it.  Built only with
// -DWITH_MOTORS (the `display-motors` environment in platformio.ini).
//
// Why a task on core 0
// --------------------
// Stepping needs a pulse every ~1.25 ms at the default speed, and drawing a
// frame blocks for tens of ms.  The display and MQTT run in loop() on core 1,
// so the step loop gets core 0 to itself, apart from the WiFi stack, which
// preempts it for a few hundred microseconds at a time.  The planner already
// copes with that: a step that is late by more than one interval re-bases the
// schedule and counts in `late` (see MAX_CATCHUP_INTERVALS), it never bunches.
//
// The motor protocol owns UART0, the same USB-serial port the display node
// uses for its log, so the node's own log is silenced in this build.
//
// Wiring (silkscreen labels; every STEP/DIR/ENABLE line through ~330 ohm):
//
//   channel  person   STEP  DIR
//   0        Becky    D25   D26
//   1        Evan     D27   D33
//   2        Elijah   D32   D13
//   3        Caleb    D14   RX2 (16)
//   4        Austin   TX2 (17)   D19
//   5        Micah    D21   D22
//   ENABLE (all six, active low)   D15
//
// Not used: D2, D4, D5, D18, D23 (the display), D0, D12 (strapping), D1, D3
// (console), D6 to D11 (flash), D34 to D39 (input only).

#pragma once

#include <Arduino.h>
#include <LittleFS.h>
#include <esp_log.h>
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <esp_timer.h>

#include "motor_core.h"

namespace motors {

static const int CHANNELS = 6;
static const uint8_t STEP_PINS[CHANNELS] = {25, 27, 32, 14, 17, 21};
static const uint8_t DIR_PINS[CHANNELS] = {26, 33, 13, 16, 19, 22};
static const uint8_t ENABLE_PIN = 15;  // high at boot: drivers released

// The A4988 needs 1 us; its datasheet asks for 5.
static const int PULSE_US = 5;
static const char POSITION_FILE[] = "/positions.json";
static const char POSITION_TMP[] = "/positions.tmp";
static const size_t MAX_LINE = 512;

class Esp32Board : public mc::Board {
 public:
  uint32_t write_failures = 0;
  const char* source = "none";

  Esp32Board() {
    for (int i = 0; i < CHANNELS; i++) last_dir_[i] = -1;
  }

  void pulse(int ch, int direction) override {
    const int want = direction > 0 ? 1 : 0;
    if (last_dir_[ch] != want) {
      digitalWrite(DIR_PINS[ch], want);
      last_dir_[ch] = want;
      delayMicroseconds(1);  // DIR setup time is 200 ns
    }
    digitalWrite(STEP_PINS[ch], HIGH);
    delayMicroseconds(PULSE_US);
    digitalWrite(STEP_PINS[ch], LOW);
  }

  void setEnable(bool on) override { digitalWrite(ENABLE_PIN, on ? LOW : HIGH); }

  void send(const char* line) override {
    char buf[1100];
    size_t n = strnlen(line, sizeof buf - 2);
    memcpy(buf, line, n);
    buf[n++] = '\n';
    Serial.write((const uint8_t*)buf, n);  // one write, so lines never interleave
  }

  const char* positionSource() override { return source; }
  uint32_t flashErrors() override { return write_failures; }

  const char* bootReason() override {
    switch (esp_reset_reason()) {
      case ESP_RST_POWERON: return "power-on";
      case ESP_RST_EXT: return "reset";
      case ESP_RST_SW: return "soft-reset";
      case ESP_RST_PANIC:
      case ESP_RST_INT_WDT:
      case ESP_RST_TASK_WDT:
      case ESP_RST_WDT: return "watchdog-reset";
      default: return "unknown";
    }
  }

  // Positions survive a power cut: the file is never overwritten in place.  A
  // new copy is written beside it and swapped in, and load() reads whichever
  // survived.  (Same scheme as FileStore in firmware/pico/main.py.)
  bool loadPositions(int32_t* out, int n) override {
    int found = readFile(POSITION_FILE, out, n);
    if (found == 1) { source = "flash"; return true; }
    int tmp = readFile(POSITION_TMP, out, n);
    if (tmp == 1) { source = "recovered"; return true; }
    source = (found == -1 || tmp == -1) ? "corrupt" : "none";
    return false;
  }

  void savePositions(const int32_t* pos, int n) override {
    char buf[160];
    int len = snprintf(buf, sizeof buf, "{\"pos\":[");
    for (int i = 0; i < n; i++)
      len += snprintf(buf + len, sizeof buf - len, "%s%ld", i ? "," : "", (long)pos[i]);
    len += snprintf(buf + len, sizeof buf - len, "]}");

    File f = LittleFS.open(POSITION_TMP, "w");
    if (!f) { write_failures++; return; }
    size_t wrote = f.write((const uint8_t*)buf, len);
    f.close();
    // Read back before swapping, so a write that kept nothing cannot destroy
    // the good copy.
    int32_t check[mc::MAX_AXES];
    if (wrote != (size_t)len || readFile(POSITION_TMP, check, n) != 1) {
      write_failures++;
      return;
    }
    LittleFS.remove(POSITION_FILE);
    if (!LittleFS.rename(POSITION_TMP, POSITION_FILE)) write_failures++;
    else source = "flash";
  }

 private:
  int last_dir_[CHANNELS];

  // 1 = read n positions, 0 = no such file, -1 = something there, unreadable.
  static int readFile(const char* path, int32_t* out, int n) {
    if (!LittleFS.exists(path)) return 0;
    File f = LittleFS.open(path, "r");
    if (!f) return -1;
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, f);
    f.close();
    if (err) return -1;
    JsonArrayConst a = doc["pos"];
    if (a.isNull() || (int)a.size() < n) return -1;
    for (int i = 0; i < n; i++) out[i] = a[i].as<long>();
    return 1;
  }
};

static Esp32Board board;
static mc::MotorCore* core = nullptr;

static void motorTask(void*) {
  // The step loop busy-waits while a hand moves, so the idle task on this core
  // never runs and its own watchdog would reset the board.  Replace it with
  // one on this task: if the loop itself ever hangs, the board resets and
  // ENABLE goes high.
  disableCore0WDT();
  esp_task_wdt_add(NULL);

  core->start(esp_timer_get_time());

  char line[MAX_LINE + 1];
  size_t len = 0;
  for (;;) {
    core->tick(esp_timer_get_time());

    // Bounded, so a host that floods the port cannot starve the step loop.
    int budget = 256;
    while (budget-- > 0 && Serial.available()) {
      int c = Serial.read();
      if (c < 0) break;
      if (c == '\n') {
        if (len) core->handleLine(line, len, esp_timer_get_time());
        len = 0;
      } else if (len < MAX_LINE) {
        line[len++] = (char)c;
      } else {
        len = 0;  // over-long line: drop it rather than grow
      }
    }

    esp_task_wdt_reset();
    if (!core->mc.anyMoving()) vTaskDelay(1);  // idle: do not spin the core
  }
}

// Call first thing in setup().  Starts the serial port and the motor task.
inline void begin() {
  // Park every output before anything else: drivers released, STEP and DIR low.
  pinMode(ENABLE_PIN, OUTPUT);
  digitalWrite(ENABLE_PIN, HIGH);
  for (int i = 0; i < CHANNELS; i++) {
    pinMode(STEP_PINS[i], OUTPUT);
    digitalWrite(STEP_PINS[i], LOW);
    pinMode(DIR_PINS[i], OUTPUT);
    digitalWrite(DIR_PINS[i], LOW);
  }

  esp_log_level_set("*", ESP_LOG_NONE);  // nothing but JSON on UART0
  Serial.setRxBufferSize(1024);
  Serial.setTxBufferSize(4096);  // a reply must never block the step loop
  Serial.begin(115200);

  LittleFS.begin(true);  // the position file lives here; main() mounts it too

  core = new mc::MotorCore(&board, CHANNELS);
  xTaskCreatePinnedToCore(motorTask, "motors", 8192, nullptr, 5, nullptr, 0);
}

}  // namespace motors
