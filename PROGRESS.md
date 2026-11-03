# PROGRESS

Running log for the tracking-system build. Branch `claude/tracking-system`.
Updated after every push.

Legend: **tested in sim** = exercised by pytest or by `make sim` on this Linux
container. **untested on hardware** = the code exists and is reviewed but no
Pico, motor, Pi or ESP32 has run it. **not done** = not written.

## Status: 2026-11-03, push 1

### Done (tested in sim)

- `controller/clockd/geo.py` -- the distance-to-angle mapping from
  `birthday_clock.m`, plus shortest-path helpers. All six spec vectors pass
  within 1 km / 0.1 degree (`controller/tests/test_geo.py`, 78 tests).
- `firmware/pico/motion.py` -- pure-Python trapezoidal planner, six independent
  axes, exact arrival, speed/accel ceilings, braking reversal, stop/estop/set_pos
  (`controller/tests/test_motion.py`, 38 tests).
- `firmware/pico/protocol.py` -- newline-delimited JSON codec and command
  validation (`controller/tests/test_protocol.py`, 72 tests).
- `controller/clockd/picolib.py` -- one source of truth: the controller, the
  tests and the simulator all import the firmware's `motion`/`protocol`.
- Packaging (`controller/pyproject.toml`), `.gitignore`, `.env.example`.

Total: 188 pytest tests passing on Python 3.13 (Linux container).

### Untested on hardware

Everything in `firmware/pico/` is untested on hardware -- no Pico has been
connected in this session. The planner's arithmetic is tested under CPython; its
pin layer is not.

### Next

- `firmware/pico/main.py` (hardware layer) and `tools/pico_sim.py`.
- Controller config schema, serial link, MQTT bus, location sources, engine.
- Web UI: Dashboard, Control, Simulate, Logs.
- `deploy/install.sh`, systemd unit, RUNBOOK.

### Not done

ESP32 display firmware, `make_loop.py`, Home Assistant source, alerts,
fallback hotspot, UI screenshots.

### Notes and deviations

- The spec says branch `claude/tracking-system`; the session harness suggested a
  different branch name. Following the spec and the explicit instruction.
- No gear reduction anywhere in the hub: `docs/hardware-plan.html` mounts each
  hand directly on its motor shaft, so 3200 microsteps = 360 degrees = one hand
  revolution. `steps_per_rev` is still per-person configurable.
- Python here is 3.13, not the Pi's 3.11. Nothing used needs 3.12+; the code
  targets 3.11 (`requires-python`, ruff `target-version`).
