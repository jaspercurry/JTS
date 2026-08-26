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

import json
import logging

import pytest

from jasper.capture_relay import client as relay_client_module
from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayError, RelayResponse
from jasper.capture_relay.cues import (
    MEASUREMENT_FAILED_CUE_SLUG,
    RELAY_UNREACHABLE_CUE_SLUG,
)
from jasper.capture_relay.session import (
    BLOB_PULL_TRANSIENT_GRACE_S,
    STATUS_POLL_TRANSIENT_GRACE_S,
    CaptureAborted,
    CaptureFailed,
    CapturePageIncompatible,
    CaptureStopped,
    CaptureTimeout,
    _run_with_failure_cues,
    classify_status,
    mint_session,
    register_session,
    run_capture,
)
from jasper.capture_relay.spec import build_room_sweep_spec
from jasper.capture_relay.spec import build_crossover_sweep_spec
from tests._capture_relay_fake import CAPTURE_PAGE as _CAPTURE_PAGE
from tests._capture_relay_fake import FakeRelayBackend


def _mint(backend):
    spec = build_room_sweep_spec(position=1, total_positions=3)
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    backend.bind_phone(session)
    return client, session


def _test_clock(step_s=0.0):
    """A clock the runner cannot advance merely by READING it.

    Time moves only where it really passes: ``step_s`` per poll the runner
    sleeps through, plus whatever a stalled call burns inside itself. That
    keeps a test's wall-time claims exact and independent of how many times
    the runner happens to read the clock. Returns the mutable clock (so a
    stall can charge it), its reader, and the sleep.
    """
    clock = {"t": 0.0}

    def sleep(_s):
        clock["t"] += step_s

    return clock, (lambda: clock["t"]), sleep


def _stall(client, clock, method, *, stalls, stall_s, exc=None):
    """Make the first ``stalls`` calls to ``client.<method>`` stall, then serve.

    Each stall burns ``stall_s`` of the clock the way a request that times out
    does — the wall time is spent INSIDE the call, which is why the runner's
    grace is wall-clock rather than a retry count. Returns a counter so a test
    can prove how many calls were actually attempted.
    """
    real = getattr(client, method)
    budget = {"left": stalls}
    counts = {"stalled": 0}

    def stalling(*args, **kwargs):
        if budget["left"] > 0:
            budget["left"] -= 1
            counts["stalled"] += 1
            clock["t"] += stall_s
            raise (exc if exc is not None else TimeoutError("relay stalled"))
        return real(*args, **kwargs)

    setattr(client, method, stalling)
    return counts


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
    assert params["a"]


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


def test_host_stop_after_poll_prevents_arm_without_failure_cue(caplog):
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)
    checks = iter((False, True))
    armed_calls = []
    cues = []

    with caplog.at_level(logging.INFO), pytest.raises(
        CaptureStopped, match="capture stopped"
    ):
        run_capture(
            client,
            session,
            on_armed=lambda: armed_calls.append(True),
            stop_requested=lambda: next(checks),
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
            play_cue=cues.append,
        )

    assert armed_calls == []
    assert cues == []
    assert "event=capture_relay.stopped" in caplog.text
    assert "event=capture_relay.failed" not in caplog.text


def test_required_acknowledgement_is_verified_before_stimulus():
    backend = FakeRelayBackend()
    binding = "placement_abcdefghijklmnopqrstuv"
    session = mint_session(
        build_crossover_sweep_spec(
            driver_label="Woofer driver",
            driver_role="woofer",
            acknowledgement_binding=binding,
        ),
        relay_base="https://relay.test",
        capture_origin="capture.test",
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    backend.bind_phone(session)
    backend.phone_arm(
        session.session_id,
        acknowledgement={
            "schema_version": 1,
            "id": "driver_same_distance_v1",
            "binding_id": "wrong_binding_abcdefghijkl",
            "accepted": True,
        },
        auth_session=session,
    )
    armed_calls = []

    with pytest.raises(CaptureFailed, match="acknowledgement"):
        run_capture(
            client,
            session,
            on_armed=lambda: armed_calls.append(True),
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=lambda _s: None,
        )

    assert armed_calls == []
    assert backend.sessions[session.session_id]["host_event"] == {
        "phase": "sweep_failed",
        "error": "Confirm the microphone placement before starting the sweep.",
    }

    wav = b"RIFF" + bytes(range(64))

    def on_armed():
        armed_calls.append(True)
        backend.phone_upload(session.session_id, session.content_key, wav)

    backend.phone_arm(
        session.session_id,
        acknowledgement={
            "schema_version": 1,
            "id": "driver_same_distance_v1",
            "binding_id": binding,
            "accepted": True,
        },
        auth_session=session,
        sequence=2,
    )
    result = run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=5.0,
        sleep=lambda _s: None,
    )
    assert result.wav == wav
    assert armed_calls == [True]


