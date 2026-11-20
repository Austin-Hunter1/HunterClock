# Weasley Clock tracking system.
#
#   make test     run the whole pytest suite
#   make lint     ruff across the repo
#   make sim      start the controller with a fake Pico and fake locations
#   make help     everything else
#
# Works from a bare checkout: `make venv` creates .venv and installs the
# controller. On Windows use the commands in docs/TESTING.md instead; nmake and
# GNU make behave differently enough that one Makefile cannot serve both.

VENV    ?= .venv
PY      ?= $(VENV)/bin/python
PIP     ?= $(VENV)/bin/pip
PYTEST  ?= $(VENV)/bin/pytest
RUFF    ?= $(VENV)/bin/ruff
PORT    ?= 8080

# Fall back to whatever python is on PATH when .venv does not exist yet, so
# `make test` works in CI without a venv step.
ifeq ($(wildcard $(VENV)/bin/python),)
  PY     := python3
  PYTEST := python3 -m pytest
  RUFF   := ruff
endif

.DEFAULT_GOAL := help
.PHONY: help venv install test test-fast lint format sim sim-fast sim-mqtt \
        screenshots firmware-upload clean check

help:  ## Show this help
	@echo "Weasley Clock -- make targets"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Full test and deployment instructions: docs/TESTING.md, docs/RUNBOOK.md"

venv:  ## Create .venv and install the controller (editable) plus dev tools
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip wheel
	$(VENV)/bin/pip install --quiet -e "controller[dev]"
	@echo "Ready. Now: make test"

install: venv  ## Alias for venv

test:  ## Run every test (about a minute; the integration tests drive a fake Pico)
	cd controller && $(abspath $(PY)) -m pytest -q

test-fast:  ## Run only the fast unit tests (skips integration and API)
	cd controller && $(abspath $(PY)) -m pytest -q \
	  --ignore=tests/test_integration.py --ignore=tests/test_api.py

lint:  ## Check the whole repo with ruff
	$(RUFF) check .

format:  ## Apply ruff's safe fixes
	$(RUFF) check . --fix

check: lint test  ## Lint and test, which is what CI runs

sim:  ## Start the controller with a fake Pico, fake broker and fake locations
	@echo "UI at http://127.0.0.1:$(PORT)/  (ctrl-C to stop)"
	cd controller && $(abspath $(PY)) -m clockd.cli --sim --sim-fixtures --port $(PORT)

sim-fast:  ## Same, with simulated time at 10x so moves finish quickly
	cd controller && $(abspath $(PY)) -m clockd.cli --sim --sim-fixtures \
	  --sim-time-scale 10 --port $(PORT)

sim-mqtt:  ## Sim mode, but against a real local Mosquitto instead of the fake broker
	cd controller && $(abspath $(PY)) -m clockd.cli --sim --sim-fixtures \
	  --mqtt-host localhost --port $(PORT)

screenshots:  ## Re-render the UI screenshots in docs/img/ui (needs playwright)
	node tools/ui_screenshots.js

firmware-upload:  ## Copy the firmware onto a connected Pico (needs mpremote)
	@command -v mpremote >/dev/null || { echo "mpremote not installed: pip install mpremote"; exit 1; }
	mpremote connect $(or $(PICO_PORT),/dev/ttyACM0) fs cp \
	  firmware/pico/motion.py firmware/pico/protocol.py \
	  firmware/pico/core.py firmware/pico/main.py :
	mpremote connect $(or $(PICO_PORT),/dev/ttyACM0) reset
	@echo "Uploaded and reset. Every hand is now unverified: re-zero on /assist."

clean:  ## Remove caches and build artefacts (keeps .venv, config and var/)
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache controller/*.egg-info controller/build
