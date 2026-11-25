"""The page somebody standing at the clock sees on their phone.

One screen, no login, no JavaScript, no fonts or stylesheets from anywhere:
the device looking at this has just joined an access point with no route to
the internet, so anything not inlined would hang and then render unstyled.

The design is shaped by one awkward fact about having a single radio: **the
portal cannot tell you whether it worked.**  Joining the house network means
tearing down the access point that is serving the page, so the answer to "did
my password work?" would have to travel over a network that no longer exists.
Pretending otherwise -- a spinner, a progress bar, a redirect to a result
page -- produces a phone that hangs on a dead socket, which reads as "the
clock is broken" rather than "the clock is busy succeeding".

So the flow is honest instead.  The POST hands back a page that says what is
about to happen, the join runs in the background, and the feedback channel is
the access point itself:

* the setup network vanishes and stays gone -> it worked, the clock is back;
* the setup network comes back in a few minutes -> it did not work, and the
  page will say what went wrong when they rejoin, because the failure is
  recorded in :class:`PortalState`.

That last point is why the portal keeps its own small state rather than being
stateless: the result of the join has to survive the network going away and
be waiting on the page when the person reconnects to try again.

Authentication is the access point's WPA2 passphrase and nothing else.
Adding a second password would mean a non-technical person on a stepladder
needing two secrets to fix a clock, and the portal only exists while the Pi
is already offline and only answers on the access point's own address.  What
it will not do is widen that: it reads nothing from ``config/clock.yaml``,
touches no motors, and never asks ``nmcli`` to print a stored passphrase, so
the worst a guest on the setup network can learn is the *names* of the
networks the clock has been told about.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .nm import Network, NetworkManager
from .settings import HotspotSettings

log = logging.getLogger(__name__)

#: URLs phones and laptops fetch to decide whether they are behind a captive
#: portal.  Answering any of them with something other than what the OS
#: expects is what makes the setup page appear on its own, which is the
#: difference between "join the network and the page pops up" and "join the
#: network, then open a browser and type an IP address" -- and the second one
#: is not a thing we can ask of the person who is actually standing there.
DETECTION_PATHS = (
    "/hotspot-detect.html",  # iOS, macOS
    "/library/test/success.html",  # older iOS
    "/generate_204",  # Android
    "/gen_204",  # Android
    "/connecttest.txt",  # Windows
    "/ncsi.txt",  # Windows
    "/canonical.html",  # Ubuntu, GNOME
    "/success.txt",  # Firefox
    "/connectivity-check",  # misc
)

#: Longest passphrase WPA2 takes, and the longest an SSID can be.
MAX_PASSWORD_LEN = 63
MAX_SSID_BYTES = 32


class PortalState:
    """What the portal knows, shared with the monitor and across restarts.

    ``last_seen`` is the one the monitor reads: it is how the service decides
    that somebody is actually at the clock and the access point should stay up
    a while longer.  Every request updates it, including the captive-portal
    detection hits a phone fires off by itself on associating, so it goes true
    within a second or two of somebody joining the network.
    """

    def __init__(self) -> None:
        self.last_seen: float | None = None
        self.networks: list[Network] = []
        self.scanned_at: float | None = None
        #: Result of the most recent join attempt, waiting to be shown to
        #: whoever comes back after the setup network reappears.
        self.last_attempt: dict[str, Any] | None = None
        self.joining: str | None = None
        self.attempts = 0

    def touch(self, now: float | None = None) -> None:
        """Record that somebody is there."""
        self.last_seen = now if now is not None else time.time()

    def set_scan(self, networks: list[Network], now: float | None = None) -> None:
        """Store a scan taken before the access point went up."""
        self.networks = networks
        self.scanned_at = now if now is not None else time.time()

    def record(self, ssid: str, ok: bool, detail: str, now: float | None = None) -> None:
        """Store the outcome of a join so the next visitor can be told."""
        self.attempts += 1
        self.joining = None
        self.last_attempt = {
            "ssid": ssid,
            "ok": ok,
            "detail": detail,
            "at": now if now is not None else time.time(),
        }


def validate_ssid(value: str) -> str | None:
    """Check a typed network name.  Returns an error sentence, or ``None``.

    Length is checked in bytes because that is what 802.11 counts, and a
    network named in Chinese hits 32 bytes at eleven characters.
    """
    text = value.strip()
    if not text:
        return "Choose a network, or type its name."
    if len(text.encode("utf-8")) > MAX_SSID_BYTES:
        return "That network name is too long to be a real one."
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return "That network name has characters in it that cannot be part of a name."
    return None


def validate_password(value: str, *, network: Network | None) -> str | None:
    """Check a typed passphrase against what the network appears to need."""
    if network is not None and network.open:
        return None
    if not value:
        return "Type the WiFi password."
    if len(value) < 8:
        return "A WiFi password is at least 8 characters. Check for a missing character."
    if len(value) > MAX_PASSWORD_LEN:
        return f"A WiFi password is at most {MAX_PASSWORD_LEN} characters."
    return None


# ---------------------------------------------------------------- rendering

#: Inlined because the device reading it has no internet.  Big touch targets
#: and a 16px minimum font size, which is also what stops iOS zooming the
#: whole page when a field is focused.
STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px 16px 48px;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #faf8f4; color: #241c14;
}
@media (prefers-color-scheme: dark) {
  body { background: #17130f; color: #efe7dc; }
  .card { background: #211b15 !important; border-color: #3a2f24 !important; }
  input, select { background: #17130f !important; color: #efe7dc !important;
                  border-color: #4a3c2d !important; }
  .muted { color: #a89883 !important; }
}
.wrap { max-width: 32rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 24px 0 8px; }
p { margin: 0 0 12px; }
.muted { color: #6d6155; font-size: 0.9rem; }
.card { background: #fff; border: 1px solid #e3dbcf; border-radius: 12px;
        padding: 16px; margin: 16px 0; }
label { display: block; font-weight: 600; margin: 14px 0 6px; }
select, input[type=text], input[type=password] {
  width: 100%; padding: 12px; font-size: 16px; border-radius: 8px;
  border: 1px solid #c9bfb0; background: #fff; color: inherit;
}
button { width: 100%; padding: 14px; font-size: 17px; font-weight: 600;
         border: 0; border-radius: 10px; background: #7a2e2e; color: #fff;
         margin-top: 20px; }
.err { background: #fdeaea; border: 1px solid #e4b4b4; color: #7a2020;
       border-radius: 8px; padding: 12px; margin: 0 0 16px; }
.good { background: #eaf5ea; border: 1px solid #b4d8b4; color: #205020;
        border-radius: 8px; padding: 12px; margin: 0 0 16px; }
@media (prefers-color-scheme: dark) {
  .err { background: #3a1d1d; border-color: #6d3434; color: #f3c9c9; }
  .good { background: #1d3a22; border-color: #35663f; color: #c9f0d2; }
}
.row { display: flex; align-items: center; gap: 8px; margin-top: 10px;
       font-weight: 400; }
.row input { width: auto; }
ol { padding-left: 1.3rem; } li { margin-bottom: 8px; }
code { background: rgba(127,127,127,.18); padding: 1px 5px; border-radius: 4px; }
"""