@pytest.mark.parametrize(
    "acknowledgement",
    [
        None,
        {
            "schema_version": 1,
            "id": "wrong_policy_v1",
            "binding_id": "placement_abcdefghijklmnopqrstuv",
            "accepted": True,
        },
        {
            "schema_version": 1,
            "id": "driver_same_distance_v1",
            "binding_id": "placement_from_prior_session_xyz",
            "accepted": True,
        },
        {
            "schema_version": 1,
            "id": "driver_same_distance_v1",
            "binding_id": "placement_abcdefghijklmnopqrstuv",
            "accepted": False,
        },
        {
            "schema_version": 9,
            "id": "driver_same_distance_v1",
            "binding_id": "placement_abcdefghijklmnopqrstuv",
            "accepted": True,
        },
    ],
    ids=["missing", "wrong-id", "prior-session-binding", "not-accepted", "schema"],
)
def test_invalid_placement_acknowledgement_never_reaches_playback(
    acknowledgement,
):
    backend = FakeRelayBackend()
    session = mint_session(
        build_crossover_sweep_spec(
            driver_label="Woofer driver",
            driver_role="woofer",
            acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
        ),
        relay_base="https://relay.test",
        capture_origin="capture.test",
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    backend.bind_phone(session)
    backend.phone_arm(
        session.session_id,
        acknowledgement=acknowledgement,
        auth_session=session,
    )
    armed_calls = []

    with pytest.raises(CaptureFailed, match="acknowledgement"):
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
        "sweep_failed"
    )


@pytest.mark.parametrize("failure_mode", ["unsigned", "tampered", "other-session"])
def test_control_integrity_fails_before_playback(failure_mode):
    backend = FakeRelayBackend()
    binding = "placement_abcdefghijklmnopqrstuv"
    session = mint_session(
        build_crossover_sweep_spec(
            driver_label="Woofer driver",
            driver_role="woofer",
            acknowledgement_binding=binding,
        ),
        relay_base="https://relay.test",
        capture_origin="capture.test",
    )
    client = RelayClient("https://relay.test", transport=backend)
    register_session(client, session)
    backend.bind_phone(session)
    acknowledgement = {
        "schema_version": 1,
        "id": "driver_same_distance_v1",
        "binding_id": binding,
        "accepted": True,
    }
    if failure_mode == "unsigned":
        # Written past the fake phone's helper on purpose: nothing in the
        # product can emit a bare event any more, so the only way to test the
        # refusal is to forge the relay slot directly.
        backend.sessions[session.session_id]["event"] = {
            "armed": True,
            "capture_page": dict(_CAPTURE_PAGE),
            "acknowledgement": acknowledgement,
        }
    elif failure_mode == "other-session":
        other = mint_session(
            session.spec,
            relay_base="https://relay.test",
            capture_origin="capture.test",
        )
        backend.phone_arm(
            session.session_id,
            acknowledgement=acknowledgement,
            auth_session=other,
        )
    else:
        backend.phone_arm(
            session.session_id,
            acknowledgement=acknowledgement,
            auth_session=session,
        )
        envelope = backend.sessions[session.session_id]["event"][
            "authenticated_event"
        ]
        envelope["payload"] = envelope["payload"].replace("\"armed\":true", "\"armed\":false")

    armed_calls = []
    with pytest.raises(CaptureFailed, match="control integrity"):
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


