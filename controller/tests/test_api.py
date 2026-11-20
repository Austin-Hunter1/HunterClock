"""Web API: auth, the hardware endpoints, config validation and simulate.

Driven through httpx against the ASGI app in the same event loop as the engine,
so the requests go through the real dependency chain rather than a stub.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from clockd.web.auth import COOKIE_NAME, Authenticator, AuthError


@pytest.fixture
async def client(stack, tmp_path):
    """An HTTP client against the app, signed in, with a password set."""
    from clockd.auth import hash_password
    from clockd.web import create_app

    stack.config.web.password_hash = hash_password("clockpassword")
    app = create_app(
        stack.engine, sim=True, config_path=tmp_path / "clock.yaml", repo_root=tmp_path
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://clock") as http:
        response = await http.post(
            "/login", data={"password": "clockpassword", "next": "/"}, follow_redirects=False
        )
        assert response.status_code == 303
        http.cookies.set(COOKIE_NAME, response.cookies[COOKIE_NAME])
        yield http


@pytest.fixture
async def anon(stack, tmp_path):
    """An HTTP client that has not signed in."""
    from clockd.auth import hash_password
    from clockd.web import create_app

    stack.config.web.password_hash = hash_password("clockpassword")
    app = create_app(
        stack.engine, sim=True, config_path=tmp_path / "clock.yaml", repo_root=tmp_path
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://clock") as http:
        yield http


# -- auth --------------------------------------------------------------------


async def test_the_api_rejects_anonymous_requests(anon):
    for path in ("/api/state", "/api/system", "/api/logs"):
        assert (await anon.get(path)).status_code == 401, path


async def test_anonymous_actions_are_rejected(anon):
    for path, body in [
        ("/api/jog", {"person": "evan", "degrees": 1}),
        ("/api/stop", None),
        ("/api/estop", None),
        ("/api/zero", {"person": "evan"}),
        ("/api/config/save", {"text": "version: 1"}),
    ]:
        response = await anon.post(path, json=body)
        assert response.status_code == 401, path


async def test_pages_redirect_to_the_login_form(anon):
    for path in ("/", "/control", "/assist", "/simulate", "/logs", "/config", "/system"):
        response = await anon.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


async def test_a_wrong_password_is_refused(anon):
    response = await anon.post("/login", data={"password": "guess", "next": "/"})
    assert response.status_code == 401
    assert COOKIE_NAME not in response.cookies


async def test_an_empty_password_is_refused(anon):
    assert (await anon.post("/login", data={"password": "", "next": "/"})).status_code == 401


async def test_a_forged_cookie_is_refused(anon):
    anon.cookies.set(COOKIE_NAME, "made.up")
    assert (await anon.get("/api/state")).status_code == 401


async def test_logout_clears_the_session(client):
    assert (await client.get("/api/state")).status_code == 200
    await client.post("/logout", follow_redirects=False)
    client.cookies.clear()
    assert (await client.get("/api/state")).status_code == 401


async def test_live_mode_without_a_password_refuses_to_start(stack):
    """Better to not come up than to serve the clock's controls to the network."""
    stack.config.web.password_hash = ""
    with pytest.raises(AuthError) as excinfo:
        Authenticator.from_config(stack.config, sim=False)
    assert "password" in str(excinfo.value).lower()


async def test_sim_mode_without_a_password_is_open_but_says_so(stack):
    stack.config.web.password_hash = ""
    auth = Authenticator.from_config(stack.config, sim=True)
    assert auth.open_access
    assert auth.source == "none (sim mode)"


async def test_a_password_from_the_environment_is_accepted(stack, monkeypatch):
    stack.config.web.password_hash = ""
    monkeypatch.setenv("CLOCK_UI_PASSWORD", "from-the-env")
    auth = Authenticator.from_config(stack.config, sim=False)
    assert not auth.open_access
    assert auth.check("from-the-env")
    assert not auth.check("something else")


# -- pages -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/control", "/assist", "/simulate", "/logs", "/config", "/system"]
)
async def test_every_page_renders(client, path):
    response = await client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<nav" in response.text


async def test_pages_render_with_the_pico_disconnected(client, stack):
    """The UI has to work when the hardware does not; that is when it is needed."""
    await stack.link.close()
    for path in ("/", "/control", "/assist"):
        assert (await client.get(path)).status_code == 200
    state = (await client.get("/api/state")).json()
    assert state["pico"]["connected"] is False


