"""The broker's access control, against a real Mosquitto.

``deploy/mosquitto-weasley.acl`` is the only thing standing between a display
node and the hands.  Six ESP32 boards sit in the cabinet on the house WiFi,
each with its MQTT password in a plain ``config.json`` on its own flash, so
anyone who can unscrew a bezel can read one.  If that password could publish
to ``clock/location/<person>`` it could move every hand on the clock.

An ACL is not the kind of thing to take on trust from reading it.  Mosquitto's
matching rules have corners -- ``+`` against a multi-level topic, the order of
``user`` blocks, what an unmatched client gets -- and the failure mode is
silent: Mosquitto accepts a publish it is going to drop and tells the client
nothing.  So these tests start a **real** ``mosquitto`` with the committed
config and ACL and check what actually arrives at the other end.

Skipped where Mosquitto is not installed, which includes most laptops; CI on
a Debian runner has it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONF = REPO / "deploy" / "mosquitto-weasley.conf"
ACL = REPO / "deploy" / "mosquitto-weasley.acl"

CLOCK_PASSWORD = "clock-test-password"
DISPLAY_PASSWORD = "display-test-password"
PHONE_PASSWORD = "phone-test-password"

needs_mosquitto = pytest.mark.skipif(
    not all(shutil.which(name) for name in ("mosquitto", "mosquitto_passwd",
                                            "mosquitto_pub", "mosquitto_sub")),
    reason="mosquitto and mosquitto-clients are not installed",
)


def free_port() -> int:
    """A port nothing is using, so tests can run beside a real broker."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    """A real Mosquitto running the committed config and ACL.

    The config is the shipped file with only the listener line and the two
    file paths rewritten -- everything else, including every ACL rule, is
    exactly what goes onto the Pi.
    """
    if not all(shutil.which(n) for n in ("mosquitto", "mosquitto_passwd")):
        pytest.skip("mosquitto is not installed")

    import getpass

    directory = tmp_path_factory.mktemp("mosquitto")
    port = free_port()
    passwd = directory / "passwd"
    acl = directory / "acl"
    conf = directory / "mosquitto.conf"

    acl.write_text(ACL.read_text(encoding="utf-8"), encoding="utf-8")
    for user, password in (
        ("clock", CLOCK_PASSWORD),
        ("display", DISPLAY_PASSWORD),
        ("phone", PHONE_PASSWORD),
    ):
        subprocess.run(
            ["mosquitto_passwd", *(["-c"] if user == "clock" else []),
             "-b", str(passwd), user, password],
            check=True, capture_output=True,
        )

    text = CONF.read_text(encoding="utf-8")
    text = text.replace("listener 1883", f"listener {port} 127.0.0.1")
    text = text.replace("/etc/mosquitto/weasley.passwd", str(passwd))
    text = text.replace("/etc/mosquitto/weasley.acl", str(acl))
    # Running as root, mosquitto drops to the `mosquitto` user and then cannot
    # read a password file in a temp directory. On the Pi that is handled by
    # chowning it; here, just do not drop.
    text += f"\nuser {getpass.getuser()}\n"
    conf.write_text(text, encoding="utf-8")

    process = subprocess.Popen(
        ["mosquitto", "-c", str(conf)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        if process.poll() is not None:
            pytest.fail(f"mosquitto exited: {process.communicate()[0]}")
        time.sleep(0.1)
    else:  # pragma: no cover - a broker that never listens
        process.kill()
        pytest.fail("mosquitto did not start listening")

    try:
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def publish(port, user, password, topic, payload="x", retain=True) -> int:
    """Publish, returning the exit code.

    Note that a successful exit means nothing about whether the broker kept
    the message: an ACL denial is silent, which is exactly why these tests
    check the other end instead.
    """
    args = ["mosquitto_pub", "-h", "127.0.0.1", "-p", str(port),
            "-t", topic, "-m", payload]
    if user:
        args += ["-u", user, "-P", password]
    if retain:
        args.append("-r")
    return subprocess.run(args, capture_output=True, text=True, timeout=10).returncode


def receive_all(port, user, password, topic, wait=2) -> list[str]:
    """Subscribe to a wildcard and collect every ``topic payload`` line.

    Used for the realistic attack: do not try to read one topic, just ask for
    everything and see what the broker hands over.
    """
    args = ["mosquitto_sub", "-h", "127.0.0.1", "-p", str(port),
            "-t", topic, "-v", "-W", str(wait)]
    if user:
        args += ["-u", user, "-P", password]
    done = subprocess.run(args, capture_output=True, text=True, timeout=wait + 8)
    return [line for line in done.stdout.splitlines() if line.strip()]


def receive(port, user, password, topic, wait=2) -> str | None:
    """Subscribe and return the first retained payload, or None on timeout."""
    args = ["mosquitto_sub", "-h", "127.0.0.1", "-p", str(port),
            "-t", topic, "-C", "1", "-W", str(wait)]
    if user:
        args += ["-u", user, "-P", password]
    done = subprocess.run(args, capture_output=True, text=True, timeout=wait + 8)
    return done.stdout.strip() if done.returncode == 0 else None


@needs_mosquitto
class TestTheBrokerIsClosed:
    """Nothing gets in without a name."""

    def test_anonymous_cannot_connect_at_all(self, broker):
        # Mosquitto 2.x defaults to this, but the config says it explicitly
        # and a drop-in that lost the line would be silently wide open.
        assert publish(broker, None, None, "clock/display/evan") != 0

    def test_a_wrong_password_is_refused(self, broker):
        assert publish(broker, "clock", "not-the-password", "clock/display/evan") != 0

    def test_a_user_that_does_not_exist_is_refused(self, broker):
        assert publish(broker, "intruder", "whatever", "clock/display/evan") != 0


@needs_mosquitto
class TestTheController:
    """The `clock` account owns the clock tree."""

    def test_it_can_publish_state(self, broker):
        assert publish(broker, "clock", CLOCK_PASSWORD, "clock/state/evan", '{"s":1}') == 0
        assert receive(broker, "clock", CLOCK_PASSWORD, "clock/state/evan") == '{"s":1}'

    def test_it_can_publish_display_topics(self, broker):
        assert publish(broker, "clock", CLOCK_PASSWORD, "clock/display/evan", '{"d":1}') == 0
        assert receive(broker, "clock", CLOCK_PASSWORD, "clock/display/evan") == '{"d":1}'

    def test_it_can_publish_system_status_for_the_last_will(self, broker):
        publish(broker, "clock", CLOCK_PASSWORD, "clock/system/status", '{"online":true}')
        assert receive(
            broker, "clock", CLOCK_PASSWORD, "clock/system/status"
        ) == '{"online":true}'

    def test_it_can_read_a_location_from_the_generic_source(self, broker):
        publish(broker, "clock", CLOCK_PASSWORD, "clock/location/evan", '{"lat":1}')
        assert receive(broker, "clock", CLOCK_PASSWORD, "clock/location/evan") == '{"lat":1}'

    def test_it_can_read_owntracks(self, broker):
        publish(broker, "phone", PHONE_PASSWORD, "owntracks/evan/iphone", '{"_type":"location"}')
        assert receive(
            broker, "clock", CLOCK_PASSWORD, "owntracks/evan/iphone"
        ) == '{"_type":"location"}'


@needs_mosquitto
class TestADisplayNodeCannotMoveTheHands:
    """The reason the roles are split at all.

    A board in the cabinet has its password in cleartext on its own flash.
    These are the things that password must not be able to do.
    """

    def test_it_cannot_publish_a_position(self, broker):
        # The one that matters. Mosquitto accepts the packet and drops it
        # silently, so the proof is that the controller never sees it.
        publish(broker, "display", DISPLAY_PASSWORD, "clock/location/evan", '{"lat":51.5}')
        assert receive(broker, "clock", CLOCK_PASSWORD, "clock/location/evan") != '{"lat":51.5}'

    def test_it_cannot_forge_controller_state(self, broker):
        publish(broker, "display", DISPLAY_PASSWORD, "clock/state/evan", '{"forged":true}')
        assert receive(broker, "clock", CLOCK_PASSWORD, "clock/state/evan") != '{"forged":true}'

    def test_it_cannot_repaint_another_node_by_writing_the_display_topic(self, broker):
        publish(broker, "display", DISPLAY_PASSWORD, "clock/display/caleb", '{"forged":true}')
        assert receive(
            broker, "clock", CLOCK_PASSWORD, "clock/display/caleb"
        ) != '{"forged":true}'

    def test_it_cannot_take_the_clock_offline_by_forging_system_status(self, broker):
        publish(broker, "display", DISPLAY_PASSWORD, "clock/system/status", '{"online":false}')
        assert receive(
            broker, "clock", CLOCK_PASSWORD, "clock/system/status"
        ) != '{"online":false}'

    def test_it_cannot_read_where_anybody_is(self, broker):
        publish(broker, "clock", CLOCK_PASSWORD, "clock/state/micah", '{"secret":"home"}')
        assert receive(broker, "display", DISPLAY_PASSWORD, "clock/state/micah") is None

    def test_asking_for_the_whole_tree_only_yields_what_it_is_allowed(self, broker):
        """The realistic attack, and the way Mosquitto actually behaves.

        A subscription to ``clock/#`` is *accepted* -- Mosquitto does not
        refuse the wildcard -- and then every message is filtered against the
        ACL as it is delivered.  So the test is not "the subscribe fails", it
        is "nothing it should not see comes out".  Worth pinning, because
        reading the ACL file suggests the subscription itself would be
        rejected, and it is not.
        """
        publish(broker, "clock", CLOCK_PASSWORD, "clock/state/micah", '{"secret":"home"}')
        publish(broker, "clock", CLOCK_PASSWORD, "clock/display/micah", '{"name":"Micah"}')

        lines = receive_all(broker, "display", DISPLAY_PASSWORD, "clock/#")

        assert not [line for line in lines if line.startswith("clock/state/")], lines
        assert not [line for line in lines if "secret" in line], lines
        # And it does still get its own job's traffic, so this is not just a
        # broker that handed over nothing at all.
        assert [line for line in lines if line.startswith("clock/display/")], lines


@needs_mosquitto
class TestADisplayNodeCanDoItsJob:
    """Locked down is only correct if the nodes still work."""

    def test_it_can_read_its_own_display_topic(self, broker):
        publish(broker, "clock", CLOCK_PASSWORD, "clock/display/evan", '{"name":"Evan"}')
        assert receive(
            broker, "display", DISPLAY_PASSWORD, "clock/display/evan"
        ) == '{"name":"Evan"}'

    def test_it_can_see_the_controller_going_away(self, broker):
        # The node greys itself out on the Last Will, so it has to be able to
        # read system status even though it may write nothing there.
        publish(broker, "clock", CLOCK_PASSWORD, "clock/system/status", '{"online":false}')
        assert receive(
            broker, "display", DISPLAY_PASSWORD, "clock/system/status"
        ) == '{"online":false}'

    def test_it_can_publish_its_own_status(self, broker):
        assert publish(
            broker, "display", DISPLAY_PASSWORD, "clock/node/evan/status", '{"rssi":-55}'
        ) == 0
        assert receive(
            broker, "clock", CLOCK_PASSWORD, "clock/node/evan/status"
        ) == '{"rssi":-55}'


@needs_mosquitto
class TestAPhoneIsWriteOnly:
    """OwnTracks, if it is ever switched on."""

    def test_it_can_publish_its_own_position(self, broker):
        assert publish(
            broker, "phone", PHONE_PASSWORD, "owntracks/austin/iphone", '{"lat":40.0}'
        ) == 0
        assert receive(
            broker, "clock", CLOCK_PASSWORD, "owntracks/austin/iphone"
        ) == '{"lat":40.0}'

    def test_it_cannot_read_where_anybody_is(self, broker):
        # A phone has no business learning the rest of the family's positions
        # from the broker.
        publish(broker, "clock", CLOCK_PASSWORD, "clock/state/becky", '{"secret":"home"}')
        assert receive(broker, "phone", PHONE_PASSWORD, "clock/state/becky") is None

    def test_it_cannot_read_another_phone_back(self, broker):
        publish(broker, "phone", PHONE_PASSWORD, "owntracks/becky/iphone", '{"lat":44.9}')
        assert receive(broker, "phone", PHONE_PASSWORD, "owntracks/becky/iphone") is None

    def test_it_cannot_publish_into_the_clock_tree(self, broker):
        publish(broker, "phone", PHONE_PASSWORD, "clock/location/becky", '{"lat":0}')
        assert receive(broker, "clock", CLOCK_PASSWORD, "clock/location/becky") != '{"lat":0}'