def test_stale_capture_page_fails_before_stimulus_and_publishes_reason(caplog):
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    # A properly AUTHENTICATED armed event that simply carries no page
    # identity: integrity passes, the compatibility handshake still refuses.
    # (Popping the key after signing would fail integrity first and never
    # reach the check this test is about.)
    backend.sessions[session.session_id]["event"] = backend._authenticate(
        session.session_id, {"armed": True}
    )
    armed_calls = []

    with pytest.raises(CapturePageIncompatible, match="expected protocol 3"):
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
        # sequence=2: the setup_validate event above is sequence 1, and the
        # authenticated envelope is strictly monotonic per session.
        backend.phone_arm(session.session_id, setup=state.setup, sequence=2)

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


def test_armed_without_upload_times_out_after_one_fresh_window(caplog):
    caplog.set_level(logging.WARNING, logger="jasper.capture_relay.session")
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)  # armed, but never uploads
    armed_calls = []

    _clock, monotonic, sleep = _test_clock(1.0)

    with pytest.raises(
        CaptureTimeout,
        match=r"phone never uploaded within 3s after arming",
    ):
        run_capture(
            client,
            session,
            on_armed=lambda: armed_calls.append(True),
            poll_interval_s=0.0,
            timeout_s=3.0,
            sleep=sleep,
            monotonic=monotonic,
        )
    assert armed_calls == [True]
    assert "event=capture_relay.failed" in caplog.text
    assert "phase=awaiting_upload" in caplog.text


def test_phone_that_never_arms_remains_bounded_by_pre_arm_window(caplog):
    caplog.set_level(logging.WARNING, logger="jasper.capture_relay.session")
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    armed_calls = []
    _clock, monotonic, sleep = _test_clock(1.0)

    with pytest.raises(CaptureTimeout, match=r"phone never armed within 3s"):
        run_capture(
            client,
            session,
            on_armed=lambda: armed_calls.append(True),
            poll_interval_s=0.0,
            timeout_s=3.0,
            sleep=sleep,
            monotonic=monotonic,
        )
    assert armed_calls == []
    assert "event=capture_relay.failed" in caplog.text
    assert "phase=awaiting_arm" in caplog.text


