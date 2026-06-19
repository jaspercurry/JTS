"""Route-level tests for jasper.control.server.

Spins the ThreadingHTTPServer on a random port. The volume routes go
through `_with_coordinator` — we monkey-patch that helper to bypass
the real CamillaController/RendererClient stack and feed in a fake
coordinator that records calls.
"""
from __future__ import annotations

import json
import re
import signal
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

import pytest

from jasper.control.server import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    _active_speaker_output_safety_snapshot,
    _clamp_db,
    _control_route_allowed_for_install_profile,
    _db_to_percent,
    _delta_db_to_delta_percent,
    _make_handler,
)


@pytest.fixture(autouse=True)
def _isolate_household_secret(monkeypatch, tmp_path):
    """Point household_credential at a throwaway path for every test here.

    _post_grouping_set adopts (on bond) and CLEARS (on unbond) the household
    secret, so any /grouping/set test that reaches the handler would otherwise
    read/delete the real /var/lib/jasper/household_secret if the suite ran on a
    bonded Pi. Redirect it to a tmp file (absent ⇒ unpaired) so the tests never
    touch real system state; tests that need a specific state call
    `_pair_household`/`_unpair_household`, which override this.
    """
    import jasper.control.household_credential as hc

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "household_secret"))


class FakeCoordinator:
    """In-memory stand-in. Same async surface as VolumeCoordinator."""

    def __init__(self, level: int = 60) -> None:
        self._level = int(level)
        self._pre_mute_level: int | None = None
        self.calls: list[tuple[str, int | None]] = []
        self.fail_next = False

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated coordinator failure")

    def get_listening_level(self) -> int:
        self._maybe_fail()
        self.calls.append(("get", None))
        return self._level

    def load_persisted_level(self) -> int:
        return self._level

    def is_muted(self) -> bool:
        return self._pre_mute_level is not None

    async def set_listening_level(self, percent: int) -> int:
        self._maybe_fail()
        target = max(0, min(100, int(percent)))
        self._level = target
        self._pre_mute_level = None
        self.calls.append(("set", target))
        return target

    async def adjust_listening_level(self, delta: int) -> int:
        self._maybe_fail()
        target = max(0, min(100, self._level + int(delta)))
        self._level = target
        self._pre_mute_level = None
        self.calls.append(("adjust", int(delta)))
        return target

    async def mute(self) -> int:
        self._maybe_fail()
        saved = self._pre_mute_level if self._pre_mute_level is not None else self._level
        if self._level > 0 and self._pre_mute_level is None:
            self._pre_mute_level = self._level
        self._level = 0
        self.calls.append(("mute", saved))
        return saved or 0

    async def unmute(self, fallback_level: int = 50) -> int:
        self._maybe_fail()
        target = self._pre_mute_level if self._pre_mute_level is not None else fallback_level
        self._pre_mute_level = None
        self._level = target
        self.calls.append(("unmute", target))
        return target

    async def observe_source_volume(self, source, percent: int) -> None:
        self._maybe_fail()
        # The real coordinator gates this on whether `source` is the
        # currently active one and on echo windows; the fake just
        # records the call so /volume/set route tests can assert the
        # right path was taken. The fake's `_level` mutation mirrors
        # what would happen in the active-source case so the response
        # body has a sensible value.
        target = max(0, min(100, int(percent)))
        self._level = target
        self.calls.append(("observe", target))

    async def aclose(self) -> None:
        return None


def test_active_speaker_output_safety_snapshot_classifies_staged_config() -> None:
    payload = _active_speaker_output_safety_snapshot({
        "current": {
            "camilla": {
                "config_path": (
                    "/var/lib/camilladsp/configs/"
                    "active_speaker_staged_startup.yml"
                ),
            },
        },
    })

    assert payload["safety_muted"] is True
    assert payload["reason"] == "active_speaker_staged_startup"
    assert payload["active_config_path"].endswith(
        "active_speaker_staged_startup.yml"
    )


def test_active_speaker_output_safety_snapshot_allows_baseline_config() -> None:
    payload = _active_speaker_output_safety_snapshot({
        "current": {
            "camilla": {
                "config_path": (
                    "/var/lib/camilladsp/configs/"
                    "active_speaker_baseline.yml"
                ),
            },
        },
    })

    assert payload["safety_muted"] is False
    assert payload["reason"] is None


@pytest.fixture
def server_with_coordinator(monkeypatch):
    """Start a ThreadingHTTPServer and patch _with_coordinator to use
    the fake. Yields (base_url, fake_coord)."""
    fake = FakeCoordinator(level=60)

    async def fake_with_coordinator(op, **kwargs):  # noqa: ARG001
        return await op(fake)

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_with_coordinator", fake_with_coordinator)

    class _NoAirPlayProcess:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_subprocess_exec(*args, **kwargs):  # noqa: ARG001
        return _NoAirPlayProcess()

    async def fake_mux_status(*args, **kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        srv_mod.asyncio,
        "create_subprocess_exec",
        fake_subprocess_exec,
    )
    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_status)

    handler = _make_handler("127.0.0.1", 9, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def server_with_voice_socket(monkeypatch):
    """Server fixture for /session/* endpoints: stubs out the UDS round-trip
    by monkey-patching _voice_socket_command. Yields (base, responses, received).
    Push dicts onto responses to control the next reply; default {"result":"OK"}."""
    voice_responses: list[dict] = []
    received_cmds: list[str] = []

    async def fake_command(socket_path, cmd):
        received_cmds.append(cmd)
        return voice_responses.pop(0) if voice_responses else {"result": "OK"}

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)

    # Coordinator is also patched — session-only tests don't touch
    # volume routes, but the handler factory still needs the wiring.
    fake_coord = FakeCoordinator(level=60)

    async def fake_with_coordinator(op, **kwargs):  # noqa: ARG001
        return await op(fake_coord)

    monkeypatch.setattr(srv_mod, "_with_coordinator", fake_with_coordinator)

    handler = _make_handler("127.0.0.1", 1234, "/tmp/unused.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, voice_responses, received_cmds
    finally:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)


