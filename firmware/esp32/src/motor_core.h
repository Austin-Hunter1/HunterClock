// The motor firmware's brain: the C++ port of firmware/pico/protocol.py and
// firmware/pico/core.py.  It speaks the same newline-delimited JSON as the Pico
// (docs/PROTOCOL.md), so clockd and tools/bench.py need no changes.
//
// No Arduino here either: time, pins, flash and the serial port come in through
// the Board interface, so this file compiles on a PC and tests/ can run it
// against the Python original.

#pragma once

#include <ArduinoJson.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "motion_core.h"

namespace mc {

static const int PROTOCOL_VERSION = 1;
static const char FIRMWARE_VERSION[] = "0.1.0";

// What the core needs from the board, on top of mc::Hal.
struct Board : public Hal {
  virtual void send(const char* line) = 0;  // one JSON message, no newline
  // Positions saved before the last reset: returns false when there are none.
  virtual bool loadPositions(int32_t* out, int n) = 0;
  virtual void savePositions(const int32_t* pos, int n) = 0;
  virtual const char* positionSource() { return "none"; }
  virtual uint32_t flashErrors() { return 0; }
  virtual const char* bootReason() { return "unknown"; }
  virtual const char* boardName() { return "esp32"; }
};

struct ProtocolError {
  const char* code;
  char msg[96];
  bool hasSeq;
  int64_t seq;
};

static inline ProtocolError perr(const char* code, const char* msg, bool hasSeq = false,
                                 int64_t seq = 0) {
  ProtocolError e;
  e.code = code;
  snprintf(e.msg, sizeof e.msg, "%s", msg);
  e.hasSeq = hasSeq;
  e.seq = seq;
  return e;
}

// A validated command.
struct Command {
  int64_t seq;
  char cmd[16];
  int ch;  // -1 when absent
  int32_t target, delta, pos;
  bool on;
  bool hasMaxSpeed, hasAccel, hasIdleDisable, hasWatchdog, hasPosInterval;
  real maxSpeed, accel;
  bool idleDisable;
  float watchdogS;
  int32_t posIntervalMs;
};

class MotorCore {
 public:
  static const int DEFAULT_POS_INTERVAL_MS = 200;
  static constexpr float DEFAULT_WATCHDOG_S = 10.0f;
  static const int64_t FLASH_MIN_INTERVAL_MS = 10000;
  static const int64_t FLASH_SETTLE_MS = 1000;

  MotionController mc;
  Board* board;
  int channels;

  float watchdog_s;
  int32_t pos_interval_ms;
  bool idle_disable;
  bool needs_verification;
  int64_t started_us;
  int64_t last_ping_us;
  bool have_last_ping;
  int64_t last_pos_us;
  bool have_last_pos;
  bool watchdog_tripped;
  bool watchdog_stopping;
  uint32_t commands_handled;
  uint32_t errors;
  int64_t last_flash_us;
  bool have_last_flash;
  int64_t rest_since_us;
  bool have_rest_since;
  int32_t flashed[MAX_AXES];
  bool have_flashed;
  int32_t last_reported[MAX_AXES];
  bool have_last_reported;
  const char* pos_source;

  MotorCore(Board* b, int n)
      : board(b), channels(n > MAX_AXES ? MAX_AXES : n), watchdog_s(DEFAULT_WATCHDOG_S),
        pos_interval_ms(DEFAULT_POS_INTERVAL_MS), idle_disable(false),
        needs_verification(true), started_us(0), last_ping_us(0), have_last_ping(false),
        last_pos_us(0), have_last_pos(false), watchdog_tripped(false),
        watchdog_stopping(false), commands_handled(0), errors(0), last_flash_us(0),
        have_last_flash(false), rest_since_us(0), have_rest_since(false), have_flashed(false),
        have_last_reported(false), pos_source("none") {
    mc.init(channels, b);
    int32_t restored[MAX_AXES] = {0};
    if (b->loadPositions(restored, channels)) {
      for (int i = 0; i < channels; i++) mc.axes[i].setPosition(restored[i]);
      copyPositions(flashed);
      have_flashed = true;
    }
    pos_source = b->positionSource();
  }

  // -- lifecycle -------------------------------------------------------------

  void start(int64_t now_us) {
    started_us = now_us;
    last_ping_us = now_us;
    have_last_ping = true;
    rest_since_us = now_us;
    have_rest_since = true;
    JsonDocument d;
    d["ev"] = "boot";
    d["fw"] = FIRMWARE_VERSION;
    d["proto"] = PROTOCOL_VERSION;
    d["reason"] = board->bootReason();
    d["channels"] = channels;
    d["needs_verification"] = needs_verification;
    putPositions(d["pos"].to<JsonArray>());
    d["pos_source"] = pos_source;
    emit(d);
  }