def test_late_arm_gets_a_fresh_bounded_capture_window(monkeypatch):
    """Operator setup time must not consume the sweep/upload budget."""
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF late arm"
    status = client.status
    polls = 0

    def status_then_arm(session_id, pull_token):
        nonlocal polls
        polls += 1
        if polls == 3:
            backend.phone_arm(session.session_id)
        return status(session_id, pull_token)

    monkeypatch.setattr(client, "status", status_then_arm)

    def on_armed():
        backend.phone_upload(session.session_id, session.content_key, wav)

    # The original deadline expires exactly ON the arm poll.  A fresh window
    # lets the next poll observe the upload; it stays bounded from that arm.
    _clock, monotonic, sleep = _test_clock(1.5)
    result = run_capture(
        client,
        session,
        on_armed=on_armed,
        poll_interval_s=0.0,
        timeout_s=3.0,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert result.wav == wav
    assert polls == 4


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


@pytest.mark.parametrize(
    ("response", "served"),
    [
        (RelayResponse(404, {}, b'{"error":"not_found"}'), False),
        (RelayResponse(500, {}, b'{"error":"boom"}'), False),
        # A 2xx that is not a capability document is NOT the same as a missing
        # endpoint: something answered for the relay and it was not the relay.
        # `served=True` is what keeps the refusal from blaming the release.
        (RelayResponse(200, {}, b"<html>captive portal</html>"), True),
        (RelayResponse(200, {}, b'["a","list"]'), True),
        (RelayResponse(200, {}, b""), True),
    ],
    ids=[
        "pre-capacity-404",
        "server-error",
        "html-interstitial",
        "not-an-object",
        "empty",
    ],
)
def test_client_capabilities_reports_no_document_rather_than_raising(response, served):
    # `capabilities()` is a VERSION probe: nothing here raises, every outcome
    # leaves the caller failing closed, and `served` preserves WHY.
    def transport(method, url, headers, body):
        assert method == "GET"
        assert url == "https://relay.test/capabilities"
        return response

    client = RelayClient("https://relay.test", transport=transport)
    probe = client.capabilities()
    assert probe.document is None
    assert probe.served is served


def test_client_capabilities_returns_the_document():
    def transport(method, url, headers, body):
        return RelayResponse(
            200, {}, b'{"schema_version":1,"max_capture_plan_attempts":32}'
        )

    client = RelayClient("https://relay.test", transport=transport)
    probe = client.capabilities()
    assert probe.served is True
    assert probe.document == {
        "schema_version": 1,
        "max_capture_plan_attempts": 32,
    }


def test_client_capabilities_propagates_an_unreachable_relay():
    # An unreachable relay must NOT be silently reclassified as an old
    # deployment — it keeps raising so it reaches the relay-unreachable cue.
    def transport(method, url, headers, body):
        raise OSError("no route to host")

    client = RelayClient("https://relay.test", transport=transport)
    with pytest.raises(OSError):
        client.capabilities()


def test_client_requires_https_base_without_custom_transport():
    # Outbound-HTTPS-only: a real client refuses a non-https base so tokens can
    # never go over http://. An injected transport (tests) bypasses the guard.
    with pytest.raises(ValueError, match="https"):
        RelayClient("http://relay.test")
    RelayClient("https://relay.test")  # ok
    RelayClient("http://relay.test", transport=lambda *_a: RelayResponse(200, {}, b"{}"))


def test_client_with_timeout_clones_the_control_transport():
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        return RelayResponse(200, {}, b"{}")

    client = RelayClient(
        "https://relay.test",
        transport=transport,
        timeout=10.0,
        registration_token="registration",
    )
    control = client.with_timeout(1.5)

    assert control is not client
    assert control._timeout == 1.5
    control.status("cap_1", "pull")
    assert calls == [("GET", "https://relay.test/sessions/cap_1/status")]


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


def test_failure_reason_names_config_rejected_not_camilla_unavailable(caplog):
    """W6 hardware run 4 finding J: a healthy CamillaDSP that REJECTED a config
    (e.g. "Use of missing mixer 'split_active_2way'") used to log
    ``reason=CamillaUnavailable`` here -- indistinguishable from an actually
    unreachable/dead daemon. ``CamillaConfigRejected`` (jasper.camilla) is a
    ``CamillaUnavailable`` subclass raised for exactly that case; this reason
    field is derived generically as ``type(exc).__name__`` (see
    ``_run_with_failure_cues`` above), so the honest ``reason=
    CamillaConfigRejected`` falls out for free -- no change needed here."""
    from jasper.camilla import CamillaConfigRejected, CamillaUnavailable

    caplog.set_level(logging.WARNING, logger="jasper.capture_relay.session")
    backend = FakeRelayBackend()
    _client, session = _mint(backend)

    def runner():
        raise CamillaConfigRejected("Use of missing mixer 'split_active_2way'")

    with pytest.raises(CamillaUnavailable):
        _run_with_failure_cues(session, None, runner)
    assert "reason=CamillaConfigRejected" in caplog.text
    assert "reason=CamillaUnavailable" not in caplog.text


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
    _clock, monotonic, sleep = _test_clock(1.0)
    with pytest.raises(CaptureTimeout):
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=2.0,
            sleep=sleep,
            monotonic=monotonic,
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
    # A relay that 5xx's on EVERY poll is an outage the status grace rides for
    # its window and then gives up on: run_capture cues
    # measurement_relay_unreachable and re-raises the original RelayError (no
    # un-cued escape, and no infinite retry).
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
    _clock, monotonic, sleep = _test_clock(5.0)
    with pytest.raises(RelayError) as excinfo:
        run_capture(
            client,
            session,
            on_armed=lambda: None,
            poll_interval_s=0.0,
            timeout_s=5.0,
            sleep=sleep,
            monotonic=monotonic,
            play_cue=cues.append,
        )
    assert excinfo.value.status == 503
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


# --- transient relay outages, single-capture runner (issue #2453) -------------
#
# The runner behind room sweep / balance / sync polls status and then pulls the
# blob, and both calls used to be bare here: one stalled request killed a live
# measurement, and a stalled PULL destroyed a capture the household had already
# uploaded. It now consumes the same tolerance the plan runner does. These pin
# both halves of that promise — a transient outage is survived, a settled
# "this session is dead" answer is still fatal on the FIRST read.


@pytest.mark.parametrize("call", ("status", "pull_blob"))
def test_one_transient_stall_does_not_void_a_single_capture(call):
    """A 10 s stall on either relay call, inside the grace, is ridden out and
    the capture still comes back — bit-identical, analyzed, not voided."""
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    wav = b"RIFF" + bytes(range(256))
    backend.phone_arm(session.session_id)
    clock, monotonic, sleep = _test_clock()
    counts = _stall(client, clock, call, stalls=1, stall_s=10.0)

    result = run_capture(
        client,
        session,
        on_armed=lambda: backend.phone_upload(
            session.session_id, session.content_key, wav
        ),
        poll_interval_s=0.0,
        timeout_s=120.0,
        sleep=sleep,
        monotonic=monotonic,
    )

    assert result.wav == wav
    # The stall really happened, and it really burned most of the window.
    assert counts["stalled"] == 1
    assert clock["t"] == 10.0 < min(
        STATUS_POLL_TRANSIENT_GRACE_S, BLOB_PULL_TRANSIENT_GRACE_S
    )


@pytest.mark.parametrize("call", ("status", "pull_blob"))
def test_a_dead_session_answer_is_never_retried_in_a_single_capture(call):
    """The boundary the tolerance must not cross. 401/403/404/410 is the relay
    stating a settled fact — the session is purged, the blob is gone — so it
    stays fatal on the first read, with the same status the caller has always
    classified on, and burns none of the grace re-asking."""
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)
    clock, monotonic, sleep = _test_clock()
    counts = _stall(
        client,
        clock,
        call,
        stalls=99,
        stall_s=10.0,
        exc=RelayError("gone", 410),
    )

    with pytest.raises(RelayError) as excinfo:
        run_capture(
            client,
            session,
            on_armed=lambda: backend.phone_upload(
                session.session_id, session.content_key, b"RIFF unread"
            ),
            poll_interval_s=0.0,
            timeout_s=120.0,
            sleep=sleep,
            monotonic=monotonic,
        )

    assert excinfo.value.status == 410
    assert counts["stalled"] == 1  # raised on the first, never re-asked
    assert clock["t"] == 10.0