# -- state -------------------------------------------------------------------


async def test_state_has_everything_the_dashboard_needs(client):
    state = (await client.get("/api/state")).json()
    assert len(state["people"]) == 6
    assert set(state) >= {"people", "pico", "mqtt", "sources", "home", "motion", "sim"}
    person = state["people"][0]
    assert set(person) >= {
        "name", "slug", "level", "channel", "color", "status", "verified",
        "target_deg", "actual_deg", "distance_km", "source",
    }


# -- hardware actions --------------------------------------------------------


async def test_jog_moves_the_hand(client, stack):
    await stack.zero_all()
    response = await client.post("/api/jog", json={"person": "evan", "degrees": 10})
    assert response.status_code == 200
    await stack.settle()
    assert stack.engine.state("evan").actual_deg == pytest.approx(10.0, abs=0.5)


async def test_jog_accepts_a_negative_angle(client, stack):
    await stack.zero_all()
    await client.post("/api/jog", json={"person": "evan", "degrees": -10})
    await stack.settle()
    assert stack.engine.state("evan").actual_deg == pytest.approx(350.0, abs=0.5)


async def test_jog_rejects_an_unknown_person(client):
    response = await client.post("/api/jog", json={"person": "voldemort", "degrees": 1})
    assert response.status_code == 404


@pytest.mark.parametrize("degrees", [361, -361, 10_000])
async def test_jog_rejects_an_absurd_angle(client, degrees):
    response = await client.post("/api/jog", json={"person": "evan", "degrees": degrees})
    assert response.status_code == 422


async def test_jog_rejects_a_missing_field(client):
    assert (await client.post("/api/jog", json={"person": "evan"})).status_code == 422


async def test_move_to_an_angle(client, stack):
    await stack.zero_all()
    await client.post("/api/move", json={"person": "caleb", "angle_deg": 229.5})
    await stack.settle(seconds=6.0)
    assert stack.engine.state("caleb").actual_deg == pytest.approx(229.5, abs=0.6)


async def test_move_takes_the_short_way_round(client, stack):
    """0 to 350 degrees is a ten-degree backward move, not 350 forward."""
    await stack.zero_all()
    await client.post("/api/move", json={"person": "caleb", "angle_deg": 350.0})
    await stack.settle(seconds=6.0)
    person = stack.config.person("caleb")
    steps = stack.pico.positions()[person.channel]
    assert steps == pytest.approx(-89, abs=2)


async def test_set_zero_declares_twelve_and_verifies(client, stack):
    await client.post("/api/jog", json={"person": "micah", "degrees": 30})
    await stack.settle()
    response = await client.post("/api/zero", json={"person": "micah"})
    assert response.status_code == 200

    state = stack.engine.state("micah")
    assert state.verified
    assert state.actual_deg == pytest.approx(0.0, abs=0.01)
    assert stack.store.is_verified("micah")
    assert stack.pico.positions()[state.channel] == 0


async def test_verify_toggles_without_moving(client, stack):
    await stack.zero_all()
    steps = list(stack.pico.positions())
    await client.post("/api/verify", json={"person": "evan", "verified": False})
    assert not stack.engine.state("evan").verified
    assert stack.pico.positions() == steps
    await client.post("/api/verify", json={"person": "evan", "verified": True})
    assert stack.engine.state("evan").verified


async def test_enable_and_release_the_drivers(client, stack):
    await client.post("/api/enable", json={"on": False})
    assert stack.link.enabled is False
    await client.post("/api/enable", json={"on": True})
    assert stack.link.enabled is True


async def test_stop_all(client, stack):
    await stack.zero_all()
    await client.post("/api/move", json={"person": "evan", "angle_deg": 300})
    await asyncio.sleep(0.05)
    assert (await client.post("/api/stop")).status_code == 200
    await stack.settle(seconds=4.0)
    assert not stack.pico.core.mc.any_moving()


async def test_estop_unverifies_everything(client, stack):
    await stack.zero_all()
    assert (await client.post("/api/estop")).status_code == 200
    state = (await client.get("/api/state")).json()
    assert all(not p["verified"] for p in state["people"])
    assert all(p["status"] == "unverified" for p in state["people"])