def page(title: str, body: str) -> str:
    """Wrap body HTML in the document shell."""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    )


def _age(seconds: float | None, now: float) -> str:
    """"4 minutes ago", for a timestamp a person is judging freshness by."""
    if seconds is None:
        return "never"
    delta = max(0.0, now - seconds)
    if delta < 90:
        return "just now"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(delta // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def render_form(
    state: PortalState,
    settings: HotspotSettings,
    *,
    error: str | None = None,
    now: float | None = None,
) -> str:
    """The main page: pick a network, type the password."""
    now = now if now is not None else time.time()
    parts: list[str] = [
        "<h1>Weasley Clock</h1>",
        "<p class=muted>WiFi setup</p>",
    ]

    if error:
        parts.append(f"<div class=err>{html.escape(error)}</div>")

    attempt = state.last_attempt
    if attempt is not None and not attempt["ok"]:
        parts.append(
            "<div class=err><strong>The last try did not work.</strong><br>"
            "{}: {}.<br>Have another go. The most likely reason is a typo in "
            "the password.</div>".format(
                html.escape(str(attempt["ssid"])), html.escape(str(attempt["detail"]))
            )
        )

    parts.append(
        "<div class=card><p>The clock cannot get onto the WiFi, so it has put up "
        "this network for you to fix it from.</p>"
        "<p class=muted>Pick the house WiFi below and type its password. The clock "
        "will join it and this setup network will disappear.</p></div>"
    )

    parts.append("<form method=post action=/join>")

    if state.networks:
        parts.append("<label for=ssid>WiFi network</label><select id=ssid name=ssid>")
        for network in state.networks:
            bars = "•" * max(1, min(4, round(network.signal / 25))) if network.signal else ""
            lock = "" if network.open else " \U0001f512"
            value = html.escape(network.ssid, quote=True)
            shown = html.escape(network.ssid)
            parts.append(f'<option value="{value}">{shown}{lock} {bars}</option>')
        parts.append('<option value="">(type a name instead)</option></select>')
        parts.append(
            "<p class=muted style='margin-top:6px'>Networks the clock could see "
            f"{html.escape(_age(state.scanned_at, now))}. It cannot look again while "
            "this setup network is up, so if the one you want is missing, type it in "
            "below.</p>"
        )
        parts.append(
            "<label for=other>Or type the network name</label>"
            "<input id=other name=other type=text autocapitalize=none "
            'autocorrect=off spellcheck=false placeholder="exactly as it is spelled">'
        )
    else:
        parts.append(
            "<p class=muted>The clock has no list of nearby networks, so type the "
            "name exactly as it is spelled, capital letters and all.</p>"
            "<label for=other>WiFi network name</label>"
            "<input id=other name=other type=text autocapitalize=none "
            "autocorrect=off spellcheck=false>"
        )

    parts.append(
        "<label for=password>WiFi password</label>"
        "<input id=password name=password type=password autocapitalize=none "
        "autocorrect=off spellcheck=false>"
        "<label class=row for=show><input id=show type=checkbox "
        "onclick=\"document.getElementById('password').type="
        "this.checked?'text':'password'\"> Show the password</label>"
        "<button type=submit>Connect the clock</button>"
        "</form>"
    )

    # The wait is quoted from the settings rather than written into the copy,
    # so the page cannot drift out of step with what the monitor actually
    # does if anyone retunes the timings in config/hotspot.yaml.
    minutes = max(1, round((settings.hotspot_idle_s + settings.retry_wifi_s) / 60))
    parts.append(
        "<h2>If this does not work</h2>"
        f"<p class=muted>Leave it alone for about {minutes} minutes. The clock keeps "
        "trying the networks it already knows, and puts this setup network back up if "
        "none of them answer, so you can always come back and try again. Nothing you "
        "do here can stop the clock working once it is back on the WiFi.</p>"
    )
    return page("Weasley Clock WiFi setup", "".join(parts))