def test_a_sustained_blob_pull_outage_still_ends_a_single_capture():
    """Tolerance, not infinite retry — and the reason the pull carries its OWN
    outage anchor.

    Every retry here is preceded by a status poll that SUCCEEDS, one line
    above it in the same loop, so an anchor shared with the status grace would
    be cleared on every pass and this session would retry until the
    phone-inactivity deadline fired and blamed the household for an upload
    they had already completed."""
    backend = FakeRelayBackend()
    client, session = _mint(backend)
    backend.phone_arm(session.session_id)
    clock, monotonic, sleep = _test_clock()
    status_ok = {"n": 0}
    real_status = client.status

    def status(*args, **kwargs):
        status_ok["n"] += 1
        return real_status(*args, **kwargs)

    client.status = status
    counts = _stall(client, clock, "pull_blob", stalls=99, stall_s=10.0)

    with pytest.raises(TimeoutError):
        run_capture(
            client,
            session,
            on_armed=lambda: backend.phone_upload(
                session.session_id, session.content_key, b"RIFF lost"
            ),
            poll_interval_s=0.0,
            timeout_s=120.0,
            sleep=sleep,
            monotonic=monotonic,
        )

    # Bounded: it gave up on the SECOND stall (10 s is inside the 15 s window,
    # 20 s is past it), nowhere near the 99 it was offered — and it did so
    # while the control plane was answering every single time.
    assert counts["stalled"] == 2
    assert status_ok["n"] > counts["stalled"]
    assert clock["t"] == 20.0 > BLOB_PULL_TRANSIENT_GRACE_S


# --- which clock ran out (work order D8, issue #1807) -------------------------


