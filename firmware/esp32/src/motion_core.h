// Trapezoidal step planner: the C++ port of firmware/pico/motion.py.
//
// Pure C++ -- no Arduino, no pins -- so it compiles on a PC as well as on the
// ESP32, and tests/test_motion_port.py replays the same moves through this and
// through motion.py and compares the step traces.  If you change the planner,
// change both.
//
// Everything is in microsteps; 3200 is one turn of a hand.  Positions are
// signed and unwrapped, as in docs/PROTOCOL.md.

#pragma once

#include <math.h>
#include <stdint.h>

#ifndef MC_REAL
#define MC_REAL float  // the ESP32 has a single-precision FPU; doubles are emulated
#endif

namespace mc {

typedef MC_REAL real;

// Hard ceilings.  set_params may lower these, never raise them.
static const real SPEED_LIMIT = 4000;     // microsteps/s
static const real ACCEL_LIMIT = 20000;    // microsteps/s^2
static const real MIN_SPEED = 1;          // floors the step interval at 1 s
static const real DEFAULT_MAX_SPEED = 800;
static const real DEFAULT_ACCEL = 1600;

// How late a STEP may be and still be caught up by bunching it against the
// next one, in whole step intervals.  Beyond that the schedule is re-based on
// the current time and `late` is counted, rather than emitting a burst of
// pulses a motor cannot follow.  See the long comment in motion.py.
static const int MAX_CATCHUP_INTERVALS = 1;

static const int MAX_AXES = 6;

static inline real clampr(real v, real lo, real hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// sqrt and ceil in the precision of `real`: sqrtf on the ESP32 (hardware FPU),
// sqrt on a PC test build with MC_REAL=double.
static inline real rsqrt(real v) {
  return sizeof(real) == sizeof(float) ? (real)sqrtf((float)v) : (real)sqrt((double)v);
}
static inline real rceil(real v) {
  return sizeof(real) == sizeof(float) ? (real)ceilf((float)v) : (real)ceil((double)v);
}

static inline int sgn(real v) { return v > 0 ? 1 : (v < 0 ? -1 : 0); }
static inline int sgn(int32_t v) { return v > 0 ? 1 : (v < 0 ? -1 : 0); }

// One hand.
struct Axis {
  int index;
  int32_t position;
  int32_t target;
  real velocity;
  real max_speed;
  real accel;
  int64_t next_step_us;
  bool scheduled;  // false is Python's next_step_us = None
  uint32_t steps_taken;
  int direction;
  uint32_t late;

  Axis()
      : index(0), position(0), target(0), velocity(0),
        max_speed(DEFAULT_MAX_SPEED), accel(DEFAULT_ACCEL), next_step_us(0),
        scheduled(false), steps_taken(0), direction(1), late(0) {}

  void init(int idx, real max_speed_, real accel_) {
    index = idx;
    max_speed = clampr(max_speed_, MIN_SPEED, SPEED_LIMIT);
    accel = clampr(accel_, 1, ACCEL_LIMIT);
  }

  void setParams(bool haveSpeed, real speed, bool haveAccel, real acc) {
    if (haveSpeed) max_speed = clampr(speed, MIN_SPEED, SPEED_LIMIT);
    if (haveAccel) accel = clampr(acc, 1, ACCEL_LIMIT);
  }

  bool isMoving() const { return position != target || velocity != 0; }

  // Declare where the hand is without moving it (zeroing).  Only legal at rest.
  void setPosition(int32_t p) {
    int32_t delta = p - position;
    position = p;
    target += delta;
    if (!isMoving()) target = position;
  }

  void moveTo(int32_t t, int64_t now_us) {
    target = t;
    if (target != position && !scheduled) {
      next_step_us = now_us;
      scheduled = true;
      updateDirection();
    }
  }

  void jog(int32_t delta, int64_t now_us) { moveTo(target + delta, now_us); }

  // Decelerate to a stop at the configured acceleration.  Calling it again on
  // an axis that is already braking must not move the stopping point further
  // away; see the comment on Axis.stop in motion.py.
  void stop(int64_t now_us) {
    if (!isMoving()) {
      target = position;
      return;
    }
    int dir = sgn(velocity);
    if (dir == 0) dir = sgn((int32_t)(target - position));
    int32_t braking = (int32_t)rceil(velocity * velocity / (2 * accel));
    int32_t stop_target = position + dir * braking;
    if (dir > 0) {
      if (stop_target < target) target = stop_target;
    } else {
      if (stop_target > target) target = stop_target;
    }
    if (!scheduled) {
      next_step_us = now_us;
      scheduled = true;
    }
  }

  void estop() {
    velocity = 0;
    target = position;
    scheduled = false;
  }

  // Latch the DIR value for the step about to happen.
  void updateDirection() {
    if (velocity != 0) {
      direction = velocity > 0 ? 1 : -1;
    } else {
      int32_t delta = target - position;
      if (delta != 0) direction = delta > 0 ? 1 : -1;
    }
  }

  real braked(real speed) const {
    real squared = speed * speed - 2 * accel;
    if (squared <= 0) return 0;
    real v = rsqrt(squared);
    return v >= MIN_SPEED ? v : 0;
  }

  // Signed velocity for the next step, 0 to come to rest.
  real nextVelocity() const {
    int32_t delta = target - position;
    real speed = velocity < 0 ? -velocity : velocity;
    int moving_dir = sgn(velocity);

    if (delta == 0) {
      if (speed == 0) return 0;
      return moving_dir * braked(speed);
    }
    int want_dir = sgn(delta);
    if (moving_dir != 0 && moving_dir != want_dir) {
      return moving_dir * braked(speed);
    }
    real adelta = (real)(delta < 0 ? -(int64_t)delta : (int64_t)delta);
    real v_stop = rsqrt(2 * accel * adelta);
    real v_accel = rsqrt(speed * speed + 2 * accel);
    real v = max_speed;
    if (v_stop < v) v = v_stop;
    if (v_accel < v) v = v_accel;
    if (v < MIN_SPEED) v = MIN_SPEED;
    return want_dir * v;
  }

  // Advance one microstep and schedule the next.  The caller has already
  // produced the STEP pulse in `direction`.  Returns the direction stepped,
  // or 0 if there was nothing to do.
  int takeStep(int64_t now_us) {
    if (!isMoving()) {
      scheduled = false;
      velocity = 0;
      return 0;
    }
    int stepped = direction;
    position += stepped;
    steps_taken++;

    velocity = nextVelocity();
    if (velocity == 0 && position == target) {
      scheduled = false;
      return stepped;
    }
    if (velocity == 0) {
      // Came to rest mid-move (end of a braking phase): restart the ramp.
      updateDirection();
      if (position == target) {
        scheduled = false;
        return stepped;
      }
      velocity = nextVelocity();
    }

    updateDirection();
    real speed = velocity < 0 ? -velocity : velocity;
    if (speed < MIN_SPEED) speed = MIN_SPEED;
    int64_t interval = (int64_t)((real)1000000 / speed);
    if (interval < 1) interval = 1;
    int64_t due = (scheduled ? next_step_us : 0) + interval;
    if (due < now_us - MAX_CATCHUP_INTERVALS * interval) {
      due = now_us + interval;  // the loop was away; the move takes longer
      late++;
    }
    next_step_us = due;
    scheduled = true;
    return stepped;
  }
};

// What the planner needs from the board.
struct Hal {
  virtual void pulse(int channel, int direction) = 0;  // one STEP pulse
  virtual void setEnable(bool on) = 0;                 // shared, active low
  virtual ~Hal() {}
};

// The six axes together.
struct MotionController {
  Axis axes[MAX_AXES];
  int channels;
  bool enabled;
  uint32_t step_count;
  Hal* hal;

  MotionController() : channels(MAX_AXES), enabled(false), step_count(0), hal(0) {}

  void init(int n, Hal* h) {
    channels = n > MAX_AXES ? MAX_AXES : n;
    hal = h;
    for (int i = 0; i < channels; i++) axes[i].init(i, DEFAULT_MAX_SPEED, DEFAULT_ACCEL);
  }

  bool anyMoving() const {
    for (int i = 0; i < channels; i++)
      if (axes[i].isMoving()) return true;
    return false;
  }

  uint32_t lateSteps() const {
    uint32_t total = 0;
    for (int i = 0; i < channels; i++) total += axes[i].late;
    return total;
  }

  void setEnable(bool on) {
    enabled = on;
    if (hal) hal->setEnable(on);
  }

  void stop(int64_t now_us, int channel /* -1 = all */) {
    for (int i = 0; i < channels; i++)
      if (channel < 0 || channel == i) axes[i].stop(now_us);
  }

  void estop() {
    for (int i = 0; i < channels; i++) axes[i].estop();
  }

  // Emit every STEP due at or before now_us, at most `budget` of them.
  int service(int64_t now_us, int budget = 64) {
    int emitted = 0;
    while (emitted < budget) {
      bool moved = false;
      for (int i = 0; i < channels; i++) {
        Axis& a = axes[i];
        if (!a.scheduled || a.next_step_us > now_us) continue;
        if (hal) hal->pulse(a.index, a.direction);
        a.takeStep(now_us);
        emitted++;
        step_count++;
        moved = true;
        if (emitted >= budget) break;
      }
      if (!moved) break;
    }
    return emitted;
  }
};

}  // namespace mc
