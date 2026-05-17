"""Route-level tests for jasper.control.server.

Spins the ThreadingHTTPServer on a random port. The volume routes go
through `_with_coordinator` — we monkey-patch that helper to bypass
the real CamillaController/RendererClient stack and feed in a fake
coordinator that records calls.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from jasper.control.server import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    _clamp_db,
    _db_to_percent,
    _delta_db_to_delta_percent,
    _make_handler,
)


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


@pytest.fixture
def server_with_coordinator(monkeypatch):
    """Start a ThreadingHTTPServer and patch _with_coordinator to use
    the fake. Yields (base_url, fake_coord)."""
    fake = FakeCoordinator(level=60)

    async def fake_with_coordinator(op, **kwargs):  # noqa: ARG001
        return await op(fake)

    import jasper.control.server as srv_mod
    monkeypatch.setattr(srv_mod, "_with_coordinator", fake_with_coordinator)

    handler = _make_handler("127.0.0.1", 1234, "/nonexistent.sock")
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


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read() if e.fp else b"")


def _post(url: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST",
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
    POST unmutes (restores 60%). Used by the VK-01 knob click."""
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


def test_state_returns_snapshot_with_fail_soft_sections(
    server_with_coordinator, monkeypatch, tmp_path,
):
    """GET /state aggregates across daemons. In a unit test no daemon
    is reachable (no camilla, no shairport, no voice UDS), so each
    section comes back as null/None — but the response is still 200
    with a stable top-level shape."""
    base, _ = server_with_coordinator
    state_path = tmp_path / "speaker_volume.json"
    state_path.write_text('{"listening_level": 73}')
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(state_path))
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")
    monkeypatch.setenv("JASPER_OPENAI_MODEL", "gpt-realtime-2")
    # Point librespot state at a missing file → empty dict.
    monkeypatch.setenv(
        "JASPER_LIBRESPOT_STATE", str(tmp_path / "missing.json"),
    )

    status, body = _get(f"{base}/state")
    assert status == 200
    assert "ts" in body
    assert body["voice"]["provider"] == "openai"
    assert body["voice"]["model"] == "gpt-realtime-2"
    assert body["voice"]["reachable"] is False
    assert body["voice"]["session_active"] is False
    assert body["audio"]["listening_level_percent"] == 73
    # Camilla isn't reachable from the test → main_volume_db None.
    assert body["audio"]["main_volume_db"] is None
    assert body["renderers"]["spotify"]["playing"] is False
    assert body["active_source"] in {"idle", "airplay"}
    assert body["satellites"]["dial"]["online"] is False


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