def test_expired_time_budget_names_the_clock_or_stays_silent():
    """Every one of these deaths persists as the same failure code, so this
    classifier is where the difference survives to the household.

    Deliberately conservative: an unclassifiable death answers ``""`` and the
    surfaces fall back to their existing generic copy. Naming the wrong clock
    would be worse than naming none — "you have two minutes between taps" is
    advice, and advice about the wrong budget sends a household back to fail
    the same way.
    """
    from jasper.capture_relay.session import (
        TIME_BUDGET_LINK,
        TIME_BUDGET_STEP,
        expired_time_budget,
    )

    # The phone-inactivity budget, in every relay phase that can spend it.
    for phase in ("awaiting_begin", "awaiting_arm", "awaiting_upload", None):
        assert (
            expired_time_budget(CaptureTimeout("gone", phase=phase))
            == TIME_BUDGET_STEP
        )

    # A purged or expired relay session answers the Pi exactly as it answers
    # the phone (the page's own isDeadSessionError reads the same statuses).
    for status in (401, 403, 404, 410):
        assert (
            expired_time_budget(RelayError("dead", status)) == TIME_BUDGET_LINK
        )

    # An outage is not a budget, and neither is a deliberate stop or a corrupt
    # blob. Each of these already has its own honest copy.
    assert expired_time_budget(RelayError("relay is broken", 502)) == ""
    assert expired_time_budget(OSError("connection reset")) == ""
    assert expired_time_budget(CaptureAborted("stopped", reason="stopped")) == ""
    assert expired_time_budget(CaptureFailed("bad blob")) == ""


def test_transient_relay_failures_are_exactly_the_ones_no_clock_explains():
    """The retry classifier and the clock classifier must PARTITION relay
    deaths, not overlap (issue #2083).

    Anything ``expired_time_budget`` can name a clock for is the relay stating
    a settled fact, and re-polling it would only delay an honest terminal;
    anything it cannot is either an outage worth one more look or a failure
    that was never about the transport. Asserting the two functions together
    is the point — a status that both retried AND read as an expired link
    would spend the grace re-asking a question already answered.
    """
    from jasper.capture_relay.session import (
        TIME_BUDGET_LINK,
        expired_time_budget,
        is_transient_relay_failure,
    )

    # Retried: the relay is unreachable or unwell, and says nothing about the
    # session. Mirrors correction_setup._post_relay_host_event's own rule.
    for exc in (
        OSError("connection reset"),
        TimeoutError("timed out"),  # an OSError subclass — the incident's shape
        RelayError("bad gateway", 502),
        RelayError("unavailable", 503),
        RelayError("gateway timeout", 504),
        RelayError("slow down", 429),
    ):
        assert is_transient_relay_failure(exc) is True, exc
        assert expired_time_budget(exc) == ""  # …and names no clock

    # Never retried: a dead session. These are precisely the statuses
    # expired_time_budget calls TIME_BUDGET_LINK, so the partition holds.
    for status in (401, 403, 404, 410):
        exc = RelayError("dead", status)
        assert is_transient_relay_failure(exc) is False, status
        assert expired_time_budget(exc) == TIME_BUDGET_LINK

    # Never retried: a 4xx that is our own bug, and the non-transport failures.
    assert is_transient_relay_failure(RelayError("bad request", 400)) is False
    assert is_transient_relay_failure(CaptureFailed("bad blob")) is False
    assert is_transient_relay_failure(CaptureTimeout("gone")) is False
    assert is_transient_relay_failure(ValueError("nonsense")) is False


def test_max_ttl_stays_in_lockstep_with_the_worker():
    """The Pi's ``MAX_TTL_S`` mirrors the Worker's, and the Worker CLAMPS.

    Both halves matter. The mirror is what lets a caller sizing a long link
    (``correction_crossover_v2.relay_link_ttl_s``, issue #2509) clamp on this
    side; the clamp is WHY it has to. An over-large request is not refused by
    the relay — it is silently cut back — and the session publishes its
    requested ``ttl_s`` to the phone as ``time_budget.session_s``, so an
    unclamped caller would tell the household a link lifetime the relay never
    granted.
    """
    from pathlib import Path

    from jasper.capture_relay.session import MAX_TTL_S

    worker_src = (
        Path(__file__).resolve().parent.parent / "relay" / "src" / "worker.js"
    ).read_text(encoding="utf-8")
    assert f"const MAX_TTL_S = {MAX_TTL_S};" in worker_src, (
        "worker link-TTL ceiling drifted from the Pi-side mirror"
    )
    assert "ttl = Math.max(MIN_TTL_S, Math.min(MAX_TTL_S, ttl));" in worker_src, (
        "the worker must CLAMP a requested ttl_s — a refusal instead would "
        "make the Pi-side clamp a silent downgrade rather than a disclosure fix"
    )
