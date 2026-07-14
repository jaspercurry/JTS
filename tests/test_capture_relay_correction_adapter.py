# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the correction daemon adapter — the host-mediated graft that feeds a
relay-pulled WAV into the existing MeasurementSession analysis seam.

The adapter never plays audio or imports the correction daemon; it mints/registers
a relay capture, runs it with an injected stimulus callback, and writes the
verified WAV to the per-position path the host then feeds to
``on_capture_uploaded``. These tests exercise that with a faithful in-memory
relay, so the whole graft is proven hardware-free.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from types import SimpleNamespace
import urllib.parse

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jasper.capture_relay import correction_adapter as adapter
from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayResponse
from tests.active_speaker_fixtures import mono_output_topology

_CAPTURE_PAGE = {
    "schema_version": 1,
    "capture_protocol_version": 1,
    "supported_capture_protocol_versions": [1],
    "capture_page_build": "20260710.1",
}


def _topology():
    return mono_output_topology(topology_name="Bench mono")


def _level_pi_session():
    from dataclasses import replace

    from jasper.capture_relay.spec import build_level_ramp_spec

    return SimpleNamespace(
        session_id="sid",
        pull_token="pull",
        content_key=b"k" * 32,
        # Most adapter tests exercise host behavior with the legacy plain-event
        # transport. Dedicated tests below pin the authenticated v2 boundary.
        spec=replace(
            build_level_ramp_spec(run_token="test-run-token"),
            capture_protocol_version=1,
        ),
    )