  int64_t uptimeMs(int64_t now_us) const { return (now_us - started_us) / 1000; }

  // -- incoming lines ----------------------------------------------------------

  // Decode, validate and execute one received line.  Never fails: a malformed
  // line gets an error reply.
  void handleLine(const char* line, size_t len, int64_t now_us) {
    JsonDocument in;
    ProtocolError e;
    if (!decode(line, len, in, e)) {
      errors++;
      emitError(e, nullptr);
      return;
    }
    Command c;
    if (!parseCommand(in, c, e)) {
      errors++;
      const char* name = in["cmd"].is<const char*>() ? in["cmd"].as<const char*>() : nullptr;
      emitError(e, name);
      return;
    }
    JsonDocument reply;
    if (!execute(c, now_us, reply, e)) {
      errors++;
      e.hasSeq = true;
      e.seq = c.seq;
      emitError(e, c.cmd);
      return;
    }
    commands_handled++;
    emit(reply);
  }

  // -- periodic work -------------------------------------------------------------

  // Emit due STEP pulses, then the housekeeping.  Call it as fast as possible.
  void tick(int64_t now_us) {
    mc.service(now_us);
    const bool moving = mc.anyMoving();

    // Watchdog: losing the Pi's pings stops the hands in a controlled way.
    float silence_s = 0;
    bool silent = false;
    if (watchdog_s > 0 && have_last_ping) {
      silence_s = (float)(now_us - last_ping_us) / 1000000.0f;
      silent = silence_s > watchdog_s;
    }
    if (silent) {
      const bool first = !watchdog_tripped;
      watchdog_tripped = true;
      if (moving) mc.stop(now_us, -1);
      if (first || (moving && !watchdog_stopping)) {
        JsonDocument d;
        d["ev"] = "fault";
        d["code"] = "watchdog";
        char msg[64];
        snprintf(msg, sizeof msg,
                 moving ? "no ping for %.1f s; decelerating"
                        : "no ping for %.1f s; hands already at rest",
                 (double)silence_s);
        d["msg"] = msg;
        putPositions(d["pos"].to<JsonArray>());
        emit(d);
      }
      watchdog_stopping = moving;
    } else {
      watchdog_stopping = false;
    }

    if (shouldReport(now_us, moving)) {
      last_pos_us = now_us;
      have_last_pos = true;
      copyPositions(last_reported);
      have_last_reported = true;
      JsonDocument d;
      d["ev"] = "pos";
      d["t"] = uptimeMs(now_us);
      putPositions(d["pos"].to<JsonArray>());
      JsonArray mv = d["moving"].to<JsonArray>();
      for (int i = 0; i < channels; i++) mv.add(mc.axes[i].isMoving());
      emit(d);
    }

    if (moving) {
      have_rest_since = false;
    } else if (!have_rest_since) {
      rest_since_us = now_us;
      have_rest_since = true;
    }

    if (idle_disable && !moving && mc.enabled && atRestForMs(now_us, FLASH_SETTLE_MS)) {
      mc.setEnable(false);
    }
    maybeFlash(now_us, moving);
  }

 private:
  // -- helpers -----------------------------------------------------------------

  void copyPositions(int32_t* out) const {
    for (int i = 0; i < channels; i++) out[i] = mc.axes[i].position;
  }

  void putPositions(JsonArray a) const {
    for (int i = 0; i < channels; i++) a.add(mc.axes[i].position);
  }

  bool positionsEqual(const int32_t* other) const {
    for (int i = 0; i < channels; i++)
      if (mc.axes[i].position != other[i]) return false;
    return true;
  }

  void emit(const JsonDocument& d) {
    char buf[1024];
    size_t n = serializeJson(d, buf, sizeof buf - 1);
    buf[n] = 0;
    board->send(buf);
  }

  void emitError(const ProtocolError& e, const char* cmd) {
    JsonDocument d;
    if (e.hasSeq) d["seq"] = e.seq; else d["seq"] = nullptr;
    if (cmd) d["cmd"] = cmd; else d["cmd"] = nullptr;
    d["ok"] = false;
    d["err"] = e.code;
    d["msg"] = e.msg;
    emit(d);
  }

  bool shouldReport(int64_t now_us, bool moving) const {
    if (pos_interval_ms <= 0) return false;
    if (!have_last_pos) return true;
    if ((now_us - last_pos_us) < (int64_t)pos_interval_ms * 1000) return false;
    if (moving) return true;  // at rest, report only when something changed
    return !(have_last_reported && positionsEqual(last_reported));
  }