async def test_all_to_twelve(client, stack):
    await stack.zero_all()
    await client.post("/api/jog", json={"person": "evan", "degrees": 40})
    await stack.settle()
    response = await client.post("/api/all-to-twelve")
    assert response.json()["moved"] == 6
    await stack.settle(seconds=6.0)
    assert stack.engine.state("evan").actual_deg == pytest.approx(0.0, abs=0.6)


async def test_params_are_clamped_to_the_configured_ceiling(client, stack):
    limit = stack.config.motion.speed_limit
    await client.post("/api/params", json={"max_speed": limit * 100})
    assert stack.link.info["max_speed"] <= limit


async def test_params_reject_a_nonsense_value(client):
    assert (await client.post("/api/params", json={"max_speed": -5})).status_code == 422


async def test_a_hardware_failure_returns_503_not_500(client, stack):
    """The UI must be able to say 'the Pico is not answering' plainly."""
    await stack.link.close()
    response = await client.post("/api/jog", json={"person": "evan", "degrees": 1})
    assert response.status_code == 503
    assert "not connected" in response.json()["error"].lower()


# -- simulate ----------------------------------------------------------------


async def test_override_by_distance(client, stack):
    await stack.zero_all()
    response = await client.post(
        "/api/override", json={"person": "micah", "distance_km": 100.0, "ttl_s": 60}
    )
    assert response.status_code == 200
    await stack.settle()
    assert stack.engine.state("micah").target_deg == pytest.approx(167.4, abs=0.1)
    state = (await client.get("/api/state")).json()
    micah = next(p for p in state["people"] if p["slug"] == "micah")
    assert micah["override"]["seconds_left"] > 0
    assert micah["status"] == "override"


async def test_override_by_position(client, stack):
    await stack.zero_all()
    await client.post(
        "/api/override", json={"person": "micah", "lat": 40.0150, "lon": -105.2705}
    )
    await stack.settle()
    assert stack.engine.state("micah").target_deg == pytest.approx(254.8, abs=0.1)


async def test_override_needs_something_to_go_on(client):
    response = await client.post("/api/override", json={"person": "micah"})
    assert response.status_code == 400


async def test_override_rejects_half_a_coordinate_pair(client):
    response = await client.post("/api/override", json={"person": "micah", "lat": 40.0})
    assert response.status_code == 400


async def test_override_rejects_an_impossible_latitude(client):
    response = await client.post("/api/override", json={"person": "micah", "lat": 99, "lon": 0})
    assert response.status_code == 422


async def test_clearing_an_override(client, stack):
    await stack.zero_all()
    await client.post("/api/override", json={"person": "evan", "distance_km": 10})
    await client.post("/api/override/clear", json={"person": "evan"})
    assert stack.engine.state("evan").override is None


async def test_sources_can_be_switched_off(client, stack):
    response = await client.post("/api/source", json={"name": "owntracks", "enabled": False})
    assert response.json()["enabled"] is False
    assert not stack.config.sources.owntracks.enabled
    await client.post("/api/source", json={"name": "owntracks", "enabled": True})
    assert stack.config.sources.owntracks.enabled


async def test_an_unknown_source_is_a_404(client):
    response = await client.post("/api/source", json={"name": "telepathy", "enabled": True})
    assert response.status_code == 404


async def test_scenarios_are_listed_and_playable(client, stack):
    listing = (await client.get("/api/scenarios")).json()
    names = [s["name"] for s in listing["scenarios"]]
    assert "evan_flies_home" in names

    await stack.zero_all()
    response = await client.post("/api/scenario", json={"name": "evan_flies_home", "seconds": 1})
    assert response.status_code == 200
    await asyncio.sleep(1.6)
    assert stack.engine.state("evan").distance_km == pytest.approx(0.0, abs=1.0)


async def test_an_unknown_scenario_is_a_404(client):
    assert (await client.post("/api/scenario", json={"name": "nope"})).status_code == 404


# -- logs --------------------------------------------------------------------


async def test_logs_are_filterable(client, stack):
    await stack.zero_all()
    everything = (await client.get("/api/logs?limit=500")).json()
    assert everything["events"]
    assert "action" in everything["counts"]

    actions = (await client.get("/api/logs?categories=action&limit=500")).json()
    assert actions["events"]
    assert all(e["category"] == "action" for e in actions["events"])