class FakeRelayBackend:
    """Minimal in-memory relay mirroring the Worker's Pi-facing endpoints."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}

    def __call__(self, method, url, headers, body):
        path = urllib.parse.urlsplit(url).path
        parts = [p for p in path.split("/") if p]
        token = (headers.get("Authorization", "") or "").removeprefix("Bearer ")

        def jr(status, obj):
            return RelayResponse(status, {}, json.dumps(obj).encode())

        if parts == ["sessions"] and method == "POST":
            reg = json.loads(body)
            self.sessions[reg["session_id"]] = {
                "capture_spec": reg["capture_spec"],
                "pull_token": reg["pull_token"],
                "state": "pending",
                "event": None,
                "integrity": None,
                "blob": None,
            }
            return jr(201, {"session_id": reg["session_id"], "state": "pending"})
        if len(parts) >= 2 and parts[0] == "sessions":
            sid, sub = parts[1], (parts[2] if len(parts) > 2 else "")
            s = self.sessions.get(sid)
            if not s:
                return jr(404, {"error": "not_found"})
            if token != s["pull_token"]:
                return jr(401, {"error": "unauthorized"})
            if sub == "status" and method == "GET":
                return jr(200, {
                    "state": s["state"], "size": len(s["blob"] or b""),
                    "integrity": s["integrity"], "event": s["event"], "expires_at": 0,
                })
            if sub == "blob" and method == "GET":
                if s["state"] != "ready":
                    return jr(409, {"error": "not_ready"})
                return RelayResponse(200, {
                    "x-plaintext-length": str(s["integrity"]["plaintext_len"]),
                    "x-plaintext-sha256": s["integrity"]["sha256"],
                }, s["blob"])
            if sub == "" and method == "DELETE":
                del self.sessions[sid]
                return RelayResponse(204, {}, b"")
        return jr(404, {"error": "not_found"})

    def phone_arm(self, sid, device=None):
        event = {"armed": True, "capture_page": dict(_CAPTURE_PAGE)}
        if device is not None:
            event["device"] = device
        self.sessions[sid]["event"] = event

    def phone_upload(self, sid, content_key, wav):
        iv = os.urandom(crypto.IV_BYTES)
        s = self.sessions[sid]
        s["blob"] = iv + AESGCM(content_key).encrypt(iv, wav, None)
        s["integrity"] = {"plaintext_len": len(wav), "sha256": hashlib.sha256(wav).hexdigest()}
        s["state"] = "ready"


# --- config gate --------------------------------------------------------------


def test_relay_enabled_gates_on_relay_base(monkeypatch):
    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    assert adapter.relay_enabled() is False
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "")
    assert adapter.relay_enabled() is False
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "disabled")
    assert adapter.relay_enabled() is False
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    assert adapter.relay_enabled() is True


def test_capture_origin_default_and_override(monkeypatch):
    monkeypatch.delenv("JASPER_CAPTURE_ORIGIN", raising=False)
    assert adapter.capture_origin_from_env() == "capture.jasper.tech"
    # Bare host is kept; a pasted scheme is stripped (tap_link prepends https://,
    # so https://cap.example must not become https://https://cap.example).
    monkeypatch.setenv("JASPER_CAPTURE_ORIGIN", "cap.example/")
    assert adapter.capture_origin_from_env() == "cap.example"
    monkeypatch.setenv("JASPER_CAPTURE_ORIGIN", "https://cap.example/")
    assert adapter.capture_origin_from_env() == "cap.example"
    monkeypatch.setenv("JASPER_CAPTURE_ORIGIN", "http://cap.example")
    assert adapter.capture_origin_from_env() == "cap.example"


# --- open + run + store seam --------------------------------------------------


def test_open_room_sweep_capture_registers_and_links():
    backend = FakeRelayBackend()
    client = RelayClient("https://relay.test", transport=backend)
    rc = adapter.open_room_sweep_capture(
        client,
        position=2,
        total_positions=5,
        relay_base="https://relay.test",
        capture_origin="capture.test",
        return_url="http://jts5.local/correction/",
    )
    # Registered with the relay, opaque room_sweep spec stored.
    stored = backend.sessions[rc.pi_session.session_id]
    capture_spec = json.loads(stored["capture_spec"])
    assert capture_spec["kind"] == "room_sweep"
    assert capture_spec["return_url"] == "http://jts5.local/correction/"
    # Tap-link carries the handle in the fragment; copy names the position.
    assert rc.tap_link.startswith("https://capture.test/#")
    assert "position 2 of 5" in capture_spec["ui"]["screen"][0]["text"]


def test_followup_room_sweep_is_capture_only_after_session_setup():
    backend = FakeRelayBackend()
    client = RelayClient("https://relay.test", transport=backend)
    rc = adapter.open_room_sweep_capture(
        client,
        position=2,
        total_positions=3,
        relay_base="https://relay.test",
        capture_origin="capture.test",
        guided_setup=False,
    )

    stored = backend.sessions[rc.pi_session.session_id]
    capture_spec = json.loads(stored["capture_spec"])
    assert capture_spec["setup_validation"] is False
    assert capture_spec["calibration_models"] == []


def test_followup_room_sweep_carries_the_frozen_setup_binding():
    backend = FakeRelayBackend()
    client = RelayClient("https://relay.test", transport=backend)
    rc = adapter.open_room_sweep_capture(
        client,
        position=3,
        total_positions=3,
        relay_base="https://relay.test",
        capture_origin="capture.test",
        guided_setup=False,
        setup_binding_id="room-session-12345",
    )

    capture_spec = json.loads(
        backend.sessions[rc.pi_session.session_id]["capture_spec"]
    )
    assert capture_spec["setup_binding_id"] == "room-session-12345"


def test_pi_setup_binding_accepts_only_the_validated_compact_identity():
    from jasper.web import correction_setup

    owner = SimpleNamespace()
    setup = {
        "total_positions": 3,
        "calibration": {"mode": "none"},
    }
    binding_id = "room-session-12345"
    digest = correction_setup._setup_digest(setup)
    identity = {"schema": 1, "binding_id": binding_id, "sha256": digest}

    correction_setup._bind_relay_setup(
        owner,
        setup,
        identity,
        expected_binding_id=binding_id,
    )
    correction_setup._assert_relay_setup_binding(
        owner,
        {"binding": identity},
        expected_binding_id=binding_id,
    )

    changed = {**identity, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="setup changed"):
        correction_setup._assert_relay_setup_binding(
            owner,
            {"binding": changed},
            expected_binding_id=binding_id,
        )


def test_run_and_store_feeds_the_verified_wav(tmp_path):
    backend = FakeRelayBackend()
    client = RelayClient("https://relay.test", transport=backend)
    rc = adapter.open_room_sweep_capture(
        client, position=1, total_positions=1,
        relay_base="https://relay.test", capture_origin="capture.test",
    )
    wav = b"RIFF" + bytes(range(200)) * 4
    device = {"label": "iPhone Microphone"}
    armed_calls = []
    # The phone arms (it is recording) before the Pi's first poll, reporting which
    # mic it used.
    backend.phone_arm(rc.pi_session.session_id, device=device)

    def on_armed():
        # The host plays the stimulus; the phone finishes its window and uploads.
        armed_calls.append(True)
        backend.phone_upload(rc.pi_session.session_id, rc.pi_session.content_key, wav)

    out = tmp_path / "p0.wav"
    result = adapter.run_and_store(
        client, rc.pi_session, out,
        on_armed=on_armed, poll_interval_s=0.0, timeout_s=5.0, sleep=lambda _s: None,
    )
    # WAV written verbatim to the per-position path the host feeds to analysis; the
    # CaptureResult also carries the phone-reported device for the cal gate.
    assert out.read_bytes() == wav
    assert result.wav == wav
    assert result.device == device
    assert armed_calls == [True]  # stimulus fired exactly once
    # Relay session purged after the verified pull.
    assert rc.pi_session.session_id not in backend.sessions


# --- daemon endpoint: the safety-relevant gate + guard ------------------------
# The background sweep playback + real measurement are on-device; only the
# explicitly-unconfigured gate, the state guard, and the /status holder are
# tested here.


def test_endpoint_is_inert_when_relay_not_configured(monkeypatch):
    import pytest

    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    from jasper.web import correction_setup

    # An explicitly unconfigured relay raises before touching the session or any
    # network, so the standard on-Pi flow is unaffected.
    with pytest.raises(ValueError, match="not configured"):
        correction_setup._handle_relay_capture(None)


def test_endpoint_state_guard_rejects_wrong_state(monkeypatch):
    import types

    import pytest

    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    from jasper.correction.session import SessionState
    from jasper.web import correction_setup

    # A fresh session is IDLE, not the pre-sweep needs_noise_capture the relay
    # capture owns — reject before any network call.
    fake = types.SimpleNamespace(state=SessionState.IDLE, session_id="cap_x")
    monkeypatch.setattr(correction_setup, "_get_or_create_session", lambda: fake)
    with pytest.raises(ValueError, match="measurement position or trust repeat"):
        correction_setup._handle_relay_capture(None)


def test_relay_device_calibration_block():
    # Device-aware, POST-capture: the phone may use its built-in mic OR a USB-C
    # measurement mic plugged into it, so the decision keys off the reported device.
    import types

    from jasper.web import correction_setup

    block = correction_setup._relay_device_calibration_block
    vendor = types.SimpleNamespace(
        provider="dayton_audio", model="iMM-6", label="Dayton Audio iMM-6"
    )
    # No calibration loaded → always allow (nothing to mis-apply).
    assert block(None, None) is None
    assert block(None, {"label": "iPhone Microphone"}) is None
    # Calibration loaded but the phone reported no device → refuse (can't verify).
    msg = block(vendor, None)
    assert msg is not None and "didn't report" in msg
    # Calibration loaded + the phone's built-in mic → refuse (would mis-correct).
    assert block(vendor, {"label": "iPhone Microphone"}) is not None
    # Calibration loaded + the matching USB measurement mic → allow; the curve is
    # applied Pi-side during analysis.
    assert block(vendor, {"label": "UMIK-1"}) is None


def test_open_capture_is_kind_agnostic():
    # The generic open_capture mints+registers ANY spec, so a new kind needs no
    # per-kind adapter function — here the sync_marker spec, the second caller.
    from jasper.capture_relay.spec import build_sync_marker_spec

    backend = FakeRelayBackend()
    client = RelayClient("https://relay.test", transport=backend)
    rc = adapter.open_capture(
        client,
        build_sync_marker_spec(),
        relay_base="https://relay.test",
        capture_origin="capture.test",
        return_url="http://jts5.local/correction/sync",
    )
    stored = backend.sessions[rc.pi_session.session_id]
    capture_spec = json.loads(stored["capture_spec"])
    assert capture_spec["kind"] == "sync_marker"
    assert capture_spec["return_url"] == "http://jts5.local/correction/sync"
    assert rc.tap_link.startswith("https://capture.test/#")


def test_sync_relay_endpoint_gate_and_precheck(monkeypatch):
    import pytest

    from jasper.web import correction_setup, sync_flow

    # Inert when the relay isn't configured (default flow byte-identical).
    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        correction_setup._handle_sync_relay_capture(None)

    # Configured but no active sync session → the flow's own precheck refuses
    # before any network call or slot claim.
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    sync_flow._state["phase"] = "idle"
    correction_setup._set_relay_capture(None)
    with pytest.raises(ValueError, match="no active sync session"):
        correction_setup._handle_sync_relay_capture(None)
    assert correction_setup._get_relay_capture() is None  # slot not claimed


def test_sync_relay_endpoint_binds_originating_session_token(monkeypatch):
    import asyncio

    from jasper.web import correction_setup, sync_flow

    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    with sync_flow._lock:
        sync_flow._reset_locked()
        sync_flow._state["phase"] = "measuring"
        expected_token = int(sync_flow._state["session_token"])

    captured: dict[str, object] = {}

    async def run_and_consume(client, pi_session, *, session_token):
        captured.update(
            client=client,
            pi_session=pi_session,
            session_token=session_token,
        )

    def run_relay(kind, relay_base, *, return_url):
        asyncio.run(kind.run_and_consume("client", "pi-session"))
        captured.update(relay_base=relay_base, return_url=return_url)
        return {"status": "awaiting_phone", "tap_link": "https://capture.test/#x"}

    monkeypatch.setattr(sync_flow, "relay_run_and_consume", run_and_consume)
    monkeypatch.setattr(correction_setup, "_run_relay_capture", run_relay)

    try:
        payload = correction_setup._handle_sync_relay_capture(None)

        assert payload["relay"]["status"] == "awaiting_phone"
        assert captured["session_token"] == expected_token
        assert captured["client"] == "client"
        assert captured["pi_session"] == "pi-session"
    finally:
        sync_flow.handle_stop()


def test_endpoint_route_is_registered():
    # Pin route membership: deleting the allowlist line would 404 it silently.
    from jasper.web import correction_setup

    assert "/relay/capture" in correction_setup._POST_ROUTES
    assert "/sync/relay-capture" in correction_setup._POST_ROUTES


def test_status_holder_round_trips():
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    assert correction_setup._get_relay_capture() is None
    correction_setup._set_relay_capture({"tap_link": "https://capture.test/#x", "status": "awaiting_phone"})
    assert correction_setup._get_relay_capture()["status"] == "awaiting_phone"
    correction_setup._set_relay_capture(None)  # reset


def test_relay_status_is_visible_only_to_its_own_flow():
    from jasper.web import correction_setup

    correction_setup._set_relay_capture({
        "status": "awaiting_phone",
        "kind": "room_sweep",
        "tap_link": "room-link",
    })
    assert correction_setup._get_relay_capture_for("room_") is not None
    assert correction_setup._get_relay_capture_for("crossover_sweep:") is None
    correction_setup._set_relay_capture(None)


def test_relay_level_identity_binds_mic_and_calibration():
    from jasper.web import correction_setup

    sess = SimpleNamespace(
        input_device={"label": "USB measurement mic"},
        mic_calibration=SimpleNamespace(calibration_id="cal-1"),
    )
    identity = correction_setup._relay_level_identity(sess)
    correction_setup._assert_relay_level_identity(
        sess, identity, device={"label": "USB measurement mic"}
    )
    with pytest.raises(ValueError, match="microphone changed"):
        correction_setup._assert_relay_level_identity(
            sess, identity, device={"label": "Phone mic"}
        )
    sess.mic_calibration = SimpleNamespace(calibration_id="cal-2")
    with pytest.raises(ValueError, match="calibration changed"):
        correction_setup._assert_relay_level_identity(sess, identity)


def test_relay_capture_reentrancy_guard():
    # Atomic claim: one in-flight capture blocks a second.
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture("room_sweep") is True
    assert correction_setup._get_relay_capture() == {
        "status": "starting",
        "kind": "room_sweep",
    }
    assert correction_setup._begin_relay_capture("room_repeat") is False
    correction_setup._set_relay_capture({"tap_link": "x", "status": "awaiting_phone"})
    assert correction_setup._begin_relay_capture("room_repeat") is False
    # A finished (complete/failed) holder does not block a new capture.
    correction_setup._set_relay_capture({"tap_link": "x", "status": "complete"})
    assert correction_setup._begin_relay_capture("room_repeat") is True
    correction_setup._set_relay_capture(None)  # reset


def test_room_relay_gain_is_restored_before_measurement_window_exits(monkeypatch):
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    order = []

    @asynccontextmanager
    async def window():
        order.append("window_enter")
        try:
            yield
        finally:
            order.append("window_exit")

    async def play_sweep(*_args, **_kwargs):
        order.append("play")

    class Session:
        current_position = 0
        total_positions = 1

        async def ensure_level_match_volume(self, _setter):
            order.append("ensure")
            return True

        async def prepare_and_play_sweep(self, player, *, runtime_probe_async):
            await player()

        async def restore_level_match_volume(self, _setter):
            order.append("restore")
            return True

    class Cam:
        async def get_runtime_status(self, *, best_effort):
            return {"state": "Running"}

        async def set_volume_db(self, _db, *, best_effort):
            return True

    class Client:
        def post_host_event(self, _sid, _token, payload):
            order.append(payload["phase"])

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(playback, "play_sweep", play_sweep)

    correction_setup._run_relay_measurement_sweep(
        Session(),
        Cam(),
        client=Client(),
        pi_session=SimpleNamespace(session_id="sid", pull_token="pull"),
    )

    assert order == [
        "window_enter",
        "ensure",
        "sweep_started",
        "play",
        "sweep_complete",
        "restore",
        "window_exit",
    ]


def test_relay_repeat_sweep_uses_repeat_state_machine_and_progress(monkeypatch):
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    calls = []

    @asynccontextmanager
    async def window():
        yield

    async def play_sweep(_path):
        calls.append("play")

    class Session:
        current_position = 1
        total_positions = 6

        async def ensure_level_match_volume(self, _setter):
            return True

        async def prepare_and_play_repeat_sweep(
            self,
            player,
            *,
            runtime_probe_async,
        ):
            calls.append("repeat")
            await player("sweep.wav")

        async def restore_level_match_volume(self, _setter):
            return True

    class Cam:
        async def get_runtime_status(self, *, best_effort):
            return {"state": "Running"}

        async def set_volume_db(self, _db, *, best_effort):
            return True

    class Client:
        def post_host_event(self, _sid, _token, payload):
            calls.append(
                (
                    payload["phase"],
                    payload["position"],
                    payload["total_positions"],
                    payload["capture_kind"],
                )
            )

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(playback, "play_sweep", play_sweep)

    correction_setup._run_relay_measurement_sweep(
        Session(),
        Cam(),
        client=Client(),
        pi_session=SimpleNamespace(session_id="sid", pull_token="pull"),
        repeat=True,
    )

    assert calls == [
        ("sweep_started", 1, 6, "repeat"),
        "repeat",
        "play",
        ("sweep_complete", 1, 6, "repeat"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("post_ramp_mismatch", "commit_allowed"),
    ((False, True), (True, True), (False, False)),
)
async def test_relay_level_adapter_samples_ambient_before_strict_volume_write(
    monkeypatch, post_ramp_mismatch, commit_allowed,
):
    """The transport adapter binds setup/noise before asking the kernel to ramp."""
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    setup_binding_id = "room-session-12345"
    setup = {"total_positions": 3, "calibration": {"mode": "none"}}
    identity = {
        "schema": 1,
        "binding_id": setup_binding_id,
        "sha256": correction_setup._setup_digest(setup),
    }
    setup_status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "setup_validate": True,
            "setup_token": "setup-1",
            "setup": setup,
            "setup_identity": identity,
        },
    }
    batch_status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "level_batch": {
                "schema": 1,
                "run_token": "run-1",
                "armed": True,
                "samples": [
                    {"seq": 1, "rms_dbfs": -54.0, "peak_dbfs": -49.0},
                    {"seq": 2, "rms_dbfs": -50.0, "peak_dbfs": -46.0},
                    {"seq": 3, "rms_dbfs": -52.0, "peak_dbfs": -47.0},
                    *[
                        {
                            "seq": seq,
                            "rms_dbfs": -52.0,
                            "peak_dbfs": -47.0,
                        }
                        for seq in range(4, 11)
                    ],
                ],
                "context": {
                    "setup": {"binding": identity},
                    "device": {"label": "USB measurement mic"},
                },
            },
        },
    }
    current_status = {"value": setup_status}
    setup_acks = []
    terminal_events = []
    ambient_index = 0
    ambient_reads: dict[int, int] = {}

    class Client:
        def status(self, *_args):
            nonlocal ambient_index
            value = current_status["value"]
            if value == "ambient":
                sample = batch_status["event"]["level_batch"]["samples"][
                    ambient_index
                ]
                seq = int(sample["seq"])
                ambient_reads[seq] = ambient_reads.get(seq, 0) + 1
                # Repeat every singleton event once. A broken accumulator that
                # counts polls instead of unique client samples would unlock on
                # seq 5 and derive the wrong floor.
                if ambient_reads[seq] >= 2 and ambient_index < 9:
                    ambient_index += 1
                return {
                    "event": {
                        **batch_status["event"],
                        "level_batch": {
                            **batch_status["event"]["level_batch"],
                            "samples": [sample],
                        },
                    }
                }
            return value

        def post_host_event(self, *_args):
            payload = _args[-1]
            if payload.get("phase") == "setup_validated":
                setup_acks.append(payload)
                current_status["value"] = "ambient"
            if "ramp" in payload:
                terminal_events.append(payload["ramp"])
            return None

    writes = []

    class Cam:
        async def get_volume_db(self, *, best_effort):
            assert best_effort is False
            return -32.0

        async def set_volume_db(self, db, *, best_effort):
            writes.append((db, best_effort))
            return True

    class Tone:
        async def play(self):
            return None

        def cancel(self):
            return None

    @asynccontextmanager
    async def window():
        yield

    monkeypatch.setattr(correction_setup, "_camilla", lambda: Cam())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    tone_kwargs = {}

    def ensure_tone_wav(**kwargs):
        tone_kwargs.update(kwargs)
        return "tone.wav"

    monkeypatch.setattr(playback, "_ensure_tone_wav", ensure_tone_wav)
    monkeypatch.setattr(playback, "TonePlayer", lambda _path: Tone())
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    class Session:
        # A prior driver's floor must not carry into this geometry.
        noise_floor_db = -20.0
        mic_calibration = None
        input_device = None
        total_positions = 1
        current_position = 0

        async def run_level_match(self, _geometry, **ports):
            assert "context_id" not in ports
            assert ports["noise_floor_dbfs"] == -52.0
            await ports["set_main_volume_db"](-41.0)
            if post_ramp_mismatch:
                self.mic_calibration = object()
                self.input_device = None
            return SimpleNamespace(locked=True, ramp=SimpleNamespace(error=None))

    sess = Session()
    operation = correction_setup._run_relay_level_match(
        sess,
        Client(),
        _level_pi_session(),
        geometry="listening_position",
        run_token="run-1",
        setup_binding_id=setup_binding_id,
        tone_frequency_hz=875.0,
        reuse_noise_floor=False,
        begin_commit=lambda: commit_allowed,
    )
    if post_ramp_mismatch:
        with pytest.raises(ValueError, match="calibration is loaded"):
            await operation
        assert terminal_events[-1]["state"] == "error"
    elif not commit_allowed:
        with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
            await operation
        assert terminal_events[-1]["state"] == "cancelled"
    else:
        await operation

    assert setup_acks == [{"phase": "setup_validated", "setup_token": "setup-1"}]
    assert set(ambient_reads) == set(range(1, 11))
    assert all(ambient_reads[seq] >= 2 for seq in range(1, 10))
    assert ambient_reads[10] >= 1
    assert sess.total_positions == 1
    assert sess.noise_floor_db == -52.0
    if post_ramp_mismatch:
        assert sess.input_device is None
    else:
        assert sess.input_device["label"] == "USB measurement mic"
    assert writes == [(-41.0, False)]
    from jasper.audio_measurement.excitation import (
        AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
    )

    assert tone_kwargs["dbfs"] == AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS
    assert tone_kwargs["freq_hz"] == 875.0


@pytest.mark.asyncio
async def test_relay_level_mismatched_context_cannot_poison_ambient_floor(
    monkeypatch,
):
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator
    from jasper.web import correction_setup

    expected_id = "room-session-12345"
    good_digest = "a" * 64
    bad_claim = {
        "schema": 1,
        "binding_id": expected_id,
        "sha256": "b" * 64,
    }
    status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "level_batch": {
                "schema": 1,
                "run_token": "run-poison",
                "armed": True,
                "samples": [
                    {
                        "seq": seq,
                        "t_client_ms": seq * 200,
                        "rms_dbfs": -42.0,
                        "peak_dbfs": -36.0,
                    }
                    for seq in range(1, 11)
                ],
                "context": {
                    "setup": {"binding": bad_claim},
                    "device": {"label": "USB measurement mic"},
                },
            },
        },
    }

    class Client:
        def status(self, *_args):
            return status

        def post_host_event(self, *_args):
            return None

    @asynccontextmanager
    async def window():
        yield

    class Session:
        session_id = "room-1"
        noise_floor_db = None
        mic_calibration = None
        input_device = None
        relay_setup_binding = correction_setup._RelaySetupBinding(
            binding_id=expected_id,
            sha256=good_digest,
        )

    sess = Session()
    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    with pytest.raises(ValueError, match="microphone setup changed"):
        await correction_setup._run_relay_level_match(
            sess,
            Client(),
            _level_pi_session(),
            geometry="listening_position",
            run_token="run-poison",
            setup_binding_id=expected_id,
        )

    assert sess.noise_floor_db is None


@pytest.mark.asyncio
async def test_relay_level_stop_during_prepare_never_starts_ramp(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    import threading

    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    stop_event = threading.Event()
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    ramp_calls = []
    restores = []
    status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "level_batch": {
                "schema": 1,
                "run_token": "run-stop-prepare",
                "armed": True,
                "samples": [
                    {
                        "seq": 1,
                        "t_client_ms": 200,
                        "rms_dbfs": -50.0,
                        "peak_dbfs": -45.0,
                    }
                ],
            },
        }
    }

    class Client:
        def status(self, *_args):
            return status

        def post_host_event(self, *_args):
            return None

    class Session:
        session_id = "active-crossover"
        noise_floor_db = -55.0
        mic_calibration = None
        input_device = None

        async def cancel_level_match(self):
            return False

        async def run_level_match(self, *_args, **_kwargs):
            ramp_calls.append(True)
            return SimpleNamespace(locked=True, ramp=SimpleNamespace(error=None))

    @asynccontextmanager
    async def window():
        yield

    async def prepare():
        prepare_started.set()
        await release_prepare.wait()
        return object()

    async def restore(_prepared):
        restores.append(True)

    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(playback, "_ensure_tone_wav", lambda **_kwargs: "tone.wav")
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    task = asyncio.create_task(
        correction_setup._run_relay_level_match(
            Session(),
            Client(),
            _level_pi_session(),
            geometry="near_field_driver:mono:woofer",
            run_token="run-stop-prepare",
            prepare_tone=prepare,
            restore_tone=restore,
            stop_requested=stop_event.is_set,
        )
    )
    assert await asyncio.wait_for(prepare_started.wait(), timeout=2)
    stop_event.set()
    await asyncio.sleep(0.3)
    release_prepare.set()

    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await task
    assert ramp_calls == []
    assert restores == [True]


@pytest.mark.asyncio
async def test_relay_driver_level_rejects_changed_microphone_before_tone(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator
    from jasper.web import correction_setup

    binding_id = "protected-profile-12345"
    digest = "a" * 64
    status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "level_batch": {
                "schema": 1,
                "run_token": "run-changed-mic",
                "armed": True,
                "samples": [
                    {"seq": 1, "rms_dbfs": -50.0, "peak_dbfs": -45.0}
                ],
                "context": {
                    "setup": {
                        "binding": {
                            "schema": 1,
                            "binding_id": binding_id,
                            "sha256": digest,
                        }
                    },
                    "device": {"label": "Different USB mic"},
                },
            },
        }
    }

    class Client:
        def status(self, *_args):
            return status

        def post_host_event(self, *_args):
            return None

    @asynccontextmanager
    async def window():
        yield

    class Session:
        session_id = "active-crossover"
        noise_floor_db = None
        mic_calibration = None
        input_device = {"label": "Original USB mic"}
        relay_setup_binding = correction_setup._RelaySetupBinding(
            binding_id=binding_id,
            sha256=digest,
        )

    sess = Session()
    expected = correction_setup._relay_level_identity(sess)
    prepared = []

    async def prepare_tone():
        prepared.append(True)

    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    with pytest.raises(ValueError, match="microphone changed"):
        await correction_setup._run_relay_level_match(
            sess,
            Client(),
            _level_pi_session(),
            geometry="near_field_driver:mono:tweeter",
            run_token="run-changed-mic",
            setup_binding_id=binding_id,
            expected_level_identity=expected,
            prepare_tone=prepare_tone,
        )

    assert sess.input_device == {"label": "Original USB mic"}
    assert prepared == []


@pytest.mark.asyncio
async def test_relay_level_adapter_fails_closed_when_volume_write_is_rejected(
    monkeypatch,
):
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "level_batch": {
                "schema": 1,
                "run_token": "run-2",
                "armed": True,
                "samples": [
                    {"seq": 1, "rms_dbfs": -55.0, "peak_dbfs": -50.0}
                ],
                "context": {"setup": {}, "device": {"label": "Phone mic"}},
            },
        },
    }

    class Client:
        def status(self, *_args):
            return status

        def post_host_event(self, *_args):
            return None

    class Cam:
        async def set_volume_db(self, _db, *, best_effort):
            assert best_effort is False
            return False

    class Tone:
        async def play(self):
            return None

        def cancel(self):
            return None

    @asynccontextmanager
    async def window():
        yield

    monkeypatch.setattr(correction_setup, "_camilla", lambda: Cam())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(playback, "_ensure_tone_wav", lambda **_kwargs: "tone.wav")
    monkeypatch.setattr(playback, "TonePlayer", lambda _path: Tone())
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    class Session:
        # This test targets the strict Camilla write boundary; ambient-window
        # admission is covered by the preceding adapter test.
        noise_floor_db = -55.0
        mic_calibration = None
        input_device = None

        async def run_level_match(self, _geometry, **ports):
            await ports["set_main_volume_db"](-45.0)
            raise AssertionError("rejected write must raise first")

    with pytest.raises(RuntimeError, match="rejected the measurement volume"):
        await correction_setup._run_relay_level_match(
            Session(),
            Client(),
            _level_pi_session(),
            geometry="listening_position",
            run_token="run-2",
        )


@pytest.mark.asyncio
async def test_relay_level_agc_refusal_never_starts_tone_and_reaches_phone(
    monkeypatch,
):
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    status = {
        "event": {
            "capture_page": dict(_CAPTURE_PAGE),
            "level_refused": {
                "schema": 1,
                "run_token": "run-agc",
                "reason": "agc_not_proven_off",
            }
        }
    }
    host_events = []

    class Client:
        def status(self, *_args):
            return status

        def post_host_event(self, _sid, _token, payload):
            host_events.append(payload)

    @asynccontextmanager
    async def window():
        yield

    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(
        playback,
        "_ensure_tone_wav",
        lambda **_kwargs: pytest.fail("AGC refusal must happen before tone creation"),
    )
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    with pytest.raises(RuntimeError, match="cannot prove automatic microphone gain"):
        await correction_setup._run_relay_level_match(
            SimpleNamespace(noise_floor_db=None),
            Client(),
            _level_pi_session(),
            geometry="listening_position",
            run_token="run-agc",
        )

    terminal = host_events[-1]["ramp"]
    assert terminal["state"] == "error"
    assert terminal["terminal"] is True
    assert terminal["run_token"] == "run-agc"


@pytest.mark.asyncio
async def test_relay_level_stale_page_never_starts_tone_and_reaches_phone(
    monkeypatch,
):
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator, playback
    from jasper.web import correction_setup

    status = {
        "event": {
            "level_batch": {
                "schema": 1,
                "run_token": "run-stale",
                "armed": True,
                "samples": [{"seq": 1, "rms_dbfs": -50.0, "peak_dbfs": -45.0}],
            },
        },
    }
    host_events = []

    class Client:
        def status(self, *_args):
            return status

        def post_host_event(self, _sid, _token, payload):
            host_events.append(payload)

    @asynccontextmanager
    async def window():
        yield

    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(
        playback,
        "_ensure_tone_wav",
        lambda **_kwargs: pytest.fail("an incompatible page must fail before tone"),
    )
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    with pytest.raises(relay_session.CapturePageIncompatible):
        await correction_setup._run_relay_level_match(
            SimpleNamespace(noise_floor_db=None),
            Client(),
            _level_pi_session(),
            geometry="listening_position",
            run_token="run-stale",
        )

    terminal = host_events[-1]["ramp"]
    assert terminal["state"] == "error"
    assert "incompatible" in terminal["error"]


@pytest.mark.asyncio
async def test_relay_level_v2_verifies_and_unwraps_authenticated_events(monkeypatch):
    from jasper.capture_relay.integrity import authenticated_phone_event
    from jasper.capture_relay.spec import build_level_ramp_spec
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator
    from jasper.web import correction_setup

    pi_session = SimpleNamespace(
        session_id="sid-v2",
        pull_token="pull",
        content_key=b"v" * 32,
        spec=build_level_ramp_spec(run_token="run-v2"),
    )
    event = {
        "capture_page": {
            "schema_version": 1,
            "capture_protocol_version": 2,
            "supported_capture_protocol_versions": [1, 2],
            "capture_page_build": "20260711.3",
        },
        "level_refused": {
            "schema": 1,
            "run_token": "run-v2",
            "reason": "agc_not_proven_off",
        },
    }

    class Client:
        def status(self, *_args):
            return {
                "event": authenticated_phone_event(
                    pi_session.content_key,
                    pi_session.session_id,
                    event,
                    sequence=1,
                )
            }

        def post_host_event(self, *_args):
            return None

    @asynccontextmanager
    async def window():
        yield

    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    with pytest.raises(RuntimeError, match="cannot prove automatic microphone gain"):
        await correction_setup._run_relay_level_match(
            SimpleNamespace(noise_floor_db=None),
            Client(),
            pi_session,
            geometry="listening_position",
            run_token="run-v2",
        )


@pytest.mark.asyncio
async def test_relay_level_v2_refuses_unsigned_event_before_tone(monkeypatch):
    from jasper.capture_relay.spec import build_level_ramp_spec
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator
    from jasper.web import correction_setup

    pi_session = SimpleNamespace(
        session_id="sid-v2-unsigned",
        pull_token="pull",
        content_key=b"u" * 32,
        spec=build_level_ramp_spec(run_token="run-v2-unsigned"),
    )

    class Client:
        def status(self, *_args):
            return {"event": {"level_batch": {"run_token": "run-v2-unsigned"}}}

        def post_host_event(self, *_args):
            return None

    @asynccontextmanager
    async def window():
        yield

    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    with pytest.raises(relay_session.CaptureFailed, match="integrity"):
        await correction_setup._run_relay_level_match(
            SimpleNamespace(noise_floor_db=None),
            Client(),
            pi_session,
            geometry="listening_position",
            run_token="run-v2-unsigned",
        )


def _legacy_crossover_level_status(*, preservation_ready: bool) -> dict:
    return {
        "active": True,
        "setup": {
            "status": "ready",
            "baseline_profile": {"candidate_fingerprint": "candidate-1"},
            "protected_profile": {
                "source_fingerprint": "source-1",
                "candidate_fingerprint": "candidate-1",
            },
            "applied_crossover": {
                "valid": False,
                "reason": "active_applied_profile_snapshot_missing",
                "detail": "the applied manual crossover has no snapshot",
            },
            "manual_preservation": {
                "ready": preservation_ready,
                "reason": (
                    None
                    if preservation_ready
                    else "manual_crossover_source_changed"
                ),
                "detail": (
                    "The currently applied manual crossover can be preserved exactly."
                    if preservation_ready
                    else "Saved crossover inputs changed; apply them again."
                ),
            },
        },
    }


def test_crossover_level_start_preserves_legacy_manual_then_registers_relay(
    monkeypatch,
    tmp_path,
):
    import asyncio

    from jasper.active_speaker import web_commissioning
    from jasper.active_speaker.measurement import (
        active_driver_targets,
        load_measurement_state,
        start_active_comparison_set,
    )
    import jasper.output_topology as output_topology
    from jasper.web import correction_crossover_backend as backend
    from jasper.web import correction_setup
    from tests.test_active_speaker_profile import _two_way_preset

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "measurements.json"),
    )
    topology = _topology()
    monkeypatch.setattr(output_topology, "load_output_topology", lambda: topology)
    driver_level_locks = {
        f"mono:{role}": {
            "target_id": f"mono:{role}",
            "speaker_group_id": "mono",
            "role": role,
            "tone_frequency_hz": frequency_hz,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": gain_db,
            "locked_main_volume_db": volume_db,
        }
        for role, frequency_hz, gain_db, volume_db in (
            ("woofer", 250.0, -3.0, -10.0),
            ("tweeter", 6250.0, -18.0, -4.0),
        )
    }
    prior_set = start_active_comparison_set(
        topology,
        profile_context_id="old-profile",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
    )

    legacy = _legacy_crossover_level_status(preservation_ready=True)
    current = {
        **legacy,
        "setup": {
            **legacy["setup"],
            "protected_profile": {
                "source_fingerprint": "c" * 64,
                "candidate_fingerprint": "c" * 64,
            },
            "applied_crossover": {
                "valid": True,
                "owner": "manual",
                "reason": None,
            },
        },
        "applied_profile": {
            "recomposition_snapshot": {"preset": _two_way_preset("mono")},
        },
            "targets": {
                "drivers": active_driver_targets(topology)
            },
    }
    statuses = iter((legacy, current))
    monkeypatch.setattr(backend, "status_payload", lambda: next(statuses))
    applied = {}

    async def apply_profile(
        *, tuning_owner, expected_candidate_fingerprint, camilla_factory
    ):
        applied["owner"] = tuning_owner
        applied["candidate_fingerprint"] = expected_candidate_fingerprint
        applied["camilla_factory"] = camilla_factory
        return {"status": "applied", "issues": []}

    monkeypatch.setattr(backend, "apply_profile", apply_profile)
    monkeypatch.setattr(
        web_commissioning,
        "automatic_driver_excitation",
        lambda _topology, role, **_kwargs: {
            "status": "ready",
            "commissioning_gain_db": -3.0 if role == "woofer" else -18.0,
        },
    )
    monkeypatch.setattr(
        backend,
        "level_lease",
        lambda: SimpleNamespace(
            level_match_snapshot=lambda **_kwargs: {"running": False}
        ),
    )
    monkeypatch.setattr(correction_setup, "_require_relay_base", lambda: "relay")
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(correction_setup, "_camilla", lambda: object())
    monkeypatch.setattr(
        correction_setup,
        "_run_async",
        lambda coro, *, timeout: asyncio.run(coro),
    )
    registered = {}

    def open_capture(_client, spec, **_kwargs):
        registered["spec"] = spec
        return object()

    monkeypatch.setattr(adapter, "open_capture", open_capture)

    def run_relay(kind, relay_base, *, return_url):
        registered.update(
            label=kind.label,
            relay_base=relay_base,
            return_url=return_url,
        )
        kind.open(object(), relay_base, "https://capture.jasper.tech", return_url)
        return {"url": "https://capture.jasper.tech/session"}

    monkeypatch.setattr(correction_setup, "_run_relay_capture", run_relay)
    monkeypatch.setattr(
        correction_setup,
        "_request_local_return_url",
        lambda *_args: "https://jts.local/correction/crossover/",
    )

    body = json.dumps({"capture_geometry": "near_field"}).encode()
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        rfile=io.BytesIO(body),
    )
    payload = correction_setup._handle_crossover_relay_level_match(handler)

    assert applied["owner"] == "manual"
    assert applied["candidate_fingerprint"] == "candidate-1"
    assert registered["label"] == "level_ramp:crossover"
    assert registered["relay_base"] == "relay"
    assert registered["return_url"] == "https://jts.local/correction/crossover/"
    spec = registered["spec"]
    assert spec.stimulus.label == "250 Hz level-match tone"
    assert spec.screen[1]["items"][0].startswith(
        "Move the microphone capsule to 3 cm"
    )
    assert "woofer cone" in spec.screen[1]["items"][0]
    assert load_measurement_state(topology)["active_comparison_set"] == prior_set
    assert payload["relay"]["url"].startswith("https://capture.jasper.tech/")


def test_crossover_level_start_refuses_unsafe_legacy_preservation(monkeypatch):
    from jasper.web import correction_crossover_backend as backend
    from jasper.web import correction_setup

    monkeypatch.setattr(
        backend,
        "status_payload",
        lambda: _legacy_crossover_level_status(preservation_ready=False),
    )
    monkeypatch.setattr(
        backend,
        "apply_profile",
        lambda **_kwargs: pytest.fail("unsafe preservation must not apply"),
    )
    monkeypatch.setattr(correction_setup, "_require_relay_base", lambda: "relay")
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(
        correction_setup,
        "_run_relay_capture",
        lambda *_args, **_kwargs: pytest.fail("must fail before relay registration"),
    )

    with pytest.raises(ValueError, match="Saved crossover inputs changed"):
        body = json.dumps({"capture_geometry": "near_field"}).encode()
        handler = SimpleNamespace(
            headers={"Content-Length": str(len(body))},
            rfile=io.BytesIO(body),
        )
        correction_setup._handle_crossover_relay_level_match(handler)


def test_open_commissioning_bundle_for_level_match_forwards_calibration_id(
    monkeypatch,
) -> None:
    """The comparison-set-start hook in _handle_crossover_relay_level_match's
    _run closure opens a bundle through this one-line seam before minting the
    comparison set — see STEP 1 CONTRACT §7.1. Unit-tested directly (rather
    than through the full relay flow, which no existing test drives to this
    point) because bundles.open_bundle is already exhaustively covered in
    tests/test_active_speaker_bundles.py; this only pins the wiring."""

    from jasper.active_speaker import bundles as active_speaker_bundles
    from jasper.web import correction_setup

    seen = {}

    def fake_open_bundle(topology, *, calibration_id):
        seen["topology"] = topology
        seen["calibration_id"] = calibration_id
        return {"session_id": "abc123def456", "bundle_dir": "/tmp/x/abc123def456"}

    monkeypatch.setattr(active_speaker_bundles, "open_bundle", fake_open_bundle)

    sentinel_topology = object()
    result = correction_setup._open_commissioning_bundle_for_level_match(
        sentinel_topology, calibration_id="cal-9"
    )

    assert result == {
        "session_id": "abc123def456",
        "bundle_dir": "/tmp/x/abc123def456",
    }
    assert seen == {"topology": sentinel_topology, "calibration_id": "cal-9"}


def test_open_commissioning_bundle_for_level_match_treats_none_id_as_empty(
    monkeypatch,
) -> None:
    from jasper.active_speaker import bundles as active_speaker_bundles
    from jasper.web import correction_setup

    seen = {}
    monkeypatch.setattr(
        active_speaker_bundles,
        "open_bundle",
        lambda topology, *, calibration_id: seen.setdefault(
            "calibration_id", calibration_id
        ),
    )

    correction_setup._open_commissioning_bundle_for_level_match(
        object(), calibration_id=""
    )

    assert seen["calibration_id"] == ""


def test_open_commissioning_bundle_for_level_match_is_fail_soft(
    monkeypatch,
) -> None:
    """bundles.open_bundle is already fail-soft (returns None on write
    failure); the wrapper must pass that None straight through rather than
    masking it, so the caller's own None-handling still applies."""

    from jasper.active_speaker import bundles as active_speaker_bundles
    from jasper.web import correction_setup

    monkeypatch.setattr(
        active_speaker_bundles, "open_bundle", lambda *_a, **_k: None
    )

    result = correction_setup._open_commissioning_bundle_for_level_match(
        object(), calibration_id="cal-9"
    )

    assert result is None