def render_submitted(ssid: str, settings: HotspotSettings) -> str:
    """The page shown immediately after a join is accepted.

    This is the last thing the phone will successfully load, because the next
    thing that happens is the access point going away.  It therefore has to
    contain the whole rest of the procedure, including what to do if it fails,
    with no expectation that anything further can be fetched.
    """
    body = (
        "<h1>Trying now</h1>"
        f"<div class=good>The clock is joining <strong>{html.escape(ssid)}</strong>.</div>"
        "<div class=card><p>This setup network is about to disappear. The clock has only "
        "one WiFi radio, so it cannot run this network and join yours at the same "
        "time. Your phone will jump back to its usual WiFi or to mobile data.</p>"
        "<p><strong>That is what success looks like.</strong> There is no page to wait "
        "for.</p></div>"
        "<h2>How to tell</h2><ol>"
        f"<li><strong>If <code>{html.escape(settings.ssid)}</code> stays gone</strong>, "
        "it worked. The clock is on the WiFi and the hands will start moving again "
        "within a minute or two.</li>"
        f"<li><strong>If <code>{html.escape(settings.ssid)}</code> comes back</strong> "
        "in the next few minutes, the password was not accepted. Join it again and "
        "this page will tell you what went wrong, so you can try another one.</li>"
        "</ol>"
        "<p class=muted>You can close this page now.</p>"
    )
    return page("Trying now", body)


# -------------------------------------------------------------------- app