def _maybe_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _post(
    url: str,
    body: dict | None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=req_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _post_raw(
    url: str,
    data: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=req_headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


# --- pure helpers ---


def test_clamp_db_endpoints():
    assert _clamp_db(-100.0) == VOLUME_MIN_DB
    assert _clamp_db(50.0) == VOLUME_MAX_DB
    assert _clamp_db(-10.0) == -10.0


def test_db_to_percent_endpoints():
    assert _db_to_percent(VOLUME_MIN_DB) == 0
    assert _db_to_percent(VOLUME_MAX_DB) == 100
    assert _db_to_percent((VOLUME_MIN_DB + VOLUME_MAX_DB) / 2) == 50


def test_delta_db_to_delta_percent_5db_is_10pp():
    assert _delta_db_to_delta_percent(5.0) == 10
    assert _delta_db_to_delta_percent(-5.0) == -10
    assert _delta_db_to_delta_percent(2.5) == 5


# --- management request guardrails ---


def test_rejects_bad_host_on_get(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz", headers={"Host": "evil.example"})
    assert status == 403
    assert body["error"] == "host_not_allowed"


def test_cross_site_get_healthz_is_allowed(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _get(
        f"{base}/healthz",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 200
    assert body == {"ok": True}


def test_full_profile_allows_every_control_route():
    # Full speakers allow every route.
    for path in ("/state", "/mic", "/cue/play", "/session/start"):
        assert _control_route_allowed_for_install_profile(
            "full", method="GET", path=path,
        )
        assert _control_route_allowed_for_install_profile(
            "full", method="POST", path=path,
        )


def test_legacy_endpoint_token_uses_streambox_route_policy():
    # The removed endpoint tier maps to streambox, so the legacy token gets
    # the streambox route policy (e.g. /state + /source/state allowed).
    assert _control_route_allowed_for_install_profile(
        "endpoint", method="GET", path="/state",
    )
    assert _control_route_allowed_for_install_profile(
        "endpoint", method="GET", path="/source/state",
    )
    assert _control_route_allowed_for_install_profile(
        "endpoint", method="POST", path="/source/select",
    )
    assert not _control_route_allowed_for_install_profile(
        "endpoint", method="GET", path="/mic",
    )
    assert not _control_route_allowed_for_install_profile(
        "endpoint", method="POST", path="/system/restart/voice",
    )


def test_streambox_profile_control_route_policy():
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/healthz",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/state",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/source/state",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/system/snapshot",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/mic",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="GET", path="/aec",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/volume/set",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/source/select",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/transport/toggle",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/transport/next",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/transport/previous",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/system/audio-quality",
    )
    assert _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/system/restart/audio",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/cue/play",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/mic/mute",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/session/start",
    )
    assert not _control_route_allowed_for_install_profile(
        "streambox", method="POST", path="/system/restart/voice",
    )


def test_legacy_endpoint_token_uses_streambox_routes_at_http_layer(
    monkeypatch,
    server_with_coordinator,
):
    # A persisted legacy "endpoint" marker normalizes to streambox, so the
    # HTTP route gate applies the streambox policy: source routes are allowed,
    # voice-brain routes are 404.
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "endpoint")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body == {"ok": True}

    # voice-brain route blocked
    status, _body = _get(f"{base}/mic")
    assert status == 404

    # a streambox-allowed route is not 404 from the route gate
    status, _body = _get(f"{base}/grouping")
    assert status != 404


def test_streambox_profile_blocks_voice_brain_control_routes(
    monkeypatch,
    server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "streambox")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body == {"ok": True}

    status, _body = _get(f"{base}/mic")
    assert status == 404

    status, _body = _get(f"{base}/aec")
    assert status == 404

    status, _body = _post(f"{base}/cue/play", {"slug": "cant_connect"})
    assert status == 404

    status, _body = _post(f"{base}/system/restart/voice", {})
    assert status == 404


def test_cross_site_get_rejects_diagnostics_before_subprocess(
    server_with_coordinator, monkeypatch,
):
    import jasper.control.server as srv_mod

    calls = []

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append((args, kwargs))
        raise AssertionError("diagnostics should not run")

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)

    base, _ = server_with_coordinator
    status, body = _get(
        f"{base}/system/diagnostics",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert body["error"] == "cross_site_request"
    assert calls == []


def test_diagnostics_starts_root_oneshot_and_serves_result(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """WS1 Phase 3b-2: /system/diagnostics must START the root
    jasper-doctor-json.service oneshot (via the non-root jasper-control's polkit
    grant) and serve the JSON it wrote — NOT spawn the doctor in-process (which
    would run non-root and report false hardware failures)."""
    import jasper.control.server as srv_mod

    result = tmp_path / "doctor-result.json"
    result.write_text(json.dumps({
        "fails": 0,
        "warns": 1,
        "results": [{"name": "x", "status": "warn", "detail": "d"}],
    }))
    monkeypatch.setenv("JASPER_DIAGNOSTICS_RESULT_PATH", str(result))

    started: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN002, ANN003
        started.append(cmd)
        return FakeProc()

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")

    assert status == 200
    assert body["warns"] == 1
    assert started == [["systemctl", "start", "jasper-doctor-json.service"]]


def test_diagnostics_502_when_oneshot_start_fails(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """A polkit denial / hard start failure (non-zero systemctl) must surface as
    an honest 502, never a silent/garbled report."""
    import jasper.control.server as srv_mod

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "Interactive authentication required."

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/diagnostics")
    status2, body2 = _get(f"{base}/system/diagnostics")
    assert status == 502
    assert status2 == 502
    assert "unavailable" in body["error"]
    assert body2 == body
    assert calls == [["systemctl", "start", "jasper-doctor-json.service"]]


def test_diagnostics_concurrent_request_returns_pending_without_second_start(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """Only one root doctor capture should run at a time.

    A second browser tab / LAN client gets a fast "pending" response rather
    than tying up another jasper-control worker for the whole 30 s systemctl
    timeout window.
    """
    import jasper.control.server as srv_mod

    result = tmp_path / "doctor-result.json"
    result.write_text('{"fails":0,"warns":0,"results":[]}')
    monkeypatch.setenv("JASPER_DIAGNOSTICS_RESULT_PATH", str(result))

    started = threading.Event()
    release = threading.Event()
    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(cmd)
        started.set()
        assert release.wait(timeout=2), "test did not release diagnostics"
        return FakeProc()

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)

    base, _ = server_with_coordinator
    first: list[tuple[int, dict]] = []
    t = threading.Thread(
        target=lambda: first.append(_get(f"{base}/system/diagnostics")),
        daemon=True,
    )
    t.start()
    assert started.wait(timeout=1)

    status, body = _get(f"{base}/system/diagnostics")
    assert status == 202
    assert body["pending"] is True
    assert calls == [["systemctl", "start", "jasper-doctor-json.service"]]

    release.set()
    t.join(timeout=2)
    assert first == [(200, {"fails": 0, "warns": 0, "results": []})]


def test_diagnostics_serves_recent_success_from_cache(
    server_with_coordinator, monkeypatch, tmp_path,
):
    import jasper.control.server as srv_mod

    result = tmp_path / "doctor-result.json"
    result.write_text(json.dumps({
        "fails": 0,
        "warns": 1,
        "results": [{"name": "x", "status": "warn"}],
    }))
    monkeypatch.setenv("JASPER_DIAGNOSTICS_RESULT_PATH", str(result))

    calls: list[list[str]] = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(srv_mod.subprocess, "run", fake_run)

    base, _ = server_with_coordinator
    status1, body1 = _get(f"{base}/system/diagnostics")
    status2, body2 = _get(f"{base}/system/diagnostics")

    assert status1 == status2 == 200
    assert body1 == body2
    assert body1["warns"] == 1
    assert calls == [["systemctl", "start", "jasper-doctor-json.service"]]


def test_system_audio_quality_applies_and_try_restarts_renderers(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    applied: list[str] = []
    popens: list[list[str]] = []

    def fake_apply(converter: str) -> dict:
        applied.append(converter)
        return {
            "converter": converter,
            "active_converter": converter,
            "label": "Best",
            "summary": "Maximum ultrasonic-band fidelity.",
            "options": [],
        }

    class FakePopen:
        def __init__(self, cmd):
            popens.append(cmd)

    monkeypatch.setattr(srv_mod, "_apply_audio_quality", fake_apply)
    monkeypatch.setattr(srv_mod.subprocess, "Popen", FakePopen)

    status, body = _post(
        f"{base}/system/audio-quality",
        {"converter": "best"},
    )

    assert status == 200
    assert applied == ["samplerate_best"]
    assert body["audio_quality"]["converter"] == "samplerate_best"
    assert popens == [
        [
            "systemctl", "try-restart",
            "shairport-sync.service",
            "librespot.service",
            "bluealsa-aplay.service",
            "jasper-usbsink.service",
        ],
    ]


def test_system_audio_quality_rejects_unknown_converter(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    def fail_apply(_converter: str) -> dict:
        raise AssertionError("invalid converter should not apply")

    monkeypatch.setattr(srv_mod, "_apply_audio_quality", fail_apply)

    status, body = _post(
        f"{base}/system/audio-quality",
        {"converter": "linear"},
    )

    assert status == 400
    assert "unsupported ALSA rate converter" in body["error"]


def test_system_audio_quality_rejects_missing_converter(
    monkeypatch,
    server_with_coordinator,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    def fail_apply(_converter: str) -> dict:
        raise AssertionError("missing converter should not apply")

    monkeypatch.setattr(srv_mod, "_apply_audio_quality", fail_apply)

    status, body = _post(f"{base}/system/audio-quality", {})

    assert status == 400
    assert body["error"] == "converter is required"


def test_system_action_reboot_audits_and_invokes_systemctl(
    monkeypatch,
    server_with_coordinator,
    caplog,
):
    """A destructive /system/ action emits an `event=system.action` audit line
    (so a dashboard-triggered reboot is distinguishable from a watchdog/crash
    reset when debugging "the speaker restarted on its own") and shells out to
    the right systemctl command. subprocess.Popen is mocked so no test machine
    reboots."""
    import logging

    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator
    popens: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd):
            popens.append(cmd)

    monkeypatch.setattr(srv_mod.subprocess, "Popen", FakePopen)

    with caplog.at_level(logging.INFO, logger="jasper.control.server"):
        status, body = _post(f"{base}/system/reboot", {})

    assert status == 200
    assert body["action"] == "reboot"
    assert popens == [["systemctl", "reboot"]]
    assert any(
        "event=system.action action=reboot" in rec.getMessage()
        for rec in caplog.records
    ), "reboot must emit an event=system.action audit line"


def test_aec_toggle_restarts_reconciler(monkeypatch, tmp_path, server_with_coordinator):
    """AEC mode changes must restart the oneshot reconciler, not just start it.

    A rapid off/on click can happen while the previous reconcile is still
    active; `systemctl start` would be a no-op and leave runtime env behind
    the persisted intent.
    """
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    mode_file = tmp_path / "aec_mode.env"
    mode_file.write_text("JASPER_AEC_MODE=auto\n")
    popens: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd):
            popens.append(cmd)

    monkeypatch.setattr(srv_mod, "_AEC_MODE_FILE", str(mode_file))
    monkeypatch.setattr(srv_mod, "_aec_bridge_active", lambda: False)
    monkeypatch.setattr(srv_mod.subprocess, "Popen", FakePopen)

    status, body = _post(f"{base}/aec/toggle", None)

    assert status == 200
    assert body["mode"] == "disabled"
    assert popens == [
        ["systemctl", "restart", "--no-block", "jasper-aec-reconcile.service"],
    ]


def test_aec_leg_restarts_reconciler(monkeypatch, tmp_path, server_with_coordinator):
    """Leg changes use the same restart kick as the AEC master toggle."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    mode_file = tmp_path / "aec_mode.env"
    mode_file.write_text("JASPER_AEC_MODE=auto\n")
    popens: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd):
            popens.append(cmd)

    monkeypatch.setattr(srv_mod, "_AEC_MODE_FILE", str(mode_file))
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: {"ok": True})
    monkeypatch.setattr(srv_mod.subprocess, "Popen", FakePopen)

    status, body = _post(
        f"{base}/aec/leg",
        {"leg": "chip_aec", "enabled": True},
    )

    assert status == 200
    assert body == {"ok": True}
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" in mode_file.read_text()
    assert popens == [
        ["systemctl", "restart", "--no-block", "jasper-aec-reconcile.service"],
    ]


def test_json_array_body_is_treated_as_empty_body(server_with_coordinator):
    base, _ = server_with_coordinator

    status, body = _post_raw(f"{base}/aec/leg", b"[]")

    assert status == 400
    assert body["error"] == "leg must be one of: chip_aec, dtln, raw"


def test_aec_profile_restarts_reconciler(monkeypatch, tmp_path, server_with_coordinator):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    mode_file = tmp_path / "aec_mode.env"
    mode_file.write_text("JASPER_AEC_MODE=auto\n")
    popens: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd):
            popens.append(cmd)

    monkeypatch.setattr(srv_mod, "_AEC_MODE_FILE", str(mode_file))
    monkeypatch.setattr(srv_mod, "_aec_full_status", lambda: {"profile": "xvf_chip_aec"})
    monkeypatch.setattr(srv_mod.subprocess, "Popen", FakePopen)

    status, body = _post(
        f"{base}/aec/profile",
        {"profile": "xvf_chip_aec"},
    )

    assert status == 200
    assert body == {"profile": "xvf_chip_aec"}
    text = mode_file.read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec" in text
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" in text
    assert popens == [
        ["systemctl", "restart", "--no-block", "jasper-aec-reconcile.service"],
    ]


# ---------- POST /grouping/set (the bond-forming control endpoint) ----------


def _grouping_test_setup(monkeypatch, tmp_path):
    """Redirect grouping.env to a tmp file + capture reconciler kicks."""
    import jasper.control.server as srv_mod

    env = tmp_path / "grouping.env"
    popens: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd):
            popens.append(cmd)

    monkeypatch.setattr(srv_mod, "GROUPING_ENV_FILE", str(env))
    monkeypatch.setattr(srv_mod.subprocess, "Popen", FakePopen)
    return env, popens


_GROUPING_KICK = [
    "systemctl", "restart", "--no-block", "jasper-grouping-reconcile.service",
]


def test_grouping_set_leader_writes_env_and_kicks_reconciler(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "leader", "channel": "left",
        "bond_id": "living-room",
    })

    assert status == 200
    assert body["ok"] is True
    assert body["role"] == "leader" and body["channel"] == "left"
    text = env.read_text()
    assert "JASPER_GROUPING=on" in text
    assert "JASPER_GROUPING_ROLE=leader" in text
    assert "JASPER_GROUPING_CHANNEL=left" in text
    assert "JASPER_GROUPING_BOND_ID=living-room" in text
    assert _GROUPING_KICK in popens


def test_grouping_set_disabled_writes_off_and_kicks(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {"enabled": False})

    assert status == 200
    assert body["enabled"] is False
    assert "JASPER_GROUPING=off" in env.read_text()
    assert _GROUPING_KICK in popens


def test_grouping_set_rejects_invalid_role_without_writing(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "boss", "channel": "left", "bond_id": "x",
    })

    assert status == 400
    assert "ROLE" in body["error"]
    assert not env.exists()   # nothing persisted on a rejected request
    assert _GROUPING_KICK not in popens   # grouping reconciler not kicked


def test_grouping_set_follower_requires_leader_addr(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    env, popens = _grouping_test_setup(monkeypatch, tmp_path)

    status, body = _post(f"{base}/grouping/set", {
        "enabled": True, "role": "follower", "channel": "right", "bond_id": "x",
    })

    assert status == 400
    assert "LEADER_ADDR" in body["error"]
    assert not env.exists()
    assert _GROUPING_KICK not in popens


# ---------- GET /grouping (the dissolve-flow read endpoint) ----------


def test_grouping_get_returns_grouping_block(
    monkeypatch, server_with_coordinator,
):
    """GET /grouping returns read_grouping_state() under a `grouping`
    key — the block the dissolve flow reads to discover bond
    membership (role, bond_id, leader_addr)."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    snapshot = {
        "enabled": True,
        "role": "leader",
        "channel": "left",
        "bond_id": "living-room",
        "leader_addr": "",
        "buffer_ms": 1000,
        "codec": "flac",
        "error": None,
    }
    monkeypatch.setattr(srv_mod, "read_grouping_state", lambda: snapshot)

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["grouping"] == snapshot
    # Cross-boundary contract: the /rooms /unbond CONSUMER must extract the
    # snapshot from the PRODUCER's actual body via the shared parser. Running
    # the real emitted body through parse_grouping_response here is what would
    # have caught the C4 drift (the two daemons no longer test only their own
    # half of the contract in isolation).
    from jasper.multiroom.state import parse_grouping_response
    assert parse_grouping_response(body) == snapshot


def test_grouping_get_requires_no_csrf(monkeypatch, server_with_coordinator):
    """A plain GET (no Origin / CSRF token) succeeds — /grouping is an
    unauthenticated read on this no-auth LAN surface, like /state and
    /healthz."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_grouping_state", lambda: {"enabled": False})

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["grouping"] == {"enabled": False}


def test_grouping_get_fails_soft_on_read_error(
    monkeypatch, server_with_coordinator,
):
    """If read_grouping_state raises, /grouping still returns 200 with a
    null grouping payload rather than 500 — mirrors /state's fail-soft
    grouping section."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    def boom():
        raise RuntimeError("grouping read exploded")

    monkeypatch.setattr(srv_mod, "read_grouping_state", boom)

    status, body = _get(f"{base}/grouping")

    assert status == 200
    assert body["grouping"] is None


def test_system_snapshot_audio_quality_fails_soft(
    monkeypatch,
):
    import jasper.control.server as srv_mod

    def fail_state() -> dict:
        raise ValueError("unsupported ALSA rate converter 'linear'")

    monkeypatch.setattr(srv_mod, "_read_audio_quality_state", fail_state)
    monkeypatch.setattr(
        srv_mod,
        "_read_active_audio_converter",
        lambda: "samplerate_medium",
    )

    body = srv_mod._safe_audio_quality_state()

    assert body["converter"] == "samplerate_medium"
    assert body["active_converter"] == "samplerate_medium"
    assert "unsupported ALSA rate converter" in body["error"]


def test_system_snapshot_legacy_endpoint_token_reports_streambox_caps(
    monkeypatch,
    server_with_coordinator,
):
    # A persisted legacy "endpoint" marker normalizes to streambox; the
    # capabilities payload reflects streambox, not a removed third role.
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "endpoint")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/snapshot")

    assert status == 200
    caps = body["system_capabilities"]
    assert caps["install_profile"] == "endpoint"  # raw token preserved
    assert caps["role"] == "streambox"            # normalized role
    assert caps["voice_brain"] is False
    assert caps["developer_tools"] is False
    assert caps["network_settings"] is True
    assert caps["reboot"] is True
    assert caps["poweroff"] is True
    assert "unavailable_reason" not in caps


def test_system_snapshot_reports_full_capabilities(
    monkeypatch,
    server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "full")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/snapshot")

    assert status == 200
    caps = body["system_capabilities"]
    assert caps["install_profile"] == "full"
    assert caps["role"] == "full"
    assert caps["voice_brain"] is True
    assert caps["developer_tools"] is True


def test_system_snapshot_reports_streambox_capabilities(
    monkeypatch,
    server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "read_install_profile", lambda: "streambox")

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/system/snapshot")

    assert status == 200
    caps = body["system_capabilities"]
    assert caps["install_profile"] == "streambox"
    assert caps["role"] == "streambox"
    assert caps["local_sources"] is True
    assert caps["content_dsp"] is True
    assert caps["voice_brain"] is False
    assert caps["audio_quality"] is True
    assert caps["restart_voice"] is False
    assert caps["restart_audio"] is True
    assert caps["network_settings"] is True
    assert caps["speaker_settings"] is True
    assert caps["pair_management"] is True
    assert caps["developer_tools"] is False
    assert caps["reboot"] is True
    assert caps["poweroff"] is True
    assert "unavailable_reason" not in caps


def test_post_allows_same_origin_browser_request(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Origin": base},
    )
    assert status == 200
    assert body["percent"] == 61
    assert ("adjust", 1) in fake.calls


def test_post_rejects_cross_origin_browser_request(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert body["error"] == "origin_not_allowed"
    assert fake.calls == []


def test_post_rejects_cross_site_fetch_metadata_without_origin(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert body["error"] == "cross_site_request"
    assert fake.calls == []


def test_post_rejects_dns_rebinding_host(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/adjust",
        {"delta_percent": 1},
        headers={"Host": "evil.example", "Origin": "http://evil.example"},
    )
    assert status == 403
    assert body["error"] == "host_not_allowed"
    assert fake.calls == []


def test_post_rejects_oversized_body_before_dispatch(server_with_coordinator):
    import jasper.control.server as srv_mod

    base, fake = server_with_coordinator
    payload = b"{" + b'"x":' + b'"' + (b"a" * srv_mod.CONTROL_MAX_POST_BYTES) + b'"}'
    status, body = _post_raw(f"{base}/volume/adjust", payload)
    assert status == 413
    assert body["error"] == "request_body_too_large"
    assert fake.calls == []


# --- routes ---


def test_healthz(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/healthz")
    assert status == 200
    assert body == {"ok": True}


def test_get_volume(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _get(f"{base}/volume")
    assert status == 200
    assert body["percent"] == 60
    # `db` is computed from percent for back-compat
    assert body["db"] == round((60 / 100) * (VOLUME_MAX_DB - VOLUME_MIN_DB) + VOLUME_MIN_DB, 3)
    assert ("get", None) in fake.calls


def test_volume_adjust_legacy_delta_db(server_with_coordinator):
    """Dial firmware sends delta_db; control daemon converts to
    listening_level percent points."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {"delta_db": -2.5})
    assert status == 200
    # -2.5 dB on 50 dB span = -5 percent points; 60 - 5 = 55
    assert body["percent"] == 55
    assert ("adjust", -5) in fake.calls


def test_volume_adjust_native_delta_percent(server_with_coordinator):
    """Newer clients send delta_percent directly."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": 10})
    assert status == 200
    assert body["percent"] == 70
    assert ("adjust", 10) in fake.calls


def test_volume_adjust_clamps_high(server_with_coordinator):
    base, fake = server_with_coordinator
    fake._level = 95
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": 20})
    assert status == 200
    assert body["percent"] == 100


def test_volume_adjust_clamps_low(server_with_coordinator):
    base, fake = server_with_coordinator
    fake._level = 5
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": -30})
    assert status == 200
    assert body["percent"] == 0


def test_volume_set_legacy_db(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"db": -25.0})
    assert status == 200
    # -25 dB → 50% (midpoint of -50..0 span)
    assert body["percent"] == 50
    assert ("set", 50) in fake.calls


def test_volume_set_native_percent(server_with_coordinator):
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": 75})
    assert status == 200
    assert body["percent"] == 75
    assert ("set", 75) in fake.calls


def test_volume_set_clamps(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": 200})
    assert status == 200
    assert body["percent"] == 100


def test_adjust_missing_field_400(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {})
    assert status == 400


def test_adjust_non_numeric_400(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": "loud"})
    assert status == 400


def test_set_missing_field_400(server_with_coordinator):
    base, _ = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {})
    assert status == 400


def test_volume_set_with_usbsink_source_routes_to_observe(server_with_coordinator):
    """/volume/set with `source: usbsink` should go through
    observe_source_volume so the coordinator's echo-prevention applies.
    Without `source`, the request is authoritative (set path)."""
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/set",
        {"percent": 42, "source": "usbsink"},
    )
    assert status == 200
    assert body["percent"] == 42
    # observe call recorded, not set.
    assert ("observe", 42) in fake.calls
    assert all(c[0] != "set" for c in fake.calls), \
        f"unexpected set call in {fake.calls}"


def test_volume_set_with_unknown_source_falls_back_to_set(server_with_coordinator):
    """Unknown source names go through the authoritative set path so a
    future client that posts a fresh source name doesn't silently
    no-op. (Defensive: avoid 400ing on a typo.)"""
    base, fake = server_with_coordinator
    status, body = _post(
        f"{base}/volume/set",
        {"percent": 55, "source": "rotary-future-source"},
    )
    assert status == 200
    assert body["percent"] == 55
    assert ("set", 55) in fake.calls


def test_volume_set_without_source_is_authoritative(server_with_coordinator):
    """Existing dial / voice clients post without `source`; they
    continue to hit the authoritative set path."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": 80})
    assert status == 200
    assert ("set", 80) in fake.calls
    assert all(c[0] != "observe" for c in fake.calls)


def test_volume_mute_toggles_off_then_on(server_with_coordinator):
    """First POST mutes (saves 60% pre-mute, returns 0). Second
    POST unmutes (restores 60%). Public endpoint used by the
    dashboard and any future client; the VK-01 knob click moved
    off this route to /transport/* with the multi-tap rebind."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200
    assert body["percent"] == 0
    assert ("mute", 60) in fake.calls

    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200
    assert body["percent"] == 60
    assert ("unmute", 60) in fake.calls


def test_volume_mute_when_already_silent(server_with_coordinator):
    """Edge: clicking mute on a 0% volume saves 0 as pre-mute, level
    stays 0. Click again restores 0. Doesn't blow up — the knob is
    safe to click when nothing's playing."""
    base, fake = server_with_coordinator
    fake._level = 0
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200
    assert body["percent"] == 0


# --- /transport/{toggle,next,previous} ---


@pytest.fixture
def server_with_transport_stub(monkeypatch):
    """Mirror server_with_coordinator but stub `_dispatch_transport`
    so we don't need a renderer/Spotify-router stack. Yields
    (base_url, calls) where calls is the list of actions dispatched."""
    calls: list[str] = []

    async def fake_dispatch(action: str) -> dict:
        calls.append(action)
        return {"result": "ok", "action": action}

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_dispatch_transport", fake_dispatch)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_transport_toggle_dispatches_toggle(server_with_transport_stub):
    base, calls = server_with_transport_stub
    status, body = _post(f"{base}/transport/toggle", {})
    assert status == 200
    assert calls == ["toggle"]
    assert body["action"] == "toggle"


def test_transport_next_dispatches_next(server_with_transport_stub):
    """Double-tap on the dial / VK-01 lands here."""
    base, calls = server_with_transport_stub
    status, body = _post(f"{base}/transport/next", {})
    assert status == 200
    assert calls == ["next"]
    assert body["action"] == "next"


def test_transport_previous_dispatches_previous(server_with_transport_stub):
    """Triple-tap on the dial / VK-01 lands here."""
    base, calls = server_with_transport_stub
    status, body = _post(f"{base}/transport/previous", {})
    assert status == 200
    assert calls == ["previous"]
    assert body["action"] == "previous"


def test_transport_dispatcher_error_propagates_as_502(monkeypatch):
    """If the renderer/router stack errors mid-dispatch the response is
    502 with the error message in the body — same shape as the
    refactored toggle path."""
    async def fake_dispatch(action: str) -> dict:  # noqa: ARG001
        raise RuntimeError("simulated MPRIS unavailable")

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_dispatch_transport", fake_dispatch)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body = _post(f"{base}/transport/next", {})
        assert status == 502
        assert "simulated MPRIS unavailable" in body.get("error", "")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_transport_dispatcher_error_field_propagates_as_502(monkeypatch):
    """If the dispatcher returns {"error": ...} (e.g. "no playing
    source"), the route surfaces it as 502 — same as the existing
    toggle behaviour. Used today by the dial's LED to show red."""
    async def fake_dispatch(action: str) -> dict:  # noqa: ARG001
        return {"error": "no playing source"}

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_dispatch_transport", fake_dispatch)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body = _post(f"{base}/transport/previous", {})
        assert status == 502
        assert body["error"] == "no playing source"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- /source/state + /source/select ---


@pytest.fixture
def server_with_mux_stub(monkeypatch):
    """Stub jasper-mux's UDS command helper so source-selection route
    tests don't require a live daemon socket."""
    calls: list[str] = []
    responses: list[dict] = []

    async def fake_mux_command(cmd: str, **kwargs):  # noqa: ARG001
        calls.append(cmd)
        if responses:
            response = responses.pop(0)
            if response.get("raise") == "missing":
                raise FileNotFoundError("/run/jasper-mux/control.sock")
            return response
        if cmd.startswith("SELECT "):
            selected = cmd.split(" ", 1)[1]
            return {
                "mode": "manual",
                "selected_source": selected,
                "active_source": selected,
                "sources": {
                    "airplay": {"playing": selected == "airplay"},
                    "bluetooth": {"playing": selected == "bluetooth"},
                    "spotify": {"playing": selected == "spotify"},
                    "usbsink": {"playing": selected == "usbsink"},
                },
            }
        return {
            "mode": "auto",
            "selected_source": None,
            "active_source": "airplay",
            "sources": {
                "airplay": {"playing": True},
                "bluetooth": {"playing": False},
                "spotify": {"playing": False},
                "usbsink": {"playing": False},
            },
        }

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_command)

    def fake_augment(payload: dict) -> dict:
        for source in payload.get("sources", {}).values():
            source["available"] = True
            source["enabled"] = True
        return payload

    monkeypatch.setattr(srv_mod, "_augment_source_payload", fake_augment)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, calls, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_state_proxies_mux_status(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, body = _get(f"{base}/source/state")

    assert status == 200
    assert calls == ["STATUS"]
    assert body["active_source"] == "airplay"
    assert body["sources"]["airplay"]["playing"] is True


def test_source_select_posts_source_to_mux(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, body = _post(f"{base}/source/select", {"source": "bluetooth"})

    assert status == 200
    assert calls == ["SELECT bluetooth"]
    assert body["mode"] == "manual"
    assert body["selected_source"] == "bluetooth"


def test_source_select_auto_posts_auto_to_mux(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, _ = _post(f"{base}/source/select", {"source": "auto"})

    assert status == 200
    assert calls == ["AUTO"]


def test_source_select_rejects_unknown_source(server_with_mux_stub):
    base, calls, _ = server_with_mux_stub
    status, body = _post(f"{base}/source/select", {"source": "cassette"})

    assert status == 400
    assert calls == []
    assert "source must be" in body["error"]


def test_source_state_mux_unreachable_is_503(server_with_mux_stub):
    base, calls, responses = server_with_mux_stub
    responses.append({"raise": "missing"})

    status, body = _get(f"{base}/source/state")

    assert status == 503
    assert calls == ["STATUS"]
    assert "jasper-mux unreachable" in body["error"]


def test_source_payload_adds_sources_wizard_availability(monkeypatch):
    import jasper.web.sources_setup as sources_mod
    from jasper.control.server import _augment_source_payload

    monkeypatch.setattr(sources_mod, "_gather_state", lambda: {
        "airplay": {"available": True, "enabled": True},
        "bluetooth": {"available": False, "enabled": False},
        "spotify_connect": {"available": True, "enabled": True},
        "usbsink": {"available": False, "enabled": False},
    })
    payload = {
        "sources": {
            "airplay": {"playing": False},
            "bluetooth": {"playing": False},
            "spotify": {"playing": True},
            "usbsink": {"playing": False},
        },
    }

    result = _augment_source_payload(payload)

    assert result["sources"]["airplay"]["enabled"] is True
    assert result["sources"]["bluetooth"]["available"] is False
    assert result["sources"]["spotify"]["enabled"] is True
    assert result["sources"]["usbsink"]["available"] is False


def test_source_availability_probe_runs_outside_cache_lock(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.web.sources_setup as sources_mod

    entered_probe = threading.Event()
    release_probe = threading.Event()
    errors: list[BaseException] = []

    def slow_gather_state():
        entered_probe.set()
        assert release_probe.wait(timeout=2)
        return {
            "airplay": {"available": True, "enabled": True},
            "bluetooth": {"available": False, "enabled": False},
            "spotify_connect": {"available": True, "enabled": True},
            "usbsink": {"available": True, "enabled": True},
        }

    def augment():
        try:
            srv_mod._augment_source_payload(
                {
                    "sources": {
                        "airplay": {},
                        "bluetooth": {},
                        "spotify": {},
                        "usbsink": {},
                    },
                },
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    monkeypatch.setattr(sources_mod, "_gather_state", slow_gather_state)
    srv_mod._source_availability_cache = None
    worker = threading.Thread(target=augment)
    worker.start()
    assert entered_probe.wait(timeout=2)

    acquired = srv_mod._source_availability_lock.acquire(timeout=0.2)
    try:
        assert acquired, "source availability probe held the cache lock"
    finally:
        if acquired:
            srv_mod._source_availability_lock.release()
        release_probe.set()
        worker.join(timeout=2)
        srv_mod._source_availability_cache = None

    assert not worker.is_alive()
    assert not errors


# --- 404 / coordinator-failure ---


def test_unknown_route_404(server_with_coordinator):
    base, _ = server_with_coordinator
    status, _ = _get(f"{base}/nope")
    assert status == 404


def test_coordinator_failure_502(server_with_coordinator):
    base, fake = server_with_coordinator
    fake.fail_next = True
    status, body = _post(f"{base}/volume/adjust", {"delta_percent": -10})
    assert status == 502
    assert "error" in body


# --- /state aggregation ---


def test_sound_runtime_status_flags_base_config_mismatch() -> None:
    import jasper.control.server as srv_mod

    runtime = srv_mod._sound_runtime_status(
        {
            "enabled": True,
            "filter_count": 3,
            "last_dsp_apply": {
                "result": "success",
                "active_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
            },
        },
        "/etc/camilladsp/outputd-cutover.yml",
    )

    assert runtime["state"] == "base"
    assert runtime["active"] is False
    assert runtime["matches_last_apply"] is False
    assert "not the active" in runtime["warning"]


def test_state_returns_snapshot_with_fail_soft_sections(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """GET /state aggregates across daemons. In a unit test no daemon
    is reachable (no camilla, no shairport, no voice UDS), so each
    section comes back as null/None — but the response is still 200
    with a stable top-level shape."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod,
        "_aec_full_status",
        lambda: {
            "mode": "auto",
            "bridge_active": True,
            "audio_profile": {
                "requested": "xvf_software_aec3",
                "active": "xvf_software_aec3",
                "state": "active",
                "reason": "Software AEC3 bridge is active.",
            },
            "microphone": {
                "detected": True,
                "processing_mode": "Software AEC3",
                "session_source": "WebRTC AEC3 via :9876",
                "wake_legs": ["AEC3", "Chip-direct raw"],
                "warnings": [],
            },
        },
    )
    state_path = tmp_path / "speaker_volume.json"
    state_path.write_text('{"listening_level": 73}')
    dsp_apply = tmp_path / "dsp_apply_state.json"
    dsp_apply.write_text(json.dumps({
        "source": "sound",
        "phase": "done",
        "result": "success",
    }))
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(state_path))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(dsp_apply))
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing_sound.json"))
    # Provider + model come from the wizard-owned SSOT file, read fresh —
    # NOT from os.environ. Write the file AND set a *different* stale env
    # value to prove the file is authoritative: jasper-control keeps a
    # frozen JASPER_VOICE_PROVIDER across a switch (it isn't restarted),
    # so the file must win. This is the regression guard for the
    # stale-/system/ bug.
    provider_file = tmp_path / "voice_provider.env"
    provider_file.write_text(
        "JASPER_VOICE_PROVIDER=openai\nJASPER_OPENAI_MODEL=gpt-realtime-2\n"
    )
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(provider_file))
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")  # stale env, must be ignored
    # Point librespot state at a missing file → empty dict.
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "missing.json"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert "ts" in body
    assert body["voice"]["provider"] == "openai"
    assert body["voice"]["model"] == "gpt-realtime-2"
    assert body["voice"]["provider_status"] == "configured"
    assert body["voice"]["provider_error"] is None
    assert body["voice"]["reachable"] is False
    assert body["voice"]["session_active"] is False
    assert "music_dbfs" in body["voice"]
    # /state.voice is hand-curated, NOT a session_status pass-through, so a
    # new session_status field is silently dropped if it isn't pulled
    # through in _get_state. wake_legs (jasper-doctor's runtime cross-check
    # source) is exactly such a field — guard that its key is present.
    assert "wake_legs" in body["voice"]
    # tool_packs is the same shape of curated pull-through (jasper-doctor's
    # check_tool_packs cross-checks it against the static registry).
    assert "tool_packs" in body["voice"]
    assert body["audio"]["listening_level_percent"] == 73
    # Camilla isn't reachable from the test → main_volume_db None.
    assert body["audio"]["main_volume_db"] is None
    assert body["audio"]["playback_rms_dbfs"] is None
    assert body["audio"]["playback_peak_dbfs"] is None
    assert body["audio"]["clipped_samples"] is None
    assert body["audio"]["sound"]["curve_id"] == "flat"
    assert body["audio"]["sound"]["filter_count"] == 0
    assert body["audio"]["sound"]["last_dsp_apply"]["result"] == "success"
    assert body["audio"]["sound"]["runtime_state"] == "unknown"
    assert body["audio"]["camilla_active_config_path"] is None
    assert body["renderers"]["spotify"]["playing"] is False
    assert body["outputd"] is None
    assert body["aec"]["audio_profile"]["active"] == "xvf_software_aec3"
    assert body["aec"]["microphone"]["processing_mode"] == "Software AEC3"
    assert body["active_source"] in {"idle", "airplay"}
    assert body["satellites"]["dial"]["online"] is False
    # Transit city packs: a JSON-able {packs: [{id, label, enabled}]} block,
    # read fresh from the wizard-owned transit.env (absent file here -> the
    # legacy all-enabled default). Top-level shape guard.
    assert isinstance(body["transit"]["packs"], list)
    assert any(p["id"] == "nyc" for p in body["transit"]["packs"])


def test_state_aec_probe_failure_is_fail_soft(
    server_with_coordinator, monkeypatch,
):
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    def boom():
        raise RuntimeError("aec probe exploded")

    monkeypatch.setattr(srv_mod, "_aec_full_status", boom)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["aec"] is None
    assert body["voice"]["reachable"] is False


def test_state_transit_read_failure_is_fail_soft(
    server_with_coordinator, monkeypatch,
):
    """If the transit SSOT read raises, /state still returns 200 with a null
    transit section rather than 500 — mirrors the grouping/aec fail-soft
    guard so one broken section never takes the whole snapshot down."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    def boom():
        raise RuntimeError("transit read exploded")

    monkeypatch.setattr(srv_mod, "read_transit_state", boom)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["transit"] is None


def test_state_voice_wake_legs_flows_from_session_status(
    server_with_coordinator, monkeypatch,
):
    """Regression for the curated-vs-passthrough drop: /state.voice is
    hand-built in _get_state, so a session_status field (here wake_legs —
    the runtime-armed legs jasper-doctor cross-checks against configured
    intent) only reaches /state if it's explicitly pulled through. Before
    that pull-through, wake_legs lived in session_status but was absent
    from /state.voice, silently disabling the doctor's runtime check."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    async def fake_status(socket_path, cmd, timeout=None):  # noqa: ARG001
        return {
            "state": "WAKE", "input_ended": False, "spend_allowed": True,
            "connection_paused": False, "mic_muted": False,
            "duck_active": False, "music_dbfs": -32.0,
            "wake_legs": ["on", "off", "dtln"],
        }
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_status)

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["voice"]["reachable"] is True
    assert body["voice"]["wake_legs"] == ["on", "off", "dtln"]


def test_state_voice_tool_packs_flows_from_session_status(
    server_with_coordinator, monkeypatch,
):
    """Same curated-vs-passthrough regression as wake_legs, for tool_packs:
    jasper-voice's session_status reports per-pack registration outcomes,
    and /state.voice must pull the field through for jasper-doctor's
    check_tool_packs to see runtime truth (a pack that failed to build)."""
    base, _ = server_with_coordinator
    import jasper.control.server as srv_mod

    packs = [
        {"name": "audio", "status": "registered", "tool_count": 5,
         "error": None},
        {"name": "spotify", "status": "failed", "tool_count": 0,
         "error": "ImportError('spotipy')"},
    ]

    async def fake_status(socket_path, cmd, timeout=None):  # noqa: ARG001
        return {
            "state": "WAKE", "input_ended": False, "spend_allowed": True,
            "connection_paused": False, "mic_muted": False,
            "duck_active": False, "music_dbfs": -32.0,
            "wake_legs": ["on"], "tool_packs": packs,
        }
    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_status)

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["voice"]["reachable"] is True
    assert body["voice"]["tool_packs"] == packs


def test_state_audio_metrics_sanitize_non_finite_values(
    server_with_coordinator, monkeypatch, tmp_path,
):
    import jasper.camilla as camilla_mod

    class FakeCamilla:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def get_volume_db(self, *, best_effort=False):  # noqa: ARG002
            return -12.345

        async def get_playback_rms(self, *, best_effort=False):  # noqa: ARG002
            return float("-inf"), -32.1234

        async def get_playback_peak(self, *, best_effort=False):  # noqa: ARG002
            return float("nan"), -3.456

        async def get_clipped_samples(self, *, best_effort=False):  # noqa: ARG002
            return 7

    base, _ = server_with_coordinator
    state_path = tmp_path / "speaker_volume.json"
    state_path.write_text('{"listening_level": 73}')
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(state_path))
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "missing.json"),
    )
    monkeypatch.setattr(camilla_mod, "CamillaController", FakeCamilla)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["audio"]["main_volume_db"] == -12.35
    assert body["audio"]["playback_rms_dbfs"] == [None, -32.12]
    assert body["audio"]["playback_peak_dbfs"] == [None, -3.46]
    assert body["audio"]["clipped_samples"] == 7


def test_state_prefers_mux_winner_over_raw_renderer_probe(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """Mux owns the audible source; /state should not fall back to raw
    renderer priority when mux reports an auto winner."""
    import jasper.control.server as srv_mod

    base, _ = server_with_coordinator
    spotify_state = tmp_path / "spotify.json"
    spotify_state.write_text(json.dumps({
        "playing": True,
        "session_active": True,
        "uri": "spotify:track:test",
    }))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(spotify_state))
    monkeypatch.setenv(
        "JASPER_USBSINK_STATE_PATH", str(tmp_path / "missing_usb.json"),
    )
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )

    async def fake_mux_status(cmd: str, **kwargs):  # noqa: ARG001
        assert cmd == "STATUS"
        return {
            "mode": "auto",
            "selected_source": None,
            "winner": "airplay",
            "active_source": "airplay",
            "sources": {
                "airplay": {"playing": True},
                "spotify": {"playing": True},
                "bluetooth": {"playing": False},
                "usbsink": {"playing": False},
            },
        }

    monkeypatch.setattr(srv_mod, "_mux_socket_command", fake_mux_status)

    status, body = _get(f"{base}/state")

    assert status == 200
    assert body["renderers"]["spotify"]["playing"] is True
    assert body["active_source"] == "airplay"
    assert body["source_selection"]["winner"] == "airplay"


async def test_state_audio_volume_policy_surfaces_push_guard(
    monkeypatch, tmp_path,
):
    from jasper import volume_diagnostics
    from jasper.control import server as srv_mod

    spotify_state = tmp_path / "spotify.json"
    spotify_state.write_text(json.dumps({
        "playing": True,
        "session_active": True,
        "uri": "spotify:track:test",
    }))
    volume_state = tmp_path / "speaker_volume.json"
    volume_state.write_text(json.dumps({
        "listening_level": 100,
        "main_volume_db": -12.5,
    }))
    diag_path = tmp_path / "volume_policy.json"
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(spotify_state))
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(volume_state))
    monkeypatch.setenv("JASPER_VOLUME_DIAGNOSTICS_PATH", str(diag_path))
    monkeypatch.setenv(
        "JASPER_USBSINK_STATE_PATH", str(tmp_path / "missing_usb.json"),
    )
    volume_diagnostics.record_source_push(
        "spotify",
        level=100,
        ok=False,
        reason=volume_diagnostics.PUSH_WRITE_FAILED,
    )
    volume_diagnostics.record_push_guard(
        "spotify",
        level=100,
        guard_db=-12.5,
        previous_db=0.0,
        reason=volume_diagnostics.GUARD_PUSH_WRITE_FAILED,
        context="dispatch_spotify_degraded",
    )

    body = await srv_mod._get_state(
        camilla_host="127.0.0.1",
        camilla_port=1234,
        voice_socket_path="/nonexistent.sock",
    )

    policy = body["audio"]["volume_policy"]
    assert policy["active_source"] == "spotify"
    assert policy["source"] == "spotify"
    assert policy["volume_mode"] == "push"
    assert policy["carrier"] == "camilla_guard"
    assert policy["push_guard_active"] is True
    assert policy["guard_db"] == -12.5
    assert policy["guard_reason"] == "push_write_failed"
    assert policy["previous_db"] == 0.0
    assert policy["last_source_push_result"]["reason"] == "write_failed"


def test_state_usbsink_section_null_when_disabled(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """When jasper-usbsink isn't running, no /run/jasper-usbsink/
    state.json exists — the section comes back as null so consumers
    can distinguish "feature off" from "feature on but idle"."""
    base, _ = server_with_coordinator
    monkeypatch.setenv(
        "JASPER_USBSINK_STATE_PATH", str(tmp_path / "missing.json"),
    )
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.json"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["renderers"]["usbsink"] is None


def test_state_usbsink_section_populated_when_enabled(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """When the daemon is publishing, /state surfaces playing,
    preempted, host_connected, rms_dbfs."""
    base, _ = server_with_coordinator
    usbsink_state = tmp_path / "usbsink_state.json"
    usbsink_state.write_text(json.dumps({
        "playing": True, "preempted": False, "host_connected": True,
        "rms_dbfs": -12.3,
        "updated_at": "2026-05-16T00:00:00+00:00",
    }))
    monkeypatch.setenv("JASPER_USBSINK_STATE_PATH", str(usbsink_state))
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.json"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    section = body["renderers"]["usbsink"]
    assert section["playing"] is True
    assert section["preempted"] is False
    assert section["host_connected"] is True
    assert section["rms_dbfs"] == -12.3


def test_state_active_source_resolves_to_usbsink_when_only_usb_playing(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """active_source ranks usbsink above idle but below the named
    renderers — when nothing else is playing and USB is, the field
    surfaces as 'usbsink' so the dashboard renders correctly."""
    base, _ = server_with_coordinator
    usbsink_state = tmp_path / "usbsink_state.json"
    usbsink_state.write_text(json.dumps({
        "playing": True, "preempted": False, "host_connected": True,
        "rms_dbfs": -10.0,
        "updated_at": "2026-05-16T00:00:00+00:00",
    }))
    monkeypatch.setenv("JASPER_USBSINK_STATE_PATH", str(usbsink_state))
    monkeypatch.setenv(
        "JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"),
    )
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.json"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["active_source"] == "usbsink"


def test_state_502_when_aggregator_raises(
    server_with_coordinator, monkeypatch,
):
    """If _get_state itself blows up — not a fail-soft section, but
    something unexpected like a JSON serialization error — the route
    surfaces 502 instead of crashing the server."""
    import jasper.control.server as srv_mod

    async def boom(**kwargs):  # noqa: ARG001
        raise RuntimeError("aggregator broken")

    monkeypatch.setattr(srv_mod, "_get_state", boom)
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/state")
    assert status == 502
    assert "error" in body


def test_state_concurrent_requests_share_one_aggregate(monkeypatch):
    """Burst polls should collapse to one cross-daemon fan-out."""
    import jasper.control.server as srv_mod

    started = threading.Event()
    release = threading.Event()
    calls = 0

    async def fake_get_state(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2), "test did not release state aggregate"
        return {"ok": True, "calls": calls}

    monkeypatch.setattr(srv_mod, "_get_state", fake_get_state)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    results: list[tuple[int, dict]] = []
    try:
        t1 = threading.Thread(
            target=lambda: results.append(_get(f"{base}/state")),
            daemon=True,
        )
        t2 = threading.Thread(
            target=lambda: results.append(_get(f"{base}/state")),
            daemon=True,
        )
        t1.start()
        assert started.wait(timeout=1)
        t2.start()
        time.sleep(0.05)
        assert calls == 1
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)

    assert len(results) == 2
    assert all(item == (200, {"ok": True, "calls": 1}) for item in results)
    assert calls == 1


def test_state_home_assistant_unconfigured(server_with_coordinator, monkeypatch):
    """When JASPER_HA_URL/TOKEN are unset, /state.home_assistant returns
    configured=false with no error — fail-soft for the dashboard."""
    base, _ = server_with_coordinator
    monkeypatch.delenv("JASPER_HA_URL", raising=False)
    monkeypatch.delenv("JASPER_HA_TOKEN", raising=False)

    status, body = _get(f"{base}/state")
    assert status == 200
    ha = body["home_assistant"]
    assert ha["configured"] is False
    assert ha["connected"] is False
    assert ha["error"] is None


def test_state_home_assistant_connected(server_with_coordinator, monkeypatch):
    """Configured + reachable: /state.home_assistant carries instance_name
    + version from /api/config. We monkeypatch probe_status so the test
    never touches the network."""
    import jasper.home_assistant as ha_mod
    base, _ = server_with_coordinator

    monkeypatch.setenv("JASPER_HA_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("JASPER_HA_TOKEN", "test-token")

    async def fake_probe(url, token, *, force=False, verify_ssl=True):
        return {
            "configured": True, "connected": True, "url": url,
            "instance_name": "Brooklyn House", "version": "2026.5.1",
            "error": None,
        }
    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)

    status, body = _get(f"{base}/state")
    assert status == 200
    ha = body["home_assistant"]
    assert ha["configured"] is True
    assert ha["connected"] is True
    assert ha["instance_name"] == "Brooklyn House"
    assert ha["version"] == "2026.5.1"


def test_state_home_assistant_unreachable_fails_soft(server_with_coordinator, monkeypatch):
    """Configured but probe fails: response still 200 with the rest of
    /state intact; home_assistant carries the error string."""
    import jasper.home_assistant as ha_mod
    base, _ = server_with_coordinator

    monkeypatch.setenv("JASPER_HA_URL", "http://homeassistant.local:8123")
    monkeypatch.setenv("JASPER_HA_TOKEN", "test-token")

    async def fake_probe(url, token, *, force=False, verify_ssl=True):
        return {
            "configured": True, "connected": False, "url": url,
            "instance_name": None, "version": None,
            "error": "Couldn't reach Home Assistant — check the URL and token.",
        }
    monkeypatch.setattr(ha_mod, "probe_status", fake_probe)

    status, body = _get(f"{base}/state")
    assert status == 200
    ha = body["home_assistant"]
    assert ha["configured"] is True
    assert ha["connected"] is False
    assert ha["error"]
    # Other /state sections still populated despite HA failure
    assert "audio" in body
    assert "renderers" in body


# --- /session/* endpoints (phase 3) ---


def test_session_start_proxies_to_voice_socket(server_with_voice_socket):
    base, voice_responses, received = server_with_voice_socket
    voice_responses.append({"result": "OK"})
    status, body = _post(f"{base}/session/start", None)
    assert status == 200
    assert body["result"] == "OK"
    assert received == ["START"]


def test_session_end_proxies_to_voice_socket(server_with_voice_socket):
    base, voice_responses, received = server_with_voice_socket
    voice_responses.append({"result": "OK"})
    status, body = _post(f"{base}/session/end", None)
    assert status == 200
    assert received == ["END"]


def test_session_start_busy_409(server_with_voice_socket):
    base, voice_responses, _ = server_with_voice_socket
    voice_responses.append({"result": "BUSY"})
    status, body = _post(f"{base}/session/start", None)
    assert status == 409
    assert body["result"] == "BUSY"


def test_session_start_cap_503(server_with_voice_socket):
    base, voice_responses, _ = server_with_voice_socket
    voice_responses.append({"result": "CAP"})
    status, body = _post(f"{base}/session/start", None)
    assert status == 503


def test_session_end_no_session_409(server_with_voice_socket):
    base, voice_responses, _ = server_with_voice_socket
    voice_responses.append({"result": "NO_SESSION"})
    status, body = _post(f"{base}/session/end", None)
    assert status == 409


def test_session_endpoint_503_when_voice_socket_missing(server_with_coordinator):
    base, _ = server_with_coordinator
    # Fixture passes /nonexistent.sock — connect will FileNotFoundError.
    status, body = _post(f"{base}/session/start", None)
    assert status == 503
    assert "voice_daemon" in body["error"]


# --- _make_duck_active_probe (cross-daemon defer signal) -----------------
#
# Unit tests for the probe factory consumed by per-request
# VolumeCoordinators. Validates the wire format and the fail-open
# error envelope. See docs/HANDOFF-volume.md "Cross-daemon defer signal".


def test_duck_active_probe_returns_true_when_voice_reports_ducked(monkeypatch):
    import asyncio
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        assert cmd == "STATUS"
        return {"state": "LISTENING", "duck_active": True}

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is True


def test_duck_active_probe_returns_false_when_voice_reports_no_duck(monkeypatch):
    import asyncio
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        return {"state": "IDLE", "duck_active": False}

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is False


def test_duck_active_probe_returns_none_on_uds_missing(monkeypatch):
    """Voice daemon socket doesn't exist (jasper-voice crashed or
    never started). Probe must return None so the coordinator falls
    open and the dial keeps working."""
    import asyncio
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        raise FileNotFoundError(socket_path)

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is None


def test_duck_active_probe_returns_none_on_timeout(monkeypatch):
    """Voice daemon is wedged and doesn't respond within 1s. Probe
    fails open so the dial doesn't lock up waiting for it."""
    import asyncio
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is None


def test_duck_active_probe_returns_none_when_field_absent(monkeypatch):
    """Older jasper-voice without the duck_active field — the
    coordinator treats missing/wrong-type as unknown (fail-open)."""
    import asyncio
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        return {"state": "IDLE"}  # no duck_active key

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is None


def test_duck_active_probe_returns_none_when_field_wrong_type(monkeypatch):
    """Defensive: future protocol drift returning a non-bool (string,
    int) shouldn't crash — fail open."""
    import asyncio
    import jasper.control.server as srv_mod

    async def fake_command(socket_path, cmd, *, timeout=5.0):
        return {"state": "IDLE", "duck_active": "true"}  # string, not bool

    monkeypatch.setattr(srv_mod, "_voice_socket_command", fake_command)
    probe = srv_mod._make_duck_active_probe("/tmp/unused.sock")
    assert asyncio.run(probe()) is None


# --- /dial/status (heartbeat) ---


def test_dial_status_empty_when_no_dial_seen(server_with_coordinator):
    """Fresh daemon, no UDP datagrams yet → all heartbeat fields null."""
    import jasper.control.server as srv_mod
    srv_mod._dial_heartbeat["last_seen_at"] = None
    srv_mod._dial_heartbeat["last_seen_ip"] = None
    srv_mod._dial_heartbeat["last_message"] = None
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/dial/status")
    assert status == 200
    assert body["last_seen_at"] is None
    assert body["last_seen_ip"] is None
    assert body["age_seconds"] is None


def test_dial_status_reports_recent_heartbeat(server_with_coordinator):
    """Simulate a UDP datagram by mutating the module heartbeat dict
    (the listener does the same on each datagram). /dial/status should
    then report a recent age."""
    import time
    import jasper.control.server as srv_mod
    now = time.time()
    srv_mod._dial_heartbeat["last_seen_at"] = now - 12.0
    srv_mod._dial_heartbeat["last_seen_ip"] = "192.168.1.89"
    srv_mod._dial_heartbeat["last_message"] = "[encoder] detent=1 → POST 2.00 dB OK"
    base, _ = server_with_coordinator
    status, body = _get(f"{base}/dial/status")
    assert status == 200
    assert body["last_seen_ip"] == "192.168.1.89"
    assert body["age_seconds"] >= 12.0
    assert body["age_seconds"] < 30.0   # generous slack for slow CI
    assert "encoder" in body["last_message"]


# --- Dial reachability probe (powers /state.satellites.dial.online) ---


def test_probe_dial_reachable_returns_true_on_refused():
    """Real-network test: connecting to localhost on a port nothing is
    listening on gets RST → ConnectionRefusedError. The probe treats
    that as "online" because a host has to be alive to send RST."""
    import asyncio
    from jasper.control.server import _probe_dial_reachable

    # Port 1 is privileged and effectively guaranteed unbound on a
    # normal system; ECONNREFUSED is immediate.
    result = asyncio.run(_probe_dial_reachable("127.0.0.1", timeout=1.0))
    assert result is True


def test_probe_dial_reachable_returns_false_on_timeout():
    """Black-hole IP (RFC 5737 TEST-NET-1, not routable) → timeout →
    online=False. Uses a tight timeout so the test stays fast."""
    import asyncio
    from jasper.control.server import _probe_dial_reachable

    result = asyncio.run(_probe_dial_reachable("192.0.2.1", timeout=0.2))
    assert result is False


def test_state_dial_online_true_when_probe_succeeds(
    server_with_coordinator, monkeypatch,
):
    """/state.satellites.dial.online reflects TCP reachability, not
    UDP-dlog freshness. With a recorded last_seen_ip and the probe
    monkeypatched to succeed, the dial is correctly reported online
    even when the last dlog was hours ago."""
    import time
    import jasper.control.server as srv_mod

    # Ancient dlog activity (would have failed the old 30 s threshold).
    srv_mod._dial_heartbeat["last_seen_at"] = time.time() - 3600.0
    srv_mod._dial_heartbeat["last_seen_ip"] = "192.168.1.89"
    srv_mod._dial_heartbeat["last_message"] = "[encoder] detent=1 → POST OK"

    async def fake_probe(ip, *, timeout=0.5):  # noqa: ARG001
        return True
    monkeypatch.setattr(srv_mod, "_probe_dial_reachable", fake_probe)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/state")
    assert status == 200
    dial = body["satellites"]["dial"]
    assert dial["online"] is True
    # age_seconds still reflects the last dlog — useful for "has the
    # user touched the dial lately?" UX, separate from liveness.
    assert dial["age_seconds"] >= 3600.0


def test_state_dial_online_false_when_probe_fails(
    server_with_coordinator, monkeypatch,
):
    """Reachable IP recorded but probe times out (dial powered off /
    out of range) → online=False. last_seen_at still surfaces so the
    dashboard can show "last seen ago" even when unreachable."""
    import time
    import jasper.control.server as srv_mod

    srv_mod._dial_heartbeat["last_seen_at"] = time.time() - 10.0
    srv_mod._dial_heartbeat["last_seen_ip"] = "192.168.1.89"
    srv_mod._dial_heartbeat["last_message"] = "[encoder] detent=1 → POST OK"

    async def fake_probe(ip, *, timeout=0.5):  # noqa: ARG001
        return False
    monkeypatch.setattr(srv_mod, "_probe_dial_reachable", fake_probe)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/state")
    assert status == 200
    dial = body["satellites"]["dial"]
    assert dial["online"] is False
    assert dial["age_seconds"] is not None


def test_state_dial_online_false_when_no_last_seen_ip(
    server_with_coordinator, monkeypatch,
):
    """No dlog yet → no IP to probe → online=False without ever
    calling the probe. Asserts the probe isn't called so a fresh
    daemon doesn't add probe latency to /state."""
    import jasper.control.server as srv_mod

    srv_mod._dial_heartbeat["last_seen_at"] = None
    srv_mod._dial_heartbeat["last_seen_ip"] = None
    srv_mod._dial_heartbeat["last_message"] = None

    called = []
    async def fake_probe(ip, *, timeout=0.5):  # noqa: ARG001
        called.append(ip)
        return True
    monkeypatch.setattr(srv_mod, "_probe_dial_reachable", fake_probe)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["satellites"]["dial"]["online"] is False
    assert called == []


# --- Dial heartbeat persistence (survives jasper-control restart) ---


def test_load_dial_heartbeat_missing_file_returns_defaults(tmp_path, monkeypatch):
    """No persisted file → the loader returns the empty defaults.
    Daemon startup must not block on a missing heartbeat file."""
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "DIAL_HEARTBEAT_PATH", str(tmp_path / "missing.json"),
    )
    out = srv_mod._load_dial_heartbeat()
    assert out == {"last_seen_at": None, "last_seen_ip": None, "last_message": None}


def test_load_dial_heartbeat_malformed_file_returns_defaults(tmp_path, monkeypatch):
    """A corrupted persisted file (truncated, garbled, hand-edited
    into invalid JSON) must not block startup — fall back to defaults
    and let the next dlog refresh the state."""
    import jasper.control.server as srv_mod

    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json, last_seen_ip: ...")
    monkeypatch.setattr(srv_mod, "DIAL_HEARTBEAT_PATH", str(bad))
    out = srv_mod._load_dial_heartbeat()
    assert out["last_seen_ip"] is None


def test_load_dial_heartbeat_wrong_types_returns_defaults_per_field(
    tmp_path, monkeypatch,
):
    """Defensive: if someone hand-edits the file and sets last_seen_at
    to a string or last_seen_ip to a number, drop the bad value rather
    than propagating it into /state.satellites.dial.

    Other fields with valid types still come through — partial-bad
    files aren't completely thrown away."""
    import json
    import jasper.control.server as srv_mod

    path = tmp_path / "mixed.json"
    path.write_text(json.dumps({
        "last_seen_at": "not-a-float",   # wrong type
        "last_seen_ip": "192.168.1.89",  # valid
        "last_message": 42,              # wrong type
    }))
    monkeypatch.setattr(srv_mod, "DIAL_HEARTBEAT_PATH", str(path))
    out = srv_mod._load_dial_heartbeat()
    assert out["last_seen_at"] is None
    assert out["last_seen_ip"] == "192.168.1.89"
    assert out["last_message"] is None


def test_persist_then_load_roundtrip(tmp_path, monkeypatch):
    """Write a heartbeat, read it back. The path must survive across
    `jasper-control` lifetime — this is the actual gap-closer."""
    import jasper.control.server as srv_mod

    path = tmp_path / "round.json"
    monkeypatch.setattr(srv_mod, "DIAL_HEARTBEAT_PATH", str(path))
    snapshot = {
        "last_seen_at": 1779550000.0,
        "last_seen_ip": "192.168.1.89",
        "last_message": "[encoder] detent=1 → POST OK",
    }
    srv_mod._persist_dial_heartbeat(snapshot)
    assert path.exists()
    loaded = srv_mod._load_dial_heartbeat()
    assert loaded == snapshot


def test_persist_dial_heartbeat_is_atomic_via_tempfile(tmp_path, monkeypatch):
    """Writes go through a `.tmp` file then `os.replace` — guarantees
    a reader never sees a half-written file even if the daemon crashes
    mid-write. We check the temp file is cleaned up after a normal
    write (replace removes it)."""
    import jasper.control.server as srv_mod

    path = tmp_path / "atomic.json"
    monkeypatch.setattr(srv_mod, "DIAL_HEARTBEAT_PATH", str(path))
    srv_mod._persist_dial_heartbeat({
        "last_seen_at": 1.0, "last_seen_ip": "1.2.3.4", "last_message": "x",
    })
    assert path.exists()
    # tempfile sibling shouldn't linger after a successful write.
    assert not (tmp_path / "atomic.json.tmp").exists()


def test_persist_dial_heartbeat_fails_soft_on_io_error(tmp_path, monkeypatch, caplog):
    """An unwritable directory (e.g. read-only fs) must not crash the
    UDP listener — log a warning and continue. Heartbeat is best-effort."""
    import logging
    import jasper.control.server as srv_mod

    # Point at a path under a regular file (definitely can't mkdir
    # there) so the makedirs call raises.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(
        srv_mod, "DIAL_HEARTBEAT_PATH", str(blocker / "child" / "hb.json"),
    )
    with caplog.at_level(logging.WARNING, logger="jasper.dial"):
        srv_mod._persist_dial_heartbeat({
            "last_seen_at": 1.0, "last_seen_ip": "1.2.3.4", "last_message": "x",
        })
    # Did not raise; warning was logged.
    assert any(
        "dial heartbeat persistence" in rec.message for rec in caplog.records
    )


def test_state_dial_online_uses_persisted_ip_after_restart_simulation(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """End-to-end of the gap-closer: simulate a daemon restart by
    writing a heartbeat file, clearing the in-memory dict, then
    reloading. /state should then probe the persisted IP and return
    online=true without anyone touching the dial.

    This is the only scenario the persistence work exists to fix —
    everything else is plumbing."""
    import json
    import jasper.control.server as srv_mod

    # Simulate a previous daemon's persisted state.
    path = tmp_path / "hb.json"
    path.write_text(json.dumps({
        "last_seen_at": 1779550000.0,
        "last_seen_ip": "192.168.1.89",
        "last_message": "[encoder] detent=1 → POST OK",
    }))
    monkeypatch.setattr(srv_mod, "DIAL_HEARTBEAT_PATH", str(path))

    # Simulate fresh-process startup: reload the heartbeat into the
    # module-level dict.
    fresh = srv_mod._load_dial_heartbeat()
    for k, v in fresh.items():
        srv_mod._dial_heartbeat[k] = v

    # Stub the TCP probe so the test doesn't touch the network.
    probed = []
    async def fake_probe(ip, *, timeout=0.5):  # noqa: ARG001
        probed.append(ip)
        return True
    monkeypatch.setattr(srv_mod, "_probe_dial_reachable", fake_probe)

    base, _ = server_with_coordinator
    status, body = _get(f"{base}/state")
    assert status == 200
    assert body["satellites"]["dial"]["online"] is True
    assert probed == ["192.168.1.89"]


# --- Regression tests for the BuildResult return-shape change ---


def test_make_spotify_router_consumes_build_result_correctly(tmp_path, monkeypatch):
    """Pin the BuildResult shape consumption for control/server.py's
    _build_spotify_router_or_none. Same regression as in mux:
    previously `clients = build_clients(...)` was treated as a dict;
    the change to BuildResult silently broke the volume-coordinator
    wiring."""
    from unittest.mock import patch, MagicMock
    from jasper.control.server import _build_spotify_router_or_none
    from jasper.spotify_router import (
        ACCOUNT_OK, AccountClient, AccountStatus, BuildResult, Router,
    )
    from jasper.accounts import Account

    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "a" * 32)
    monkeypatch.setenv(
        "JASPER_SPOTIFY_ACCOUNTS_PATH", str(tmp_path / "accounts.json"),
    )
    (tmp_path / "accounts.json").write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "/nope"}], '
        '"default": "jasper"}'
    )

    fake_client = AccountClient(
        account=Account(name="jasper", cache_path="/nope"),
        sp=MagicMock(),
    )

    def fake_build_clients(_registry, *, client_id, redirect_uri):
        return BuildResult(
            clients={"jasper": fake_client},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        )

    with patch("jasper.spotify_router.build_clients", side_effect=fake_build_clients):
        router = _build_spotify_router_or_none()

    assert isinstance(router, Router)
    assert isinstance(router.clients, dict)
    assert "jasper" in router.clients
    assert router.statuses[0].state == ACCOUNT_OK


@pytest.mark.asyncio
async def test_dispatch_transport_reuses_spotify_router_helper(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.renderer as renderer_mod
    import jasper.tools.transport as transport_mod

    router = object()
    seen = {}

    class FakeRendererClient:
        def __init__(self, **kwargs):
            seen["renderer_kwargs"] = kwargs

    def fake_make_transport_dispatcher(renderer, spotify_router):
        seen["renderer"] = renderer
        seen["spotify_router"] = spotify_router

        async def dispatch(action):
            return {"action": action}

        return dispatch

    monkeypatch.setattr(srv_mod, "_build_spotify_router_or_none", lambda: router)
    monkeypatch.setattr(renderer_mod, "RendererClient", FakeRendererClient)
    monkeypatch.setattr(
        transport_mod,
        "make_transport_dispatcher",
        fake_make_transport_dispatcher,
    )

    result = await srv_mod._dispatch_transport("toggle")

    assert result == {"action": "toggle"}
    assert isinstance(seen["renderer"], FakeRendererClient)
    assert seen["spotify_router"] is router


# ---------------------------------------------------------------------------
# Audit C2 — systemd watchdog plumbing: the HTTP accept loop must drive
# the Heartbeat progress sentinel so a wedged loop stops the WATCHDOG=1
# pats (Type=notify + WatchdogSec in the unit).
# ---------------------------------------------------------------------------


class _StubHeartbeat:
    def __init__(self):
        self.bumps = 0

    def bump(self):
        self.bumps += 1


def _make_loopback_control_server():
    from jasper.control.server import ControlHTTPServer

    return ControlHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)


def test_control_http_server_sheds_when_worker_cap_is_full():
    from jasper.control.server import ControlHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class _BlockingHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, A003
            return None

        def do_GET(self):  # noqa: N802
            entered.set()
            assert release.wait(timeout=2), "test did not release handler"
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ControlHTTPServer(
        ("127.0.0.1", 0),
        _BlockingHandler,
        max_workers=1,
        request_timeout_sec=1.0,
    )
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    first: list[tuple[int, dict]] = []
    try:
        t = threading.Thread(
            target=lambda: first.append(_get(f"{base}/first")),
            daemon=True,
        )
        t.start()
        assert entered.wait(timeout=1)

        status, body = _get(f"{base}/second")
        assert status == 429
        assert body["error"] == "server_overloaded"
        assert body["retry_after"] == 1

        release.set()
        t.join(timeout=2)
        assert first == [(200, {"ok": True})]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)


def test_control_http_server_coalesces_overload_logs(caplog):
    from jasper.control.server import ControlHTTPServer

    now = 100.0

    def clock() -> float:
        return now

    server = ControlHTTPServer(
        ("127.0.0.1", 0),
        BaseHTTPRequestHandler,
        overload_log_interval_sec=5.0,
        clock=clock,
    )
    try:
        with caplog.at_level("WARNING", logger="jasper.control.server"):
            server._log_overloaded(("127.0.0.1", 1001))
            server._log_overloaded(("127.0.0.1", 1002))
            server._log_overloaded(("127.0.0.1", 1003))
            now = 105.1
            server._log_overloaded(("127.0.0.1", 1004))
    finally:
        server.server_close()

    messages = [
        record.getMessage()
        for record in caplog.records
        if "event=control.overloaded" in record.getMessage()
    ]
    assert len(messages) == 2
    assert "suppressed=0" in messages[0]
    assert "suppressed=2" in messages[1]


def test_service_actions_bumps_attached_heartbeat():
    server = _make_loopback_control_server()
    try:
        hb = _StubHeartbeat()
        server.heartbeat = hb
        server.service_actions()
        server.service_actions()
        assert hb.bumps == 2
    finally:
        server.server_close()


def test_service_actions_without_heartbeat_is_a_noop():
    """Tests / dev runs construct the server without a heartbeat —
    service_actions must not require one."""
    server = _make_loopback_control_server()
    try:
        server.service_actions()  # must not raise
    finally:
        server.server_close()


def test_serve_forever_loop_drives_heartbeat_bumps():
    """End-to-end plumbing: serve_forever's poll loop (the thing
    WatchdogSec is guarding) is what produces progress bumps — no
    requests needed. A wedged loop therefore stops bumping by
    construction."""
    import threading

    server = _make_loopback_control_server()
    hb = _StubHeartbeat()
    server.heartbeat = hb
    t = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    t.start()
    try:
        deadline = time.time() + 2.0
        while hb.bumps < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert hb.bumps >= 3
    finally:
        server.shutdown()
        t.join(timeout=2.0)
        server.server_close()


def test_control_unit_declares_notify_watchdog():
    """The unit file half of C2: Type=notify + WatchdogSec must stay
    paired with the in-process heartbeat (either one alone is broken —
    notify without pings hangs startup; pings without notify are
    ignored)."""
    unit = (REPO_ROOT / "deploy" / "systemd" / "jasper-control.service").read_text()
    assert "Type=notify" in unit
    assert "Type=simple" not in unit
    assert re.search(r"^WatchdogSec=\d+s?$", unit, re.M)


def test_sigterm_handler_requests_shutdown_from_helper_thread():
    import jasper.control.server as srv_mod

    shutdown_seen = threading.Event()
    thread_names: list[str] = []

    class FakeServer:
        def shutdown(self):
            thread_names.append(threading.current_thread().name)
            shutdown_seen.set()

    restore = srv_mod._install_sigterm_shutdown(FakeServer())
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert shutdown_seen.wait(timeout=2)
    finally:
        restore()

    assert thread_names == ["control-sigterm-shutdown"]


def test_stop_peering_daemon_stops_loop_and_runs_daemon_stop(monkeypatch):
    import jasper.control.server as srv_mod
    import jasper.peering as peering_pkg
    import jasper.peering.daemon as peering_daemon_mod

    started = threading.Event()
    stopped = threading.Event()

    class _Mode:
        value = "on"

    class _Config:
        enabled = True
        mode = _Mode()

    class FakePeeringDaemon:
        def __init__(self, cfg):
            self.cfg = cfg

        async def start(self):
            started.set()

        async def stop(self):
            stopped.set()

    monkeypatch.setattr(peering_pkg, "load_config", lambda: _Config())
    monkeypatch.setattr(peering_daemon_mod, "PeeringDaemon", FakePeeringDaemon)
    with srv_mod._peering_lock:
        srv_mod._peering_thread = None
        srv_mod._peering_loop = None
    try:
        srv_mod.start_peering_daemon_if_enabled()
        assert started.wait(timeout=2)
        srv_mod.stop_peering_daemon(timeout=2)
        assert stopped.wait(timeout=2)
        with srv_mod._peering_lock:
            assert srv_mod._peering_thread is None
            assert srv_mod._peering_loop is None
    finally:
        srv_mod.stop_peering_daemon(timeout=1)
        with srv_mod._peering_lock:
            srv_mod._peering_thread = None
            srv_mod._peering_loop = None


# ---------------------------------------------------------------------------
# Bonded-follower volume proxy — /volume* forwards to the pair leader.
# ---------------------------------------------------------------------------


def _grouping_cfg(**kw):
    from jasper.multiroom.config import GroupingConfig
    base = dict(enabled=True, role="follower", channel="right",
                bond_id="bond-1", leader_addr="jts.local", buffer_ms=400,
                codec="flac", error=None)
    base.update(kw)
    return GroupingConfig(**base)


def test_pair_follower_leader_addr_resolution(monkeypatch):
    """Only an ACTIVE bonded follower forwards: leader, solo, and
    fail-LOUD-invalid configs all resolve to None (local handling)."""
    import jasper.multiroom.config as mcfg
    import jasper.control.server as srv_mod

    cases = [
        (_grouping_cfg(), "jts.local"),
        (_grouping_cfg(role="leader", leader_addr=""), None),
        (_grouping_cfg(enabled=False), None),
        (_grouping_cfg(error="broken"), None),
        (_grouping_cfg(leader_addr=""), None),
    ]
    for cfg, want in cases:
        monkeypatch.setattr(mcfg, "load_config", lambda *a, _c=cfg, **k: _c)
        assert srv_mod._pair_follower_leader_addr() == want


class _FakeUpstream:
    """Context-manager response double for urllib.request.urlopen."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


@pytest.fixture
def follower_server(monkeypatch, server_with_coordinator):
    """The coordinator server, with this speaker patched into an active
    bonded follower and the upstream leader call captured."""
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "_pair_follower_leader_addr", lambda: "jts.local",
    )
    seen: list = []

    def fake_urlopen(req, timeout=None):
        seen.append((req, timeout))
        return _FakeUpstream(b'{"db": -15.0, "percent": 70}')

    monkeypatch.setattr(srv_mod, "_pair_urlopen", fake_urlopen)
    base, fake = server_with_coordinator
    return base, fake, seen


def test_follower_get_volume_forwards_to_leader(follower_server):
    base, fake, seen = follower_server
    status, body = _get(f"{base}/volume")
    assert status == 200
    # The leader's payload is relayed, tagged with the pair leader.
    assert body == {"db": -15.0, "percent": 70, "pair_leader": "jts.local"}
    assert fake.calls == []  # the LOCAL coordinator was never touched
    req, timeout = seen[0]
    assert req.full_url.startswith("http://jts.local:")
    assert req.full_url.endswith("/volume")
    assert req.get_header("X-jts-pair-forwarded") == "1"
    assert timeout == 2.5


def test_follower_post_volume_set_relays_body_verbatim(follower_server):
    base, fake, seen = follower_server
    status, body = _post(f"{base}/volume/set", {"percent": 35})
    assert status == 200
    assert body["pair_leader"] == "jts.local"
    assert fake.calls == []
    req, _ = seen[0]
    assert req.full_url.endswith("/volume/set")
    assert json.loads(req.data) == {"percent": 35}


def test_follower_forward_loop_is_broken(follower_server):
    """A request that already carries the forward marker is never forwarded
    again — two mutual followers must error, not ping-pong."""
    base, fake, seen = follower_server
    req = urllib.request.Request(
        f"{base}/volume", headers={"X-JTS-Pair-Forwarded": "1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            status, body = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status, body = e.code, json.loads(e.read())
    assert status == 502
    assert "loop" in body["error"]
    assert seen == []  # no upstream call attempted


def test_follower_forward_failure_is_502_with_leader_named(
    monkeypatch, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "_pair_follower_leader_addr", lambda: "jts.local",
    )

    def exploding_urlopen(req, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr(srv_mod, "_pair_urlopen", exploding_urlopen)
    base, fake = server_with_coordinator
    status, body = _get(f"{base}/volume")
    assert status == 502
    assert body["pair_leader"] == "jts.local"
    assert "unreachable" in body["error"]
    assert fake.calls == []


def test_volume_mute_explicit_set_is_idempotent(server_with_coordinator):
    """{"muted": true} twice stays muted; {"muted": false} when already
    unmuted returns the current level untouched — the explicit-set shape
    voice's distinct mute/unmute intents need (a toggle would invert a
    stale intent)."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {"muted": True})
    assert status == 200 and body["percent"] == 0
    status, body = _post(f"{base}/volume/mute", {"muted": True})
    assert status == 200 and body["percent"] == 0  # still muted, no unmute
    status, body = _post(f"{base}/volume/mute", {"muted": False})
    assert status == 200 and body["percent"] == 60  # restored pre-mute level
    status, body = _post(f"{base}/volume/mute", {"muted": False})
    assert status == 200 and body["percent"] == 60  # unchanged, no re-mute


def test_volume_mute_empty_body_keeps_legacy_toggle(server_with_coordinator):
    """HID accessory clicks post an empty body — the toggle contract is
    load-bearing for them and must survive the explicit-set addition."""
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200 and body["percent"] == 0      # toggled to muted
    status, body = _post(f"{base}/volume/mute", {})
    assert status == 200 and body["percent"] == 60     # toggled back


def test_volume_mute_non_bool_muted_is_400(server_with_coordinator):
    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/volume/mute", {"muted": "yes"})
    assert status == 400
    assert "boolean" in body["error"]


def test_follower_forward_relays_leader_http_verdict(
    monkeypatch, server_with_coordinator,
):
    """A leader that ANSWERS with 4xx/5xx is relayed verbatim (status +
    JSON body, pair_leader-tagged) — never mislabeled 'unreachable'. Only
    transport failures take the 502 path."""
    import io
    import jasper.control.server as srv_mod

    monkeypatch.setattr(
        srv_mod, "_pair_follower_leader_addr", lambda: "jts.local",
    )

    def rejecting_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", hdrs=None,
            fp=io.BytesIO(b'{"error": "percent must be an integer"}'),
        )

    monkeypatch.setattr(srv_mod, "_pair_urlopen", rejecting_urlopen)
    base, fake = server_with_coordinator
    status, body = _post(f"{base}/volume/set", {"percent": "shout"})
    assert status == 400
    assert body == {"error": "percent must be an integer",
                    "pair_leader": "jts.local"}
    assert fake.calls == []  # local coordinator untouched


def test_follower_transport_toggle_forwards_to_leader(follower_server):
    """A dial paired to the follower sends play/pause here; with the
    renderer stack parked the local mux has nothing to toggle — the
    leader owns playback, so transport forwards exactly like volume."""
    base, fake, seen = follower_server
    status, body = _post(f"{base}/transport/toggle", {})
    assert status == 200
    assert body["pair_leader"] == "jts.local"
    req, _ = seen[0]
    assert req.full_url.endswith("/transport/toggle")


def test_system_restart_voice_409s_while_parked(monkeypatch, server_with_coordinator):
    """The dashboard's restart-voice button must not boot the parked
    daemon on a bonded follower — refuse with the pair story."""
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "_pair_follower_leader_addr", lambda: "jts.local")
    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/system/restart/voice", {})
    assert status == 409
    assert "parked" in body["error"]


def test_system_restart_audio_keeps_parked_renderers_parked(
    monkeypatch, server_with_coordinator,
):
    """restart-audio on a follower touches only the units the profile
    keeps alive (camilla) — never the parked renderer stack."""
    import jasper.control.server as srv_mod

    monkeypatch.setattr(srv_mod, "_pair_follower_leader_addr", lambda: "jts.local")
    seen = []

    def fake_popen(argv, **kw):
        seen.append(list(argv))
        class _P:
            pass
        return _P()

    monkeypatch.setattr(srv_mod.subprocess, "Popen", fake_popen)
    base, _fake = server_with_coordinator
    status, _body = _post(f"{base}/system/restart/audio", {})
    assert status == 200
    flat = [a for argv in seen for a in argv]
    assert "jasper-camilla.service" in flat
    assert "librespot.service" not in flat
    assert "shairport-sync.service" not in flat


def test_grouping_set_trim_settable_validated_and_preserved(
    monkeypatch, server_with_coordinator,
):
    """trim_db: settable (validated attenuate-only), rejected when
    garbage or positive, and PRESERVED when omitted — bond/swap fan-outs
    never send it, so a calibrated balance survives role changes."""
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "follower", "channel": "right",
            "bond_id": "b", "leader_addr": "jts.local"}

    status, _ = _post(f"{base}/grouping/set", {**body, "trim_db": -2.5})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_TRIM_DB"] == "-2.5"

    status, resp = _post(f"{base}/grouping/set", {**body, "trim_db": 1.5})
    assert status == 400 and "must be between" in resp["error"]

    status, resp = _post(f"{base}/grouping/set", {**body, "trim_db": "loud"})
    assert status == 400 and "must be a number" in resp["error"]

    status, _ = _post(f"{base}/grouping/set", body)  # omitted
    assert status == 200
    assert "JASPER_GROUPING_TRIM_DB" not in writes[-1]


def test_grouping_set_latency_and_delay_settable_validated_and_preserved(
    monkeypatch, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "leader", "channel": "left",
            "bond_id": "b", "leader_addr": ""}

    status, _ = _post(
        f"{base}/grouping/set",
        {
            **body,
            "client_latency_ms": 11,
            "left_delay_ms": 1.25,
            "right_delay_ms": 0.5,
        },
    )
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_CLIENT_LATENCY_MS"] == "11"
    assert writes[-1]["JASPER_GROUPING_LEFT_DELAY_MS"] == "1.250"
    assert writes[-1]["JASPER_GROUPING_RIGHT_DELAY_MS"] == "0.500"

    status, resp = _post(
        f"{base}/grouping/set", {**body, "client_latency_ms": "soon"},
    )
    assert status == 400 and "client_latency_ms must be an integer" in resp["error"]

    status, resp = _post(
        f"{base}/grouping/set", {**body, "left_delay_ms": -0.1},
    )
    assert status == 400 and "LEFT_DELAY_MS" in resp["error"]

    status, _ = _post(f"{base}/grouping/set", body)
    assert status == 200
    assert "JASPER_GROUPING_CLIENT_LATENCY_MS" not in writes[-1]
    assert "JASPER_GROUPING_LEFT_DELAY_MS" not in writes[-1]
    assert "JASPER_GROUPING_RIGHT_DELAY_MS" not in writes[-1]


def test_grouping_set_peer_roster_settable_preserved_and_cleared(
    monkeypatch, server_with_coordinator,
):
    """Bond roster fields: settable (validated private-IPv4), PRESERVED
    when omitted (swap/trim fan-outs never send them), and CLEARED by an
    explicit empty string (the bond flow clears non-leader members so a
    role flip can't leave a stale roster)."""
    import jasper.control.server as srv_mod

    writes = []
    monkeypatch.setattr(
        srv_mod, "_atomic_rewrite_env",
        lambda path, updates: writes.append(dict(updates)),
    )
    monkeypatch.setattr(srv_mod, "_kick_grouping_reconciler", lambda: None)
    base, _fake = server_with_coordinator
    body = {"enabled": True, "role": "leader", "channel": "left",
            "bond_id": "b", "leader_addr": ""}

    status, _ = _post(f"{base}/grouping/set",
                      {**body, "peer_addr": "192.168.1.9",
                       "peer_name": "JTS3"})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_PEER_ADDR"] == "192.168.1.9"
    assert writes[-1]["JASPER_GROUPING_PEER_NAME"] == "JTS3"

    status, resp = _post(f"{base}/grouping/set",
                         {**body, "peer_addr": "8.8.8.8"})
    assert status == 400 and "private/loopback" in resp["error"]

    status, _ = _post(f"{base}/grouping/set", body)  # omitted → preserved
    assert status == 200
    assert "JASPER_GROUPING_PEER_ADDR" not in writes[-1]
    assert "JASPER_GROUPING_PEER_NAME" not in writes[-1]

    status, _ = _post(f"{base}/grouping/set",
                      {**body, "peer_addr": "", "peer_name": ""})
    assert status == 200
    assert writes[-1]["JASPER_GROUPING_PEER_ADDR"] == ""
    assert writes[-1]["JASPER_GROUPING_PEER_NAME"] == ""


# --------------------------------------------------------------------------
# Control-token gate (jasper/control/control_token.py).
#
# The primitive is fail-safe-open with no token file, but production startup
# auto-generates one so the high-impact route set requires a matching
# X-JTS-Token header by default. Ungated routes (/volume*, /healthz, …) are
# never affected.
# --------------------------------------------------------------------------

# Derived from the server's own frozenset so a newly gated route is exercised
# automatically and the two can never drift (WS1 Phase 2 added
# /system/restart/voice + /system/restart/audio). The gate rejects before any
# route dispatch, so iterating restart routes here triggers no side effect.
import jasper.control.server as _srv_mod  # noqa: E402

_GATED_ROUTES = tuple(sorted(_srv_mod._TOKEN_GATED_ROUTES))


def test_grouping_set_stays_in_token_gated_routes():
    """Pin the membership invariant the rest of this section derives from.

    The gate-behavior tests below iterate `_TOKEN_GATED_ROUTES` itself, so
    they'd stay green if `/grouping/set` were dropped — they'd just exercise
    one fewer route. This test fails on that removal: `/grouping/set` MUST
    remain token-gated. Dropping it would silently re-open the
    multiroom-vs-privilege-separation contradiction (WS1 Phase 2 made the gate
    mandatory; someone "fixing" the cross-device grouping fan-out by removing
    the gate is exactly the regression this pins). The household-credential
    work (HANDOFF-control-plane-auth.md) accepts that credential *in addition*
    to the control token on this route — it never un-gates it.
    """
    assert "/grouping/set" in _srv_mod._TOKEN_GATED_ROUTES
    # The full expected gated set, so adding/removing any route is a
    # deliberate, reviewed change rather than a silent drift.
    assert _srv_mod._TOKEN_GATED_ROUTES == frozenset({
        "/system/poweroff",
        "/system/reboot",
        "/system/restart/voice",
        "/system/restart/audio",
        "/mic/mute",
        "/grouping/set",
    })


def _enable_control_token(monkeypatch, tmp_path, token="t0ken-value"):
    """Point control_token at a tmp file containing `token` (gate ENABLED)."""
    import jasper.control.control_token as ct

    path = tmp_path / "control_token"
    path.write_text(token + "\n")
    monkeypatch.setattr(ct, "TOKEN_FILE", str(path))
    return token


def _disable_control_token(monkeypatch, tmp_path):
    """Point control_token at an absent file (primitive fail-safe-open)."""
    import jasper.control.control_token as ct

    monkeypatch.setattr(ct, "TOKEN_FILE", str(tmp_path / "absent"))


def _pair_household(monkeypatch, tmp_path, secret="hh-secret-value"):
    """Point household_credential at a tmp file containing `secret` (PAIRED).

    A paired speaker is the steady state: its /grouping/set requires EITHER the
    control token OR this household credential (HANDOFF-control-plane-auth.md §6).
    """
    import jasper.control.household_credential as hc

    path = tmp_path / "household_secret"
    path.write_text(secret + "\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(path))
    return secret


def _unpair_household(monkeypatch, tmp_path):
    """Point household_credential at an absent file (NOT yet paired).

    Unpaired ⇒ verify() fail-safe-accepts, so /grouping/set is open — the
    deliberate bootstrap window (the secret is distributed over that very route).
    """
    import jasper.control.household_credential as hc

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "absent_household"))


def test_default_off_gated_routes_reach_handlers(
    monkeypatch, tmp_path, server_with_coordinator, server_with_voice_socket,
):
    """Primitive fail-open invariant: with NO token file, none of the gated
    routes return control_token_required — each reaches its real handler
    exactly as before the gate existed. We assert the *gate* is a no-op by
    confirming no 403 control_token_required comes back; the handler's own
    success/failure is covered by the per-route tests elsewhere."""
    base, _ = server_with_coordinator
    _disable_control_token(monkeypatch, tmp_path)
    # _grouping_test_setup also patches subprocess.Popen module-wide with a
    # FakePopen, so the /system/poweroff|reboot calls below hit the fake — no
    # test machine reboots. /grouping/set has a clean 200 path under the
    # fixture; use it to prove the request flows straight through the (off)
    # gate to the handler.
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    status, body = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 200
    assert body.get("error") != "control_token_required"
    # The other gated routes: never the token error when the gate is off.
    for route in ("/system/poweroff", "/system/reboot", "/mic/mute"):
        status, body = _post(f"{base}{route}", {"muted": True})
        assert body.get("error") != "control_token_required", route


def test_enabled_gated_routes_403_without_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """With the gate enabled, every gated route 403s control_token_required
    when no X-JTS-Token is sent — including before any side effect runs.

    /grouping/set also consults the household credential, so we PAIR the speaker
    here: a paired speaker rejects a tokenless, householdless call on every gated
    route (an UNPAIRED speaker's /grouping/set is the open bootstrap window,
    pinned separately below)."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _pair_household(monkeypatch, tmp_path)
    for route in _GATED_ROUTES:
        status, body = _post(f"{base}{route}", {"muted": True})
        assert status == 403, route
        assert body["error"] == "control_token_required", route


def test_enabled_gated_routes_403_with_wrong_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path, token="correct-horse")
    _pair_household(monkeypatch, tmp_path)  # paired: /grouping/set also gated
    for route in _GATED_ROUTES:
        status, body = _post(
            f"{base}{route}", {"muted": True},
            headers={"X-JTS-Token": "wrong-token"},
        )
        assert status == 403, route
        assert body["error"] == "control_token_required", route


def test_enabled_gated_route_succeeds_with_matching_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """The matching token lets the request through to the handler. Uses
    /grouping/set (a clean 200 under the fixture) so success is unambiguous."""
    base, _ = server_with_coordinator
    token = _enable_control_token(monkeypatch, tmp_path)
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Token": token},
    )
    assert status == 200
    assert body["ok"] is True
    assert "JASPER_GROUPING=on" in env.read_text()


def test_enabled_gate_does_not_affect_ungated_routes(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """With the gate enabled, an UNgated route (/volume/set, and the
    /healthz read) works with no token — the dial's low-impact controls stay
    open by design."""
    base, fake = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    # Ungated POST: no token, still succeeds.
    status, body = _post(f"{base}/volume/set", {"percent": 42})
    assert status == 200
    assert body["percent"] == 42
    # Ungated GET liveness: unaffected.
    status, body = _get(f"{base}/healthz")
    assert status == 200 and body == {"ok": True}


# --------------------------------------------------------------------------
# Household credential on /grouping/set (jasper/control/household_credential.py).
#
# The device-to-device gate: a peer fan-out / autonomous re-group presents the
# household secret as X-JTS-Household, verified against each member's persisted
# copy — NOT the per-device CSRF token a leader can't hold for a follower. The
# gate accepts EITHER on /grouping/set ONLY; every other gated route stays
# control-token-only. Fail-safe (absent ⇒ accept) so the first bond, which
# distributes the secret over this very route, isn't rejected by the gate it
# installs. Full design: docs/HANDOFF-control-plane-auth.md §6.
# --------------------------------------------------------------------------


def test_grouping_set_accepts_household_credential_without_token(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """A paired member accepts /grouping/set on a valid X-JTS-Household with NO
    X-JTS-Token — the cross-device path. Gate accepts EITHER credential."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)  # CSRF gate armed
    secret = _pair_household(monkeypatch, tmp_path)
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Household": secret},
    )
    assert status == 200
    assert body["ok"] is True
    assert "JASPER_GROUPING=on" in env.read_text()


def test_grouping_set_403_without_either_credential_when_paired(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """Once a household is bonded, a tokenless+householdless caller can no longer
    flip grouping — the whole point of the gate."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _pair_household(monkeypatch, tmp_path)
    status, body = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 403
    assert body["error"] == "control_token_required"


def test_grouping_set_403_with_wrong_household(
    monkeypatch, tmp_path, server_with_coordinator,
):
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _pair_household(monkeypatch, tmp_path, secret="correct-household")
    status, body = _post(
        f"{base}/grouping/set", {"enabled": False},
        headers={"X-JTS-Household": "wrong-household"},
    )
    assert status == 403
    assert body["error"] == "control_token_required"


def test_household_credential_not_accepted_on_other_gated_routes(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """Scope: X-JTS-Household authorizes ONLY /grouping/set. The other gated
    routes are browser→own-speaker and stay control-token-only — a household
    bearer must NOT open /mic/mute, poweroff, reboot, or the restart routes."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret = _pair_household(monkeypatch, tmp_path)
    for route in (
        "/system/poweroff", "/system/reboot", "/mic/mute",
        "/system/restart/voice", "/system/restart/audio",
    ):
        status, body = _post(
            f"{base}{route}", {"muted": True},
            headers={"X-JTS-Household": secret},
        )
        assert status == 403, route
        assert body["error"] == "control_token_required", route


def test_unbonded_follower_accepts_and_adopts_grouping_fanout(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """BOOTSTRAP regression: an UNPAIRED follower (no secret) must accept the
    secret-distributing bond fan-out — proving the gate the secret installs
    doesn't deadlock the install — and ADOPT it so the next cross-device call
    verifies against it. The CSRF gate is armed; the leader sends only
    X-JTS-Household."""
    import jasper.control.household_credential as hc

    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret_path = tmp_path / "household_secret"
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret_path))  # follower UNPAIRED
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    assert hc.is_paired() is False
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Household": "leader-minted-secret"},
    )
    assert status == 200  # fail-safe accept → no bootstrap deadlock
    assert body["ok"] is True
    # ...and the follower ADOPTED the leader's secret (trust-on-first-use).
    assert hc.current() == "leader-minted-secret"
    # Now the gate requires it: a tokenless, householdless call 403s (the
    # bootstrap window has closed for this member).
    status, _ = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 403


def test_follower_with_deleted_secret_can_be_rebonded(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """RECOVERY regression: a follower whose household_secret was DELETED (the
    2026-05-23 ext4-loss class) must be re-bondable — fail-safe accept on absent,
    then re-adopt. Proves self-heal survives file loss (would be bricked by a
    fail-CLOSED gate)."""
    import jasper.control.household_credential as hc

    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret_path = tmp_path / "household_secret"
    secret_path.write_text("original-secret\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret_path))
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    secret_path.unlink()  # simulate filesystem loss of the secret
    assert hc.is_paired() is False
    status, body = _post(
        f"{base}/grouping/set",
        {"enabled": True, "role": "leader", "channel": "left", "bond_id": "x"},
        headers={"X-JTS-Household": "fresh-secret"},
    )
    assert status == 200
    assert body["ok"] is True
    assert hc.current() == "fresh-secret"  # re-adopted → re-bonded


def test_unpaired_grouping_set_is_open_bootstrap_window(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """The deliberate, documented trade: on an UNPAIRED speaker /grouping/set is
    fail-safe-OPEN (so the distributing fan-out is never rejected), while the
    OTHER gated routes — which never consult the household credential — stay
    closed under the armed control-token gate.

    Honest framing (control-plane-auth §6): this is NOT only a transient window.
    A never-bonded speaker is unpaired permanently (until its first bond), so its
    /grouping/set is open the whole time — genuinely weaker than the always-armed
    control_token for the unpaired case. Accepted as the trusted-LAN residual and
    unavoidable for TOFU bootstrap; pinned here so a change to that posture is
    deliberate, not silent."""
    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    _unpair_household(monkeypatch, tmp_path)
    env, _ = _grouping_test_setup(monkeypatch, tmp_path)
    # Unpaired /grouping/set: open even with no credential at all.
    status, _ = _post(f"{base}/grouping/set", {"enabled": False})
    assert status == 200
    # The other gated routes are NOT loosened by the household fail-safe.
    status, body = _post(f"{base}/mic/mute", {"muted": True})
    assert status == 403
    assert body["error"] == "control_token_required"


def test_unbond_clears_household_secret(
    monkeypatch, tmp_path, server_with_coordinator,
):
    """An unbond (/grouping/set enabled=false) carrying the matching secret
    clears it, so the speaker can later re-pair to a different household."""
    import jasper.control.household_credential as hc

    base, _ = server_with_coordinator
    _enable_control_token(monkeypatch, tmp_path)
    secret = _pair_household(monkeypatch, tmp_path, secret="to-be-cleared")
    _grouping_test_setup(monkeypatch, tmp_path)
    assert hc.is_paired() is True
    status, body = _post(
        f"{base}/grouping/set", {"enabled": False},
        headers={"X-JTS-Household": secret},
    )
    assert status == 200
    assert hc.is_paired() is False  # cleared → re-pairable