  bool atRestForMs(int64_t now_us, int64_t ms) const {
    return have_rest_since && (now_us - rest_since_us) >= ms * 1000;
  }

  void maybeFlash(int64_t now_us, bool moving) {
    if (moving || !atRestForMs(now_us, FLASH_SETTLE_MS)) return;
    if (have_flashed && positionsEqual(flashed)) return;
    if (have_last_flash && (now_us - last_flash_us) < FLASH_MIN_INTERVAL_MS * 1000) return;
    int32_t p[MAX_AXES];
    copyPositions(p);
    board->savePositions(p, channels);
    memcpy(flashed, p, sizeof p);
    have_flashed = true;
    last_flash_us = now_us;
    have_last_flash = true;
  }

  // -- decode / parse ----------------------------------------------------------

  static bool decode(const char* line, size_t len, JsonDocument& doc, ProtocolError& e) {
    while (len && (line[len - 1] == '\n' || line[len - 1] == '\r' || line[len - 1] == ' '))
      len--;
    while (len && (*line == ' ' || *line == '\t')) { line++; len--; }
    if (!len) { e = perr("bad_json", "empty line"); return false; }
    DeserializationError err = deserializeJson(doc, line, len);
    if (err) { e = perr("bad_json", "not valid JSON"); return false; }
    if (!doc.is<JsonObject>()) { e = perr("not_object", "top level is not an object"); return false; }
    return true;
  }

  static bool asInt(JsonVariantConst v, const char* key, int64_t seq, int32_t& out,
                    ProtocolError& e) {
    char m[96];
    if (v.is<bool>() || !(v.is<long long>() || v.is<double>())) {
      snprintf(m, sizeof m, "'%s' must be a number", key);
      e = perr("bad_arg", m, true, seq);
      return false;
    }
    if (v.is<long long>()) { out = (int32_t)v.as<long long>(); return true; }
    double d = v.as<double>();
    if (!(d == d) || d > 1e18 || d < -1e18) {
      snprintf(m, sizeof m, "'%s' must be finite", key);
      e = perr("bad_arg", m, true, seq);
      return false;
    }
    if (d != (double)(long long)d) {
      snprintf(m, sizeof m, "'%s' must be a whole number of microsteps", key);
      e = perr("bad_arg", m, true, seq);
      return false;
    }
    out = (int32_t)(long long)d;
    return true;
  }

  static bool asFloat(JsonVariantConst v, const char* key, int64_t seq, double& out,
                      ProtocolError& e) {
    char m[96];
    if (v.is<bool>() || !(v.is<long long>() || v.is<double>())) {
      snprintf(m, sizeof m, "'%s' must be a number", key);
      e = perr("bad_arg", m, true, seq);
      return false;
    }
    out = v.as<double>();
    if (!(out == out) || out > 1e30 || out < -1e30) {
      snprintf(m, sizeof m, "'%s' must be finite", key);
      e = perr("bad_arg", m, true, seq);
      return false;
    }
    return true;
  }

  static bool asBool(JsonVariantConst v, const char* key, int64_t seq, bool& out,
                     ProtocolError& e) {
    if (v.is<bool>()) { out = v.as<bool>(); return true; }
    if (v.is<long long>()) {
      long long n = v.as<long long>();
      if (n == 0 || n == 1) { out = n == 1; return true; }
    }
    if (v.is<const char*>()) {
      const char* s = v.as<const char*>();
      if (!strcmp(s, "on") || !strcmp(s, "true")) { out = true; return true; }
      if (!strcmp(s, "off") || !strcmp(s, "false")) { out = false; return true; }
    }
    char m[96];
    snprintf(m, sizeof m, "'%s' must be a boolean", key);
    e = perr("bad_arg", m, true, seq);
    return false;
  }

  bool channelArg(JsonDocument& in, int64_t seq, bool required, int& ch, ProtocolError& e) {
    JsonVariantConst v = in["ch"];
    if (v.isNull()) {
      if (required) { e = perr("missing_arg", "missing 'ch'", true, seq); return false; }
      ch = -1;
      return true;
    }
    int32_t n;
    if (!asInt(v, "ch", seq, n, e)) return false;
    if (n < 0 || n >= channels) {
      char m[96];
      snprintf(m, sizeof m, "channel %d out of range", (int)n);
      e = perr("bad_channel", m, true, seq);
      return false;
    }
    ch = n;
    return true;
  }

  bool required(JsonDocument& in, const char* key, int64_t seq, ProtocolError& e) {
    if (in[key].isNull()) {
      char m[96];
      snprintf(m, sizeof m, "missing '%s'", key);
      e = perr("missing_arg", m, true, seq);
      return false;
    }
    return true;
  }

