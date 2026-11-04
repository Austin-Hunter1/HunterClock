"""The FastAPI application: pages, JSON API and the live WebSocket.

No Node build step and no CDN: the templates are Jinja2, the JavaScript is
vendored in ``static/``, and everything works on a Pi with no internet.  That
matters because the clock will sit in a house in another state behind a
Tailscale address, and a UI that needs to fetch a framework from the open
internet is a UI that fails exactly when the network is the thing you are trying
to debug.

Every endpoint that moves hardware is logged through the engine's event log with
a timestamp, and every page renders with the Pico disconnected -- it says so in
the banner rather than erroring.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import config as config_module
from ..config import ClockConfig, ConfigError, dump_config, parse_config
from ..engine import Engine
from ..picolink import CommandFailed, LinkError
from .auth import COOKIE_NAME, Authenticator

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

#: How often the WebSocket pushes a fresh snapshot, in seconds.
PUSH_INTERVAL_S = 1.0

#: Pages in the navigation, in order.
NAV = [
    ("dashboard", "/", "Dashboard"),
    ("control", "/control", "Control"),
    ("assist", "/assist", "Assist"),
    ("simulate", "/simulate", "Simulate"),
    ("logs", "/logs", "Logs"),
    ("config", "/config", "Config"),
    ("system", "/system", "System"),
]


# -- request bodies ----------------------------------------------------------


class JogBody(BaseModel):
    """Nudge one hand."""

    person: str
    degrees: float = Field(..., ge=-360.0, le=360.0)


class MoveBody(BaseModel):
    """Send one hand to an absolute dial angle."""

    person: str
    angle_deg: float


class PersonBody(BaseModel):
    """An action that names one person."""

    person: str


class VerifyBody(BaseModel):
    """Mark a hand verified or not."""

    person: str
    verified: bool = True


class EnableBody(BaseModel):
    """Enable or disable the drivers."""

    on: bool


class ParamsBody(BaseModel):
    """Change speed and acceleration."""

    max_speed: float | None = Field(None, gt=0)
    accel: float | None = Field(None, gt=0)


class OverrideBody(BaseModel):
    """Set a fake location or distance."""

    person: str
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    distance_km: float | None = Field(None, ge=0)
    ttl_s: float | None = Field(None, gt=0)
    note: str = ""


class SourceBody(BaseModel):
    """Switch a location source on or off."""

    name: str
    enabled: bool


class ScenarioBody(BaseModel):
    """Play a scripted scenario."""

    name: str
    person: str | None = None
    seconds: float = Field(60.0, gt=0, le=3600.0)


class ConfigBody(BaseModel):
    """A proposed configuration document."""

    text: str


def create_app(
    engine: Engine,
    *,
    sim: bool = False,
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> FastAPI:
    """Build the FastAPI application around a running :class:`Engine`.

    Args:
        engine: The engine the UI drives.
        sim: True in simulation mode, which relaxes auth and shows a banner.
        config_path: Where the Config page saves to.
        repo_root: Checkout root, for the update and diagnostics endpoints.

    Returns:
        The configured application.
    """
    config = engine.config
    auth = Authenticator.from_config(config, sim=sim)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    app = FastAPI(title=config.web.title, docs_url=None, redoc_url=None)
    app.state.engine = engine
    app.state.auth = auth
    app.state.sim = sim
    app.state.config_path = config_path or config_module.default_config_path()
    app.state.repo_root = repo_root or config_module.repo_root()
    app.state.scenario_task: asyncio.Task | None = None

    STATIC.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # -- auth helpers ----------------------------------------------------

    def logged_in(request: Request) -> bool:
        """Does this request carry a valid session?"""
        return auth.valid(request.cookies.get(COOKIE_NAME))

    def require_session(request: Request) -> None:
        """Dependency that rejects unauthenticated API calls."""
        if not logged_in(request):
            raise HTTPException(status_code=401, detail="not signed in")

    def page(request: Request, name: str, template: str, **context: Any) -> Response:
        """Render a page, redirecting to the login form when signed out."""
        if not logged_in(request):
            return RedirectResponse("/login?next=%s" % request.url.path, status_code=303)
        return templates.TemplateResponse(
            request,
            template,
            {
                "nav": NAV,
                "active": name,
                "title": config.web.title,
                "sim": sim,
                "open_access": auth.open_access,
                "site": config.site,
                **context,
            },
        )

    # -- login -----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/") -> Response:
        """The login form."""
        if logged_in(request):
            return RedirectResponse(next or "/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": config.web.title, "next": next, "error": None, "sim": sim},
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(
        request: Request, password: str = Form(""), next: str = Form("/")
    ) -> Response:
        """Check the password and set the session cookie."""
        if not auth.check(password):
            engine.events.add("error", "failed UI login attempt")
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "title": config.web.title,
                    "next": next,
                    "error": "That password is not right.",
                    "sim": sim,
                },
                status_code=401,
            )
        response = RedirectResponse(next or "/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            auth.issue(),
            httponly=True,
            samesite="lax",
            max_age=int(config.web.session_hours * 3600),
            path="/",
        )
        engine.events.system("UI login")
        return response

    @app.post("/logout")
    async def logout() -> Response:
        """Clear the session cookie."""
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # -- pages -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        """The clock face and the state table."""
        return page(request, "dashboard", "dashboard.html")

    @app.get("/control", response_class=HTMLResponse)
    async def control(request: Request) -> Response:
        """Per-hand jog, move, zero and the stop controls."""
        return page(request, "control", "control.html")

    @app.get("/assist", response_class=HTMLResponse)
    async def assist(request: Request) -> Response:
        """The guided zeroing wizard, for someone standing at the clock."""
        return page(request, "assist", "assist.html")

    @app.get("/simulate", response_class=HTMLResponse)
    async def simulate(request: Request) -> Response:
        """Fake locations, scenarios and source switches."""
        return page(request, "simulate", "simulate.html")

    @app.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request) -> Response:
        """Live event log."""
        return page(request, "logs", "logs.html")

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request) -> Response:
        """View and edit the YAML."""
        return page(request, "config", "config.html")

    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request) -> Response:
        """Host health, firmware upload, update and diagnostics."""
        return page(request, "system", "system.html")

    # -- state -----------------------------------------------------------

    @app.get("/api/state", dependencies=[Depends(require_session)])
    async def api_state() -> JSONResponse:
        """One snapshot of everything the UI shows."""
        return JSONResponse(engine.snapshot())

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        """Push a snapshot once a second for as long as the page is open."""
        if not auth.valid(socket.cookies.get(COOKIE_NAME)):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                await socket.send_json(engine.snapshot())
                await asyncio.sleep(PUSH_INTERVAL_S)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:
            with contextlib.suppress(Exception):
                await socket.close()

    # -- hardware actions ------------------------------------------------

    def hardware_error(exc: Exception) -> JSONResponse:
        """Turn a link failure into a 503 the UI can show plainly."""
        message = str(exc)
        engine.events.error("action failed: %s" % message)
        return JSONResponse({"ok": False, "error": message}, status_code=503)

    @app.post("/api/jog", dependencies=[Depends(require_session)])
    async def api_jog(body: JogBody) -> JSONResponse:
        """Nudge one hand by a number of degrees."""
        try:
            await engine.jog(body.person, body.degrees)
        except KeyError:
            raise HTTPException(404, "no such person")
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/move", dependencies=[Depends(require_session)])
    async def api_move(body: MoveBody) -> JSONResponse:
        """Send one hand to an absolute angle."""
        try:
            await engine.move_to_angle(body.person, body.angle_deg)
        except KeyError:
            raise HTTPException(404, "no such person")
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/zero", dependencies=[Depends(require_session)])
    async def api_zero(body: PersonBody) -> JSONResponse:
        """Declare that this hand is at 12 o'clock, and mark it verified."""
        try:
            await engine.set_zero(body.person)
        except KeyError:
            raise HTTPException(404, "no such person")
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/verify", dependencies=[Depends(require_session)])
    async def api_verify(body: VerifyBody) -> JSONResponse:
        """Mark a hand verified or unverified without moving it."""
        try:
            await engine.set_verified(body.person, body.verified)
        except KeyError:
            raise HTTPException(404, "no such person")
        return JSONResponse({"ok": True})

    @app.post("/api/enable", dependencies=[Depends(require_session)])
    async def api_enable(body: EnableBody) -> JSONResponse:
        """Enable or disable all six drivers."""
        try:
            await engine.set_enabled(body.on)
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/stop", dependencies=[Depends(require_session)])
    async def api_stop() -> JSONResponse:
        """Decelerate every hand."""
        try:
            await engine.stop_all()
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/estop", dependencies=[Depends(require_session)])
    async def api_estop() -> JSONResponse:
        """Halt every hand at once; every hand becomes unverified."""
        try:
            await engine.estop()
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/all-to-twelve", dependencies=[Depends(require_session)])
    async def api_all_to_twelve() -> JSONResponse:
        """Send every hand to 12 o'clock."""
        try:
            moved = await engine.move_all_to_twelve()
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True, "moved": moved})

    @app.post("/api/resume-live", dependencies=[Depends(require_session)])
    async def api_resume_live() -> JSONResponse:
        """Send every verified hand to its live target."""
        try:
            moved = await engine.resume_live()
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True, "moved": moved})

    @app.post("/api/sweep", dependencies=[Depends(require_session)])
    async def api_sweep(body: PersonBody) -> JSONResponse:
        """Run one hand a full turn and back, slowly."""
        try:
            engine.person(body.person)
        except KeyError:
            raise HTTPException(404, "no such person")
        # A sweep takes tens of seconds; run it in the background so the button
        # returns at once and the dashboard shows the hand moving.
        asyncio.create_task(_guarded(engine.test_sweep(body.person), engine))
        return JSONResponse({"ok": True, "started": True})

    @app.post("/api/params", dependencies=[Depends(require_session)])
    async def api_params(body: ParamsBody) -> JSONResponse:
        """Change speed and acceleration within the configured ceilings."""
        try:
            await engine.set_motion_params(body.max_speed, body.accel)
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    # -- simulate --------------------------------------------------------

    @app.post("/api/override", dependencies=[Depends(require_session)])
    async def api_override(body: OverrideBody) -> JSONResponse:
        """Put a fake position or distance on one hand."""
        if body.lat is None and body.lon is None and body.distance_km is None:
            raise HTTPException(400, "give lat and lon, or distance_km")
        if (body.lat is None) != (body.lon is None):
            raise HTTPException(400, "lat and lon must be given together")
        try:
            override = await engine.set_override(
                body.person,
                lat=body.lat,
                lon=body.lon,
                distance_km=body.distance_km,
                ttl_s=body.ttl_s,
                note=body.note,
            )
        except KeyError:
            raise HTTPException(404, "no such person")
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True, "override": override.to_dict()})

    @app.post("/api/override/clear", dependencies=[Depends(require_session)])
    async def api_override_clear(body: PersonBody) -> JSONResponse:
        """Drop a manual override."""
        try:
            await engine.clear_override(body.person)
        except KeyError:
            raise HTTPException(404, "no such person")
        except (LinkError, CommandFailed) as exc:
            return hardware_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/source", dependencies=[Depends(require_session)])
    async def api_source(body: SourceBody) -> JSONResponse:
        """Switch a location source on or off."""
        try:
            enabled = engine.set_source_enabled(body.name, body.enabled)
        except KeyError:
            raise HTTPException(404, "no such source")
        return JSONResponse({"ok": True, "enabled": enabled})

    @app.post("/api/scenario", dependencies=[Depends(require_session)])
    async def api_scenario(body: ScenarioBody) -> JSONResponse:
        """Play a scripted scenario, such as 'Evan flies home' over 60 s."""
        from ..scenarios import SCENARIOS, run_scenario

        if body.name not in SCENARIOS:
            raise HTTPException(404, "no scenario named %r" % body.name)
        task = app.state.scenario_task
        if task is not None and not task.done():
            task.cancel()
        app.state.scenario_task = asyncio.create_task(
            _guarded(run_scenario(engine, body.name, body.person, body.seconds), engine)
        )
        return JSONResponse({"ok": True, "started": body.name})

    @app.get("/api/scenarios", dependencies=[Depends(require_session)])
    async def api_scenarios() -> JSONResponse:
        """List the available scenarios."""
        from ..scenarios import SCENARIOS

        return JSONResponse(
            {
                "scenarios": [
                    {"name": name, "title": s["title"], "needs_person": s["needs_person"]}
                    for name, s in SCENARIOS.items()
                ]
            }
        )

    # -- logs ------------------------------------------------------------

    @app.get("/api/logs", dependencies=[Depends(require_session)])
    async def api_logs(
        limit: int = 200,
        categories: str = "",
        since_id: int = 0,
        search: str = "",
    ) -> JSONResponse:
        """Recent events, filterable, for the Logs page."""
        wanted = [c for c in categories.split(",") if c] or None
        return JSONResponse(
            {
                "events": engine.events.tail(
                    limit=min(max(limit, 1), 1000),
                    categories=wanted,
                    since_id=since_id or None,
                    search=search or None,
                ),
                "last_id": engine.events.last_id(),
                "counts": engine.events.counts(),
                "mqtt": engine.mqtt_history(60),
            }
        )

    @app.get("/api/logs/download", dependencies=[Depends(require_session)])
    async def api_logs_download() -> Response:
        """The whole in-memory log as a text file."""
        return Response(
            engine.events.as_text(),
            media_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="clockd-events.log"'},
        )

    # -- config ----------------------------------------------------------

    @app.get("/api/config", dependencies=[Depends(require_session)])
    async def api_config_get() -> JSONResponse:
        """The configuration as YAML text, plus where it came from."""
        path = Path(app.state.config_path)
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            source = str(path)
        else:
            example = config_module.example_config_path()
            text = example.read_text(encoding="utf-8") if example.is_file() else dump_config(config)
            source = "%s (example; %s does not exist yet)" % (example, path)
        return JSONResponse({"text": text, "path": str(path), "source": source})

    @app.post("/api/config/validate", dependencies=[Depends(require_session)])
    async def api_config_validate(body: ConfigBody) -> JSONResponse:
        """Validate proposed YAML and show what would change."""
        try:
            proposed = parse_config(config_module.load_yaml(body.text))
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse(
            {
                "ok": True,
                "diff": _diff(dump_config(config), dump_config(proposed)),
                "summary": _config_summary(proposed),
            }
        )

    @app.post("/api/config/save", dependencies=[Depends(require_session)])
    async def api_config_save(body: ConfigBody) -> JSONResponse:
        """Validate, back up the current file, then write the new one.

        The controller is not reloaded in place: config touches the serial port,
        the broker and the people list, and a half-applied change on a clock in
        another state is worse than a restart.  The UI tells the user to restart
        and offers the button.
        """
        try:
            parse_config(config_module.load_yaml(body.text))
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        path = Path(app.state.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if path.is_file():
            backup = path.with_name("%s.%s.bak" % (path.name, time.strftime("%Y%m%d-%H%M%S")))
            shutil.copy2(path, backup)
        path.write_text(body.text, encoding="utf-8")
        engine.events.action(
            "configuration saved to %s%s"
            % (path, " (backup %s)" % backup.name if backup else "")
        )
        return JSONResponse(
            {
                "ok": True,
                "path": str(path),
                "backup": str(backup) if backup else None,
                "restart_required": True,
            }
        )

    # -- system ----------------------------------------------------------

    @app.get("/api/system", dependencies=[Depends(require_session)])
    async def api_system() -> JSONResponse:
        """Host health: temperature, disk, uptime, services, Tailscale."""
        return JSONResponse(collect_system_info(engine, app.state.repo_root))

    @app.get("/api/system/diagnostics", dependencies=[Depends(require_session)])
    async def api_diagnostics() -> Response:
        """A zip with logs, versions and a sanitised config.

        One click gives everything needed to debug the clock from Boulder.
        The config is sanitised on the way out: every value whose key looks like
        a secret is replaced, and the environment is reported as names only.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("snapshot.json", json.dumps(engine.snapshot(), indent=2, default=str))
            archive.writestr("events.log", engine.events.as_text())
            archive.writestr(
                "system.json",
                json.dumps(collect_system_info(engine, app.state.repo_root), indent=2, default=str),
            )
            archive.writestr("config.sanitised.yaml", _sanitised_config_text(app.state.config_path))
            archive.writestr("store.json", json.dumps(engine.store.all_people(), indent=2))
            archive.writestr(
                "mqtt-recent.json", json.dumps(engine.mqtt_history(200), indent=2, default=str)
            )
            archive.writestr("environment.txt", _environment_names())
        buffer.seek(0)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        engine.events.action("diagnostic bundle downloaded")
        return Response(
            buffer.read(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="weasley-clock-%s.zip"' % stamp
            },
        )

    @app.post("/api/system/restart", dependencies=[Depends(require_session)])
    async def api_restart() -> JSONResponse:
        """Ask systemd to restart the controller.

        The process exits; ``Restart=always`` brings it back. Running under a
        plain shell, this just stops, which the response says.
        """
        engine.events.action("restart requested from the UI")
        managed = bool(os.environ.get("INVOCATION_ID"))
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))
        return JSONResponse(
            {
                "ok": True,
                "managed_by_systemd": managed,
                "message": "Restarting now."
                if managed
                else "Exiting now. Without systemd, start it again yourself.",
            }
        )

    @app.post("/api/system/update", dependencies=[Depends(require_session)])
    async def api_update() -> JSONResponse:
        """``git pull`` in the checkout, then restart.

        Records the commit it was on first, so Rollback has somewhere to go.
        """
        root = Path(app.state.repo_root)
        before = _git(root, "rev-parse", "HEAD")
        if before is None:
            raise HTTPException(400, "%s is not a git checkout" % root)
        (root / "var").mkdir(exist_ok=True)
        (root / "var" / "rollback-commit").write_text(before.strip(), encoding="utf-8")
        result = _run(["git", "pull", "--ff-only"], cwd=root)
        after = _git(root, "rev-parse", "HEAD")
        engine.events.action(
            "update from GitHub: %s -> %s" % ((before or "?")[:8], (after or "?")[:8]),
            ok=result["ok"],
        )
        return JSONResponse(
            {
                "ok": result["ok"],
                "before": (before or "").strip(),
                "after": (after or "").strip(),
                "output": result["output"],
                "restart_required": result["ok"] and before != after,
            }
        )

    @app.post("/api/system/rollback", dependencies=[Depends(require_session)])
    async def api_rollback() -> JSONResponse:
        """Go back to the commit recorded by the last update."""
        root = Path(app.state.repo_root)
        marker = root / "var" / "rollback-commit"
        if not marker.is_file():
            raise HTTPException(400, "no recorded commit to roll back to")
        commit = marker.read_text(encoding="utf-8").strip()
        result = _run(["git", "checkout", "--detach", commit], cwd=root)
        engine.events.action("rolled back to %s" % commit[:8], ok=result["ok"])
        return JSONResponse(
            {"ok": result["ok"], "commit": commit, "output": result["output"],
             "restart_required": result["ok"]}
        )

    @app.post("/api/firmware/upload", dependencies=[Depends(require_session)])
    async def api_firmware_upload() -> JSONResponse:
        """Copy the firmware onto the Pico with mpremote.

        The link is closed first, because mpremote and the controller cannot
        both own ``/dev/ttyACM0``; it reconnects by itself afterwards.

        UNTESTED ON HARDWARE.
        """
        if sim:
            raise HTTPException(400, "not available in sim mode: there is no Pico")
        if shutil.which("mpremote") is None:
            raise HTTPException(400, "mpremote is not installed on the Pi")
        root = Path(app.state.repo_root)
        files = ["motion.py", "protocol.py", "core.py", "main.py"]
        port = config.serial.port
        engine.events.action("firmware upload started (%s)" % ", ".join(files))
        await engine.link.close()
        outputs = []
        ok = True
        try:
            for name in files:
                source = root / "firmware" / "pico" / name
                if not source.is_file():
                    outputs.append("missing %s" % source)
                    ok = False
                    break
                result = _run(
                    ["mpremote", "connect", port, "fs", "cp", str(source), ":%s" % name],
                    cwd=root,
                    timeout=60,
                )
                outputs.append(result["output"])
                if not result["ok"]:
                    ok = False
                    break
            if ok:
                outputs.append(_run(["mpremote", "connect", port, "reset"], cwd=root)["output"])
        finally:
            await engine.link.start()
        engine.events.action("firmware upload %s" % ("succeeded" if ok else "FAILED"), ok=ok)
        return JSONResponse({"ok": ok, "output": "\n".join(outputs), "files": files})

    return app


async def _guarded(coro, engine: Engine) -> None:
    """Run a background coroutine, logging anything it raises."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        engine.events.error("background task failed: %s" % exc)
        log.exception("background task failed")


# -- helpers -----------------------------------------------------------------


def _run(command: list[str], cwd: Path | None = None, timeout: float = 120.0) -> dict[str, Any]:
    """Run a command and capture its output, never raising."""
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "output": "%s: %s" % (" ".join(command), exc)}
    output = (completed.stdout or "") + (completed.stderr or "")
    return {"ok": completed.returncode == 0, "output": output.strip(), "code": completed.returncode}


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command, returning its output or ``None``."""
    result = _run(["git", *args], cwd=root, timeout=20)
    return result["output"] if result["ok"] else None


def _read_first_line(path: str) -> str | None:
    """Read one line from a sysfs-style file, or ``None``."""
    try:
        return Path(path).read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return None


def collect_system_info(engine: Engine, repo_root: Path) -> dict[str, Any]:
    """Host health for the System page.

    Everything here degrades to ``None`` off a Pi, so the page works on a
    laptop in sim mode too.
    """
    info: dict[str, Any] = {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "controller_uptime_s": round(time.time() - engine.started_at, 1),
        "pid": os.getpid(),
        "systemd": bool(os.environ.get("INVOCATION_ID")),
    }

    temperature = _read_first_line("/sys/class/thermal/thermal_zone0/temp")
    if temperature and temperature.isdigit():
        info["cpu_temp_c"] = round(int(temperature) / 1000.0, 1)
    else:
        info["cpu_temp_c"] = None

    uptime = _read_first_line("/proc/uptime")
    info["host_uptime_s"] = round(float(uptime.split()[0]), 1) if uptime else None

    try:
        usage = shutil.disk_usage(str(repo_root))
        info["disk"] = {
            "total_gb": round(usage.total / 1e9, 2),
            "free_gb": round(usage.free / 1e9, 2),
            "used_percent": round(100.0 * (usage.total - usage.free) / usage.total, 1),
        }
    except OSError:
        info["disk"] = None

    try:
        load = os.getloadavg()
        info["load"] = [round(v, 2) for v in load]
    except (OSError, AttributeError):
        info["load"] = None

    info["git"] = {
        "commit": (_git(repo_root, "rev-parse", "--short", "HEAD") or "").strip() or None,
        "branch": (_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None,
        "dirty": bool((_git(repo_root, "status", "--porcelain") or "").strip()),
        "rollback_available": (repo_root / "var" / "rollback-commit").is_file(),
    }

    tailscale = shutil.which("tailscale")
    if tailscale:
        result = _run([tailscale, "status", "--json"], timeout=10)
        if result["ok"]:
            try:
                status = json.loads(result["output"])
                info["tailscale"] = {
                    "backend_state": status.get("BackendState"),
                    "self": (status.get("Self") or {}).get("DNSName"),
                    "online": (status.get("Self") or {}).get("Online"),
                }
            except ValueError:
                info["tailscale"] = {"backend_state": "unparseable"}
        else:
            info["tailscale"] = {"backend_state": "error", "detail": result["output"][:200]}
    else:
        info["tailscale"] = None

    wifi = _read_first_line("/proc/net/wireless")
    info["wifi"] = None
    try:
        lines = Path("/proc/net/wireless").read_text(encoding="utf-8").splitlines()
        for line in lines[2:]:
            parts = line.split()
            if len(parts) >= 4:
                info["wifi"] = {
                    "interface": parts[0].rstrip(":"),
                    "link": parts[2].rstrip("."),
                    "signal_dbm": parts[3].rstrip("."),
                }
                break
    except OSError:
        pass
    if info["wifi"] is None and wifi is None:
        info["wifi"] = None

    services = {}
    if shutil.which("systemctl"):
        for unit in ("clockd", "mosquitto", "tailscaled"):
            result = _run(["systemctl", "is-active", unit], timeout=10)
            services[unit] = result["output"] or "unknown"
    info["services"] = services

    info["mpremote"] = shutil.which("mpremote")
    info["serial_port_present"] = Path(engine.config.serial.port).exists()
    return info


def _sanitised_config_text(path: Path | str) -> str:
    """Read the config and blank anything that looks like a secret.

    The diagnostic bundle is meant to be sent to whoever is helping, so it must
    be safe to send.  Keys are matched by name, and because the schema keeps
    real secrets in the environment, this is belt and braces rather than the
    only protection.
    """
    source = Path(path)
    if not source.is_file():
        example = config_module.example_config_path()
        source = example if example.is_file() else source
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        return "# could not read %s: %s\n" % (source, exc)

    secret_words = ("password", "secret", "token", "key", "psk", "passphrase")
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in line:
            out.append(line)
            continue
        key = line.split(":", 1)[0].strip().lstrip("- ").lower()
        value = line.split(":", 1)[1].strip()
        # Keep "*_env" keys: they name an environment variable, not a secret.
        if any(word in key for word in secret_words) and not key.endswith("_env") and value:
            indent = line[: len(line) - len(line.lstrip())]
            out.append("%s%s: \"<removed for sharing>\"" % (indent, line.split(":", 1)[0].strip()))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _environment_names() -> str:
    """Report which relevant environment variables are set, never their values."""
    interesting = (
        "MQTT_PASSWORD",
        "CLOCK_UI_PASSWORD",
        "CLOCK_SECRET_KEY",
        "HASS_TOKEN",
        "HEALTHCHECKS_URL",
        "NTFY_URL",
        "CLOCK_CONFIG",
        "INVOCATION_ID",
    )
    lines = ["Environment variables (names and whether they are set; never values)", ""]
    for name in interesting:
        lines.append("%-20s %s" % (name, "set" if os.environ.get(name) else "not set"))
    return "\n".join(lines) + "\n"


def _config_summary(config: ClockConfig) -> dict[str, Any]:
    """A short description of a proposed configuration, for the diff view."""
    return {
        "site": config.site,
        "home": "%s (%.4f, %.4f)" % (config.home.name, config.home.lat, config.home.lon),
        "people": ["%s (level %d)" % (p.name, p.level) for p in config.people],
        "max_speed": config.motion.max_speed,
        "sources": [
            name
            for name in ("owntracks", "generic_mqtt", "hass", "manual")
            if getattr(getattr(config.sources, name), "enabled", False)
        ],
    }


def _diff(before: str, after: str) -> list[dict[str, Any]]:
    """A unified diff between two YAML documents, as structured lines."""
    import difflib

    out = []
    for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(), "running", "proposed", lineterm="", n=3
    ):
        if line.startswith("+++") or line.startswith("---"):
            kind = "header"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "remove"
        else:
            kind = "context"
        out.append({"kind": kind, "text": line})
    return out