async def test_logs_support_incremental_polling(client, stack):
    first = (await client.get("/api/logs?limit=500")).json()
    last_id = first["last_id"]
    await stack.engine.stop_all()
    later = (await client.get(f"/api/logs?since_id={last_id}")).json()
    assert all(e["id"] > last_id for e in later["events"])


async def test_logs_search(client, stack):
    await stack.zero_all()
    found = (await client.get("/api/logs?search=zero&limit=500")).json()
    assert found["events"]
    assert all("zero" in e["message"].lower() for e in found["events"])


async def test_logs_download_is_plain_text(client):
    response = await client.get("/api/logs/download")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]


# -- config ------------------------------------------------------------------


async def test_config_is_readable(client):
    data = (await client.get("/api/config")).json()
    assert "people:" in data["text"]


async def test_valid_config_shows_a_diff(client):
    current = (await client.get("/api/config")).json()["text"]
    proposed = current.replace("max_speed: 800.0", "max_speed: 400.0")
    result = (await client.post("/api/config/validate", json={"text": proposed})).json()
    assert result["ok"]
    assert any(d["kind"] == "add" and "400" in d["text"] for d in result["diff"])
    assert result["summary"]["max_speed"] == 400.0


async def test_invalid_config_is_reported_not_saved(client, tmp_path):
    result = (await client.post(
        "/api/config/validate", json={"text": "version: 1\npeople: []\n"}
    )).json()
    assert not result["ok"]
    assert "people" in result["error"]

    response = await client.post("/api/config/save", json={"text": "version: 1\npeople: []\n"})
    assert response.status_code == 400
    assert not (tmp_path / "clock.yaml").exists()


async def test_broken_yaml_is_reported_clearly(client):
    response = await client.post(
        "/api/config/validate", json={"text": "people: [\n  - a: ["}
    )
    result = response.json()
    assert not result["ok"]
    assert "YAML" in result["error"]


async def test_saving_writes_the_file_and_backs_up_the_old_one(client, tmp_path):
    path = tmp_path / "clock.yaml"
    current = (await client.get("/api/config")).json()["text"]
    path.write_text(current, encoding="utf-8")

    proposed = current.replace('site: "not yet installed"', 'site: "Minnetonka"')
    result = (await client.post("/api/config/save", json={"text": proposed})).json()
    assert result["ok"]
    assert result["restart_required"]
    assert "Minnetonka" in path.read_text(encoding="utf-8")
    backups = list(tmp_path.glob("clock.yaml.*.bak"))
    assert len(backups) == 1
    assert "not yet installed" in backups[0].read_text(encoding="utf-8")


# -- system ------------------------------------------------------------------


async def test_system_info_degrades_gracefully_off_a_pi(client):
    info = (await client.get("/api/system")).json()
    assert info["hostname"]
    assert "cpu_temp_c" in info  # None here, a number on the Pi
    assert "disk" in info
    assert "tailscale" in info


async def test_the_diagnostic_bundle_is_a_zip_with_the_expected_members(client):
    response = await client.get("/api/system/diagnostics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {
            "snapshot.json", "events.log", "system.json",
            "config.sanitised.yaml", "store.json", "environment.txt",
        } <= names
        assert "set" in archive.read("environment.txt").decode()


async def test_the_diagnostic_bundle_carries_no_secrets(client, tmp_path, monkeypatch):
    """It exists to be sent to whoever is helping, so it must be safe to send."""
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2-the-real-one")
    path = tmp_path / "clock.yaml"
    current = (await client.get("/api/config")).json()["text"]
    path.write_text(
        current.replace('password_hash: ""', 'password_hash: "pbkdf2_sha256$1$abc$def"'),
        encoding="utf-8",
    )

    response = await client.get("/api/system/diagnostics")
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        blob = b"".join(archive.read(name) for name in archive.namelist())
    assert b"hunter2-the-real-one" not in blob
    assert b"pbkdf2_sha256$1$abc$def" not in blob
    # The variable's *name* is still reported, which is what makes it debuggable.
    assert b"MQTT_PASSWORD" in blob


async def test_firmware_upload_is_refused_in_sim_mode(client):
    """There is no Pico to upload to, and saying so beats a confusing failure."""
    response = await client.post("/api/firmware/upload", json={})
    assert response.status_code == 400
    assert "sim mode" in response.json()["detail"]


async def test_rollback_without_a_recorded_commit_is_refused(client):
    response = await client.post("/api/system/rollback", json={})
    assert response.status_code == 400