  bool parseCommand(JsonDocument& in, Command& c, ProtocolError& e) {
    memset(&c, 0, sizeof c);
    c.ch = -1;
    JsonVariantConst vs = in["seq"];
    if (vs.isNull()) { e = perr("no_seq", "command has no seq"); return false; }
    if (vs.is<bool>() || !vs.is<long long>() || vs.as<long long>() <= 0) {
      e = perr("bad_seq", "seq must be a positive integer");
      return false;
    }
    const int64_t seq = vs.as<long long>();
    c.seq = seq;
    JsonVariantConst vc = in["cmd"];
    if (vc.isNull()) { e = perr("no_cmd", "command has no cmd", true, seq); return false; }
    if (!vc.is<const char*>()) { e = perr("no_cmd", "cmd must be a string", true, seq); return false; }
    const char* cmd = vc.as<const char*>();
    static const char* const COMMANDS[] = {"hello", "info",   "status", "move",       "jog",
                                           "set_pos", "enable", "stop",  "estop",
                                           "set_params", "ping"};
    bool known = false;
    for (const char* k : COMMANDS) if (!strcmp(cmd, k)) known = true;
    if (!known) {
      char m[96];
      snprintf(m, sizeof m, "unknown command '%.40s'", cmd);
      e = perr("unknown_cmd", m, true, seq);
      return false;
    }
    snprintf(c.cmd, sizeof c.cmd, "%s", cmd);

    if (!strcmp(cmd, "move")) {
      if (!channelArg(in, seq, true, c.ch, e)) return false;
      if (!required(in, "target", seq, e)) return false;
      return asInt(in["target"], "target", seq, c.target, e);
    }
    if (!strcmp(cmd, "jog")) {
      if (!channelArg(in, seq, true, c.ch, e)) return false;
      if (!required(in, "delta", seq, e)) return false;
      return asInt(in["delta"], "delta", seq, c.delta, e);
    }
    if (!strcmp(cmd, "set_pos")) {
      if (!channelArg(in, seq, true, c.ch, e)) return false;
      if (!required(in, "pos", seq, e)) return false;
      return asInt(in["pos"], "pos", seq, c.pos, e);
    }
    if (!strcmp(cmd, "enable")) {
      if (!required(in, "on", seq, e)) return false;
      return asBool(in["on"], "on", seq, c.on, e);
    }
    if (!strcmp(cmd, "stop")) return channelArg(in, seq, false, c.ch, e);
    if (!strcmp(cmd, "set_params")) {
      if (!channelArg(in, seq, false, c.ch, e)) return false;
      double d;
      if (!in["max_speed"].isNull()) {
        if (!asFloat(in["max_speed"], "max_speed", seq, d, e)) return false;
        if (d <= 0) { e = perr("bad_arg", "max_speed must be positive", true, seq); return false; }
        c.hasMaxSpeed = true;
        c.maxSpeed = (real)d;
      }
      if (!in["accel"].isNull()) {
        if (!asFloat(in["accel"], "accel", seq, d, e)) return false;
        if (d <= 0) { e = perr("bad_arg", "accel must be positive", true, seq); return false; }
        c.hasAccel = true;
        c.accel = (real)d;
      }
      if (!in["idle_disable"].isNull()) {
        if (!asBool(in["idle_disable"], "idle_disable", seq, c.idleDisable, e)) return false;
        c.hasIdleDisable = true;
      }
      if (!in["watchdog_s"].isNull()) {
        if (!asFloat(in["watchdog_s"], "watchdog_s", seq, d, e)) return false;
        c.hasWatchdog = true;
        c.watchdogS = (float)d;
      }
      if (!in["pos_interval_ms"].isNull()) {
        if (!asInt(in["pos_interval_ms"], "pos_interval_ms", seq, c.posIntervalMs, e)) return false;
        c.hasPosInterval = true;
      }
    }
    return true;  // hello, info, status, estop, ping take no arguments
  }

  // -- execute -------------------------------------------------------------------

  void startReply(JsonDocument& r, const Command& c) {
    r["seq"] = c.seq;
    r["cmd"] = c.cmd;
    r["ok"] = true;
  }

  void putParams(JsonDocument& r, int channel) {
    const Axis& a = mc.axes[channel];
    r["max_speed"] = a.max_speed;
    r["accel"] = a.accel;
    r["idle_disable"] = idle_disable;
    r["watchdog_s"] = watchdog_s;
    r["pos_interval_ms"] = pos_interval_ms;
    r["speed_limit"] = (float)SPEED_LIMIT;
    r["accel_limit"] = (float)ACCEL_LIMIT;
  }

