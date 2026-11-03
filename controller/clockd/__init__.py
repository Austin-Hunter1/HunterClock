"""clockd -- the Weasley Clock controller.

Turns each family member's location into an angle for one of six clock hands,
drives the Pico that generates the stepper pulses, publishes state over MQTT for
the six display nodes, and serves the web UI used to set the clock up and
troubleshoot it remotely.
"""

__version__ = "0.1.0"
