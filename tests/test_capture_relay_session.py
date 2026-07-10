# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pi-side orchestration tests (phone-mic relay step 4).

Drives the full Pi round-trip — mint → register → poll → armed→stimulus → pull →
decrypt → verify → return WAV — against a faithful in-memory relay backend that
mirrors the Worker's Pi-facing behaviour (opaque spec stored verbatim,
pull_token auth, integrity relayed). The "phone" is simulated by the test
(arming + uploading an AES-GCM blob), so the whole transport loop is proven with
no network and no live Worker.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jasper.capture_relay import client as relay_client_module
from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayError, RelayResponse
from jasper.capture_relay.cues import (
    MEASUREMENT_FAILED_CUE_SLUG,
    RELAY_UNREACHABLE_CUE_SLUG,
)
from jasper.capture_relay.session import (
    CaptureAborted,
    CaptureFailed,
    CapturePageIncompatible,
    CaptureTimeout,
    classify_status,
    mint_session,
    register_session,
    run_capture,
)
from jasper.capture_relay.spec import build_room_sweep_spec

_CAPTURE_PAGE = {
    "schema_version": 1,
    "capture_protocol_version": 1,
    "supported_capture_protocol_versions": [1],
    "capture_page_build": "20260710.1",
}


class FakeRelayBackend:
    """In-memory transport mirroring the Worker's Pi-facing endpoints."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}

    def __call__(self, method, url, headers, body):
        path = urllib.parse.urlsplit(url).path
        parts = [p for p in path.split("/") if p]
        auth = headers.get("Authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""

        def jr(status, obj):
            return RelayResponse(status, {}, json.dumps(obj).encode())

        if parts == ["sessions"] and method == "POST":
            reg = json.loads(body)
            self.sessions[reg["session_id"]] = {
                "capture_spec": reg["capture_spec"],  # opaque string, verbatim
                "upload_token": reg["upload_token"],
                "pull_token": reg["pull_token"],
                "max_upload_bytes": reg["max_upload_bytes"],
                "state": "pending",
                "event": None,
                "host_event": None,
                "integrity": None,
                "blob": None,
            }
            return jr(201, {"session_id": reg["session_id"], "state": "pending"})

        if len(parts) >= 2 and parts[0] == "sessions":
            sid = parts[1]
            sub = parts[2] if len(parts) > 2 else ""
            s = self.sessions.get(sid)
            if not s:
                return jr(404, {"error": "not_found"})
            if sub in ("status", "blob", "") and token != s["pull_token"]:
                return jr(401, {"error": "unauthorized"})
            if sub == "status" and method == "GET":
                return jr(
                    200,
                    {
                        "state": s["state"],
                        "size": len(s["blob"] or b""),
                        "integrity": s["integrity"],
                        "event": s["event"],
                        "host_event": s["host_event"],
                        "expires_at": 0,
                    },
                )
            if sub == "host-event" and method == "POST":
                if token != s["pull_token"]:
                    return jr(401, {"error": "unauthorized"})
                s["host_event"] = json.loads(body)
                return jr(200, {"ok": True})
            if sub == "blob" and method == "GET":
                if s["state"] != "ready":
                    return jr(409, {"error": "not_ready"})
                return RelayResponse(
                    200,
                    {
                        "x-plaintext-length": str(s["integrity"]["plaintext_len"]),
                        "x-plaintext-sha256": s["integrity"]["sha256"],
                    },
                    s["blob"],
                )
            if sub == "" and method == "DELETE":
                del self.sessions[sid]
                return RelayResponse(204, {}, b"")
        return jr(404, {"error": "not_found"})

    # --- phone simulation ---
    def phone_arm(self, sid, device=None, *, noise_floor=None, setup=None):
        event = {"armed": True, "capture_page": dict(_CAPTURE_PAGE)}
        if device is not None:
            event["device"] = device
        if noise_floor is not None:
            event["noise_floor"] = noise_floor
        if setup is not None:
            event["setup"] = setup
        self.sessions[sid]["event"] = event

    def phone_setup_validate(self, sid, setup, *, token="setup-token"):
        self.sessions[sid]["event"] = {
            "setup_validate": True,
            "setup_token": token,
            "setup": setup,
            "capture_page": dict(_CAPTURE_PAGE),
        }

    def phone_abort(self, sid, reason="backgrounded"):
        self.sessions[sid]["event"] = {"aborted": True, "abort_reason": reason}

    def phone_upload(self, sid, content_key, wav):
        iv = os.urandom(crypto.IV_BYTES)
        blob = iv + AESGCM(content_key).encrypt(iv, wav, None)
        s = self.sessions[sid]
        s["blob"] = blob
        s["integrity"] = {
            "plaintext_len": len(wav),
            "sha256": hashlib.sha256(wav).hexdigest(),
        }
        s["state"] = "ready"

    def phone_upload_corrupt(self, sid, content_key, wav):
        # Valid ciphertext but a wrong integrity claim — must fail loud on the Pi.
        iv = os.urandom(crypto.IV_BYTES)
        blob = iv + AESGCM(content_key).encrypt(iv, wav, None)
        s = self.sessions[sid]
        s["blob"] = blob
        s["integrity"] = {"plaintext_len": len(wav), "sha256": "0" * 64}
        s["state"] = "ready"


def _mint(backend):
    spec = build_room_sweep_spec(position=1, total_positions=3)
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    return client, session


# --- tap link + registration --------------------------------------------------


def test_tap_link_carries_handle_in_fragment():
    session = mint_session(
        build_room_sweep_spec(),
        relay_base="https://relay.test",
        capture_origin="capture.test",
    )
    link = session.tap_link
    assert link.startswith("https://capture.test/#")
    frag = link.split("#", 1)[1]
    params = dict(p.split("=", 1) for p in frag.split("&"))
    assert params["s"] == session.session_id
    assert params["u"] == session.upload_token
    assert params["k"] == crypto.content_key_to_b64url(session.content_key)


def test_register_stores_opaque_spec_string():
    backend = FakeRelayBackend()
    _client, session = _mint(backend)
    stored = backend.sessions[session.session_id]
    # The relay holds the spec as the exact opaque JSON string we sent.
    assert stored["capture_spec"] == session.capture_spec_json()
    assert json.loads(stored["capture_spec"])["kind"] == "room_sweep"


# --- full round-trip ----------------------------------------------------------


def test_full_round_trip_returns_decrypted_wav():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF" + bytes(range(256)) * 8  # stand-in WAV bytes

    armed_calls = []

    def on_armed():
        armed_calls.append(True)
        # The host plays the stimulus; the phone finishes its window and uploads.
        backend.phone_upload(session.session_id, session.content_key, wav)

    # Phone armed before the Pi's first poll.
    backend.phone_arm(session.session_id)

    result = run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )
    assert result.wav == wav  # bit-identical, decrypted + verified
    assert result.device is None  # phone reported no device this time
    assert armed_calls == [True]  # on_armed fired exactly once


def test_stale_capture_page_fails_before_stimulus_and_publishes_reason(caplog):
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)
    backend.sessions[session.session_id]["event"].pop("capture_page")
    armed_calls = []

    with pytest.raises(CapturePageIncompatible, match="expected protocol 1"):
        run_capture(
            client,
            session,
            on_armed=lambda: armed_calls.append(True),
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
        )

    assert armed_calls == []
    assert backend.sessions[session.session_id]["host_event"]["phase"] == (
        "capture_incompatible"
    )
    assert "capture_relay.page_incompatible" in caplog.text


def test_device_flows_from_armed_event():
    # The phone's reported capture device rides the opaque `armed` event through
    # to the CaptureResult, so the Pi can make the device-aware calibration call.
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF" + bytes(range(64))
    device = {"label": "UMIK-1", "device_id": "abc"}
    backend.phone_arm(session.session_id, device=device)

    def on_armed():
        backend.phone_upload(session.session_id, session.content_key, wav)

    result = run_capture(
        client, session, on_armed=on_armed,
        poll_interval_s=0.0, timeout_s=5.0, sleep=lambda _s: None,
    )
    assert result.wav == wav
    assert result.device == device


def test_setup_validation_callback_runs_before_armed_capture():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF" + bytes(range(64))
    setup = {"calibration": {"mode": "serial", "model": "dayton_imm6"}}
    setup_calls = []
    armed_calls = []
    backend.phone_setup_validate(session.session_id, setup, token="tok-1")

    def on_setup(state):
        setup_calls.append((state.setup_token, state.setup))
        backend.phone_arm(session.session_id, setup=state.setup)

    def on_armed(state):
        armed_calls.append(state.setup)
        backend.phone_upload(session.session_id, session.content_key, wav)

    result = run_capture(
        client,
        session,
        on_setup=on_setup,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )

    assert result.wav == wav
    assert result.setup == setup
    assert setup_calls == [("tok-1", setup)]
    assert armed_calls == [setup]


def test_timeout_is_loud():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)  # armed, but never uploads

    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    with pytest.raises(CaptureTimeout):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=3.0,
            sleep=lambda _s: None,
            monotonic=lambda: next(ticks),
        )


def test_integrity_failure_is_loud():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF payload"

    def on_armed():
        backend.phone_upload_corrupt(session.session_id, session.content_key, wav)

    backend.phone_arm(session.session_id)
    with pytest.raises(CaptureFailed):
        run_capture(
            client,
            session,
            on_armed=on_armed,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
        )


def test_on_armed_not_fired_until_phone_arms():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF payload"
    poll_count = {"n": 0}
    armed_calls = []

    def status_then_arm():
        poll_count["n"] += 1
        # Arm only on the 3rd poll, then upload on arm.
        if poll_count["n"] == 3:
            backend.phone_arm(session.session_id)

    # Wrap the backend to inject the late-arm side effect on each status poll.
    base = backend.__call__

    def transport(method, url, headers, body):
        if url.endswith("/status") and method == "GET":
            status_then_arm()
        return base(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)

    def on_armed():
        armed_calls.append(poll_count["n"])
        backend.phone_upload(session.session_id, session.content_key, wav)

    result = run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )
    assert result.wav == wav
    assert armed_calls == [3]  # fired on the poll where armed first appeared


def test_state_aware_on_armed_receives_phone_setup():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF payload"
    setup = {
        "total_positions": 5,
        "calibration": {"mode": "none"},
    }
    seen = []

    backend.phone_arm(
        session.session_id,
        noise_floor={"duration_ms": 800, "rms_dbfs": -54.25},
        setup=setup,
    )

    def on_armed(state):
        seen.append((state.noise_floor, state.setup))
        backend.phone_upload(session.session_id, session.content_key, wav)

    result = run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )
    assert result.wav == wav
    assert result.noise_floor == {"duration_ms": 800, "rms_dbfs": -54.25}
    assert result.setup == setup
    assert seen == [(result.noise_floor, setup)]


# --- client unit behaviour ----------------------------------------------------


def test_client_register_body_shape():
    seen = {}

    def transport(method, url, headers, body):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = dict(headers)
        seen["body"] = json.loads(body)
        return RelayResponse(201, {}, b'{"state":"pending"}')

    client = RelayClient("https://relay.test/", transport=transport)
    client.register(
        session_id="cap_1",
        capture_spec_json='{"kind":"room_sweep"}',
        upload_token="up",
        pull_token="pu",
        ttl_s=900,
        max_upload_bytes=123,
    )
    assert seen["method"] == "POST"
    assert seen["url"] == "https://relay.test/sessions"
    assert seen["body"]["capture_spec"] == '{"kind":"room_sweep"}'
    assert seen["body"]["max_upload_bytes"] == 123
    assert relay_client_module.REGISTRATION_TOKEN_HEADER not in seen["headers"]


def test_client_register_sends_registration_token_only_when_configured():
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url, dict(headers)))
        return RelayResponse(201, {}, b'{"state":"pending"}')

    client = RelayClient(
        "https://relay.test/",
        transport=transport,
        registration_token=" pi-secret ",
    )
    client.register(
        session_id="cap_1",
        capture_spec_json='{"kind":"room_sweep"}',
        upload_token="up",
        pull_token="pu",
        ttl_s=900,
        max_upload_bytes=123,
    )
    client.status("cap_1", "pu")

    register_headers = calls[0][2]
    status_headers = calls[1][2]
    assert (
        register_headers[relay_client_module.REGISTRATION_TOKEN_HEADER]
        == "pi-secret"
    )
    assert relay_client_module.REGISTRATION_TOKEN_HEADER not in status_headers


def test_client_post_host_event_uses_pull_token():
    seen = {}

    def transport(method, url, headers, body):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = dict(headers)
        seen["body"] = json.loads(body)
        return RelayResponse(200, {}, b'{"ok":true}')

    client = RelayClient("https://relay.test/", transport=transport)
    client.post_host_event(
        "cap_1",
        "pull-secret",
        {"phase": "sweep_complete", "position": 1},
    )

    assert seen["method"] == "POST"
    assert seen["url"] == "https://relay.test/sessions/cap_1/host-event"
    assert seen["headers"]["Authorization"] == "Bearer pull-secret"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["body"] == {"phase": "sweep_complete", "position": 1}


def test_urllib_transport_sends_cloudflare_safe_defaults(monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        headers = {}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _urlopen(req, *, timeout):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(relay_client_module.urllib.request, "urlopen", _urlopen)

    resp = relay_client_module._urllib_transport(
        "GET",
        "https://relay.test/sessions/cap_1/status",
        {"Authorization": "Bearer pull-token"},
        None,
        timeout=3.5,
    )

    assert resp.status == 200
    assert seen["timeout"] == 3.5
    assert seen["headers"]["authorization"] == "Bearer pull-token"
    assert seen["headers"]["user-agent"] == relay_client_module.RELAY_USER_AGENT
    assert seen["headers"]["accept"] == "application/json"


def test_urllib_transport_preserves_explicit_user_agent(monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        headers = {}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _urlopen(req, *, timeout):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(relay_client_module.urllib.request, "urlopen", _urlopen)

    relay_client_module._urllib_transport(
        "GET",
        "https://relay.test/healthz",
        {"User-Agent": "Custom/1", "Accept": "text/plain"},
        None,
    )

    assert seen["headers"]["user-agent"] == "Custom/1"
    assert seen["headers"]["accept"] == "text/plain"


def test_client_raises_relay_error_on_non_2xx():
    def transport(method, url, headers, body):
        return RelayResponse(401, {}, b'{"error":"unauthorized"}')

    client = RelayClient("https://relay.test", transport=transport)
    with pytest.raises(RelayError) as ei:
        client.status("cap_1", "pu")
    assert ei.value.status == 401


def test_client_delete_tolerates_404():
    def transport(method, url, headers, body):
        return RelayResponse(404, {}, b'{"error":"not_found"}')

    client = RelayClient("https://relay.test", transport=transport)
    client.delete("cap_1", "pu")  # already gone / TTL-expired is fine for a purge


def test_pull_blob_parses_integrity_headers():
    def transport(method, url, headers, body):
        return RelayResponse(
            200,
            {"x-plaintext-length": "42", "x-plaintext-sha256": "ab" * 32},
            b"ciphertext-bytes",
        )

    client = RelayClient("https://relay.test", transport=transport)
    blob, integrity = client.pull_blob("cap_1", "pu")
    assert blob == b"ciphertext-bytes"
    assert integrity == {"plaintext_len": 42, "sha256": "ab" * 32}


def test_client_requires_https_base_without_custom_transport():
    # Outbound-HTTPS-only: a real client refuses a non-https base so tokens can
    # never go over http://. An injected transport (tests) bypasses the guard.
    with pytest.raises(ValueError, match="https"):
        RelayClient("http://relay.test")
    RelayClient("https://relay.test")  # ok
    RelayClient("http://relay.test", transport=lambda *_a: RelayResponse(200, {}, b"{}"))


# --- observability (event= logs) ---------------------------------------------


def test_observability_logs_the_capture_lifecycle(caplog):
    caplog.set_level(logging.INFO, logger="jasper.capture_relay.session")
    backend = FakeRelayBackend()
    client, session = _mint(backend)  # logs capture_relay.registered
    wav = b"RIFF" + bytes(range(64))

    def on_armed():
        backend.phone_upload(session.session_id, session.content_key, wav)

    backend.phone_arm(session.session_id)
    run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )
    text = caplog.text
    for ev in ("registered", "armed", "ready", "captured"):
        assert f"capture_relay.{ev}" in text, ev
    # session_id is logged (CSPRNG, non-secret); tokens/keys never are.
    assert session.upload_token not in text
    assert session.pull_token not in text
    assert crypto.content_key_to_b64url(session.content_key) not in text


def test_failure_logs_warning_with_reason_and_traceback(caplog):
    caplog.set_level(logging.WARNING, logger="jasper.capture_relay.session")
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_abort(session.session_id)
    with pytest.raises(CaptureAborted):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
        )
    assert "capture_relay.failed" in caplog.text
    assert "CaptureAborted" in caplog.text  # operator can see the real cause


def test_classify_status():
    assert classify_status({"state": "pending", "event": None}).armed is False
    assert classify_status({"state": "pending", "event": {"armed": True}}).armed is True
    assert classify_status({"state": "ready", "event": {"armed": True}}).ready is True
    aborted = classify_status({"state": "pending", "event": {"aborted": True, "reason": "lock"}})
    assert aborted.aborted is True
    assert aborted.abort_reason == "lock"
    noisy = classify_status(
        {
            "state": "pending",
            "event": {
                "armed": True,
                "noise_floor": {"duration_ms": 800, "rms_dbfs": -52.5},
            },
        }
    )
    assert noisy.noise_floor == {"duration_ms": 800, "rms_dbfs": -52.5}
    setup = classify_status(
        {
            "state": "pending",
            "event": {
                "setup_validate": True,
                "setup_token": "tok",
                "setup": {"calibration": {"mode": "serial"}},
            },
        }
    )
    assert setup.setup_validate is True
    assert setup.setup_token == "tok"
    assert setup.setup == {"calibration": {"mode": "serial"}}
    capture_page = classify_status(
        {"state": "pending", "event": {"capture_page": _CAPTURE_PAGE}}
    )
    assert capture_page.capture_page == _CAPTURE_PAGE


# --- step 7: abort + no-silent-failure cues ----------------------------------


def test_phone_abort_raises_loud():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_abort(session.session_id, reason="backgrounded")
    with pytest.raises(CaptureAborted):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
        )


def test_play_cue_fires_on_timeout():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)  # armed but never uploads
    cues = []
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    with pytest.raises(CaptureTimeout):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=2.0,
            sleep=lambda _s: None,
            monotonic=lambda: next(ticks),
            play_cue=cues.append,
        )
    # No-silent-failure: the speaker is told why (plan §12).
    assert cues == [MEASUREMENT_FAILED_CUE_SLUG]


def test_play_cue_fires_on_integrity_failure():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF payload"
    cues = []

    def on_armed():
        backend.phone_upload_corrupt(session.session_id, session.content_key, wav)

    backend.phone_arm(session.session_id)
    with pytest.raises(CaptureFailed):
        run_capture(
            client,
            session,
            on_armed=on_armed,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
            play_cue=cues.append,
        )
    assert cues == [MEASUREMENT_FAILED_CUE_SLUG]


def test_relay_death_mid_poll_cues_unreachable_and_propagates():
    # The relay 5xx's mid-poll -> client.status raises RelayError(503); run_capture
    # cues measurement_relay_unreachable and re-raises (no un-cued escape).
    def transport(method, url, headers, body):
        if url.endswith("/sessions"):  # registration succeeds
            return RelayResponse(201, {}, b'{"state":"pending"}')
        return RelayResponse(503, {}, b'{"error":"upstream"}')  # status 5xx

    client = RelayClient("https://relay.test", transport=transport)
    session = mint_session(
        build_room_sweep_spec(), relay_base="https://relay.test", capture_origin="c.test"
    )
    register_session(client, session)
    cues = []
    with pytest.raises(RelayError):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
            play_cue=cues.append,
        )
    assert cues == [RELAY_UNREACHABLE_CUE_SLUG]


def test_cue_is_best_effort_and_never_masks_the_failure():
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_abort(session.session_id)

    def boom(_slug):
        raise RuntimeError("cue subsystem down")

    # A failing cue must not swallow or replace the real CaptureAborted.
    with pytest.raises(CaptureAborted):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
            play_cue=boom,
        )