def build_app(
    state: PortalState,
    settings: HotspotSettings,
    manager: NetworkManager,
    *,
    on_join: Any = None,
    now: Any = time.time,
) -> FastAPI:
    """Build the portal application.

    Args:
        state: Shared with the service, which reads ``last_seen``.
        settings: For the SSID shown in the copy and the join priority.
        manager: Used to perform the join.
        on_join: Optional ``async (ssid, outcome) -> None`` called after a
            join attempt finishes, so the service can tell the monitor and
            take the access point down.
        now: Time source, injected so tests do not have to wait.

    Returns:
        The FastAPI app.  It is deliberately built fresh each time the access
        point goes up, so a stale scan list cannot be served.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _mark_activity(request: Request, call_next):
        # Every request counts as somebody being at the clock, including the
        # captive-portal probes a phone sends on its own. This is the signal
        # the monitor uses to keep the access point up, so it has to be set
        # before the handler runs, not after: a handler that takes a while
        # must not look like silence.
        state.touch(now())
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(render_form(state, settings, now=now()))

    @app.get("/status", response_class=JSONResponse)
    async def status() -> JSONResponse:
        # No secrets here: profile names and signal strengths only. nmcli is
        # never asked for --show-secrets anywhere in this package.
        return JSONResponse(
            {
                "ssid": settings.ssid,
                "scanned_at": state.scanned_at,
                "networks": [network.ssid for network in state.networks],
                "saved": [name for name, _ in await manager.wifi_profiles()],
                "attempts": state.attempts,
                "joining": state.joining,
                "last_attempt": state.last_attempt,
            }
        )

    @app.post("/join", response_class=HTMLResponse)
    async def join(
        ssid: str = Form(""),
        other: str = Form(""),
        password: str = Form(""),
    ) -> HTMLResponse:
        # "Other" wins when it has been filled in, because somebody who typed
        # a name while a dropdown was also set meant the one they typed.
        wanted = other.strip() or ssid.strip()

        problem = validate_ssid(wanted)
        if problem is not None:
            return HTMLResponse(render_form(state, settings, error=problem, now=now()), 400)

        known = next((net for net in state.networks if net.ssid == wanted), None)
        problem = validate_password(password, network=known)
        if problem is not None:
            return HTMLResponse(render_form(state, settings, error=problem, now=now()), 400)

        if state.joining is not None:
            return HTMLResponse(
                render_form(
                    state,
                    settings,
                    error=f"Already trying to join {state.joining}. Give it a minute.",
                    now=now(),
                ),
                409,
            )

        state.joining = wanted
        # Run the join *after* the response. Doing it inline would mean the
        # access point coming down mid-response and the phone showing a
        # connection error for what is actually a success.
        asyncio.get_running_loop().create_task(
            _attempt(state, settings, manager, wanted, password, on_join, now),
            name="hotspot-join",
        )
        return HTMLResponse(render_submitted(wanted, settings))

    for path in DETECTION_PATHS:
        # Answering a connectivity probe with a redirect instead of the
        # payload the OS expects is what triggers the "sign in to network"
        # notification. Returning the real success payload here would make
        # the phone decide it has internet and show nothing at all.
        app.add_api_route(
            path,
            _redirect_to_portal,
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )

    @app.api_route(
        "/{_path:path}", methods=["GET", "HEAD"], include_in_schema=False
    )
    async def catch_all(_path: str) -> RedirectResponse:
        # Whatever else the phone asks for, send it to the one page there is.
        return RedirectResponse("/", status_code=302)

    return app


async def _redirect_to_portal() -> RedirectResponse:
    """Send a connectivity probe to the setup page."""
    return RedirectResponse("/", status_code=302)


async def _attempt(
    state: PortalState,
    settings: HotspotSettings,
    manager: NetworkManager,
    ssid: str,
    password: str,
    on_join: Any,
    now: Any,
) -> None:
    """Perform a join in the background and record what happened."""
    log.info("hotspot: trying to join %r on request from the portal", ssid)
    try:
        outcome = await asyncio.wait_for(
            manager.join(ssid, password, priority=settings.join_priority),
            timeout=settings.join_timeout_s,
        )
    except TimeoutError:
        state.record(ssid, False, "the clock gave up waiting for that network", now())
        log.warning("hotspot: join to %r timed out", ssid)
        return
    except Exception as exc:  # pragma: no cover - nmcli missing or killed
        state.record(ssid, False, f"the join could not be run ({exc})", now())
        log.warning("hotspot: join to %r could not be run: %s", ssid, exc)
        return

    state.record(ssid, outcome.ok, outcome.detail, now())
    if outcome.ok:
        log.info("hotspot: joined %r", ssid)
    else:
        log.warning("hotspot: join to %r failed: %s", ssid, outcome.detail)
    if on_join is not None:
        result = on_join(ssid, outcome)
        if result is not None:
            await result
