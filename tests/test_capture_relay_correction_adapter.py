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

import hashlib
import json
import os
import urllib.parse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jasper.capture_relay import correction_adapter as adapter
from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayResponse


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
        event = {"armed": True}
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
    )
    # Registered with the relay, opaque room_sweep spec stored.
    stored = backend.sessions[rc.pi_session.session_id]
    assert json.loads(stored["capture_spec"])["kind"] == "room_sweep"
    # Tap-link carries the handle in the fragment; copy names the position.
    assert rc.tap_link.startswith("https://capture.test/#")
    assert "position 2 of 5" in json.loads(stored["capture_spec"])["ui"]["screen"][0]["text"]


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
    with pytest.raises(ValueError, match="needs_noise_capture"):
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
    )
    stored = backend.sessions[rc.pi_session.session_id]
    assert json.loads(stored["capture_spec"])["kind"] == "sync_marker"
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


def test_relay_capture_reentrancy_guard():
    # Atomic claim: one in-flight capture blocks a second.
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture() is True  # first claims it
    assert correction_setup._begin_relay_capture() is False  # second refused
    correction_setup._set_relay_capture({"tap_link": "x", "status": "awaiting_phone"})
    assert correction_setup._begin_relay_capture() is False  # still in flight
    # A finished (complete/failed) holder does not block a new capture.
    correction_setup._set_relay_capture({"tap_link": "x", "status": "complete"})
    assert correction_setup._begin_relay_capture() is True
    correction_setup._set_relay_capture(None)  # reset