  bool requireEnabled(ProtocolError& e) {
    if (!mc.enabled) {
      e = perr("not_enabled", "drivers are disabled; send enable first");
      return false;
    }
    return true;
  }

  bool execute(const Command& c, int64_t now_us, JsonDocument& r, ProtocolError& e) {
    const char* cmd = c.cmd;
    startReply(r, c);

    if (!strcmp(cmd, "hello") || !strcmp(cmd, "info")) {
      r["fw"] = FIRMWARE_VERSION;
      r["proto"] = PROTOCOL_VERSION;
      r["boot_reason"] = board->bootReason();
      r["uptime_ms"] = uptimeMs(now_us);
      r["channels"] = channels;
      r["enabled"] = mc.enabled;
      r["needs_verification"] = needs_verification;
      putPositions(r["pos"].to<JsonArray>());
      r["pos_source"] = pos_source;
      r["board"] = board->boardName();
      putParams(r, 0);
      return true;
    }
    if (!strcmp(cmd, "ping")) {
      last_ping_us = now_us;
      have_last_ping = true;
      watchdog_tripped = false;
      r["t"] = uptimeMs(now_us);
      return true;
    }
    if (!strcmp(cmd, "status")) {
      putPositions(r["pos"].to<JsonArray>());
      JsonArray tg = r["target"].to<JsonArray>();
      JsonArray mv = r["moving"].to<JsonArray>();
      JsonArray vel = r["vel"].to<JsonArray>();
      for (int i = 0; i < channels; i++) {
        tg.add(mc.axes[i].target);
        mv.add(mc.axes[i].isMoving());
        vel.add(roundf((float)mc.axes[i].velocity * 100.0f) / 100.0f);
      }
      r["enabled"] = mc.enabled;
      r["needs_verification"] = needs_verification;
      r["uptime_ms"] = uptimeMs(now_us);
      r["steps"] = mc.step_count;
      r["late"] = mc.lateSteps();
      r["errors"] = errors;
      r["flash_errors"] = board->flashErrors();
      r["watchdog_tripped"] = watchdog_tripped;
      return true;
    }
    if (!strcmp(cmd, "move") || !strcmp(cmd, "jog")) {
      Axis& a = mc.axes[c.ch];
      if (!requireEnabled(e)) return false;
      if (!strcmp(cmd, "move")) a.moveTo(c.target, now_us);
      else a.jog(c.delta, now_us);
      r["ch"] = c.ch;
      r["pos"] = a.position;
      r["target"] = a.target;
      return true;
    }
    if (!strcmp(cmd, "set_pos")) {
      Axis& a = mc.axes[c.ch];
      if (a.isMoving()) {
        char m[96];
        snprintf(m, sizeof m, "channel %d is moving; stop it first", c.ch);
        e = perr("busy", m);
        return false;
      }
      a.setPosition(c.pos);
      rest_since_us = now_us;
      have_rest_since = true;
      r["ch"] = c.ch;
      r["pos"] = a.position;
      return true;
    }
    if (!strcmp(cmd, "enable")) {
      mc.setEnable(c.on);
      r["enabled"] = mc.enabled;
      return true;
    }
    if (!strcmp(cmd, "stop")) {
      mc.stop(now_us, c.ch);
      if (c.ch >= 0) r["ch"] = c.ch; else r["ch"] = nullptr;
      putPositions(r["pos"].to<JsonArray>());
      return true;
    }
    if (!strcmp(cmd, "estop")) {
      mc.estop();
      needs_verification = true;
      putPositions(r["pos"].to<JsonArray>());
      r["needs_verification"] = true;
      return true;
    }
    if (!strcmp(cmd, "set_params")) {
      if (c.hasMaxSpeed || c.hasAccel) {
        for (int i = 0; i < channels; i++)
          if (c.ch < 0 || c.ch == i)
            mc.axes[i].setParams(c.hasMaxSpeed, c.maxSpeed, c.hasAccel, c.accel);
      }
      if (c.hasIdleDisable) idle_disable = c.idleDisable;
      if (c.hasWatchdog) watchdog_s = c.watchdogS > 0 ? c.watchdogS : 0;
      if (c.hasPosInterval) pos_interval_ms = c.posIntervalMs > 0 ? c.posIntervalMs : 0;
      if (c.ch >= 0) r["ch"] = c.ch; else r["ch"] = nullptr;
      putParams(r, c.ch < 0 ? 0 : c.ch);
      return true;
    }
    e = perr("unknown_cmd", "unhandled command");
    return false;
  }
};

}  // namespace mc
