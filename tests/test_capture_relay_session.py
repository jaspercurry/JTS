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

from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayError, RelayResponse
from jasper.capture_relay.cues import (
    MEASUREMENT_FAILED_CUE_SLUG,
    RELAY_UNREACHABLE_CUE_SLUG,
)
from jasper.capture_relay.session import (
    CaptureAborted,
    CaptureFailed,
    CaptureTimeout,
    classify_status,
    mint_session,
    register_session,
    run_capture,
)
from jasper.capture_relay.spec import build_room_sweep_spec


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
                        "expires_at": 0,
                    },
                )
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
    def phone_arm(self, sid, device=None):
        event = {"armed": True}
        if device is not None:
            event["device"] = device
        self.sessions[sid]["event"] = event

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


# --- client unit behaviour ----------------------------------------------------


def test_client_register_body_shape():
    seen = {}

    def transport(method, url, headers, body):
        seen["method"] = method
        seen["url"] = url
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
