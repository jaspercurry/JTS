# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Wake-corpus HTTP routing, response, and control-surface tests."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from jasper.wake_corpus import bridge_session, runtime_probe
from jasper.web import wake_corpus_setup

from tests.wake_corpus_setup_fixtures import (
    TEST_CSRF_TOKEN,
    _corpus_post_handler,
    _backend_fixture,
    _mutating_status,
    _patch_udp,
    _running_server_port_fixture,
    _use_tmp_bridge_env,
)

_IMPORTED_FIXTURES = (
    _backend_fixture,
    _patch_udp,
    _running_server_port_fixture,
)


def test_level_sse_returns_event_stream_headers(backend, running_server_port: int) -> None:
    """GET /api/recording/level must return 200 + correct SSE headers
    (Content-Type, no-cache, X-Accel-Buffering: no for nginx). We
    don't read the body — just confirm the headers, then close."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/recording/level")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/event-stream"
        assert resp.getheader("X-Accel-Buffering") == "no"
        # Don't drain the stream — it's open-ended. Closing the
        # connection cleanly exits the handler.
    finally:
        conn.close()


def test_level_sse_streams_idle_payload_when_not_recording(
    backend,
    running_server_port: int,
) -> None:
    """When no recording is in flight, the stream emits frames with
    recording=false + rms_dbfs=null. Read one frame then disconnect."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/recording/level")
    resp = conn.getresponse()
    try:
        # SSE frames are 'data: <json>\n\n' — read up to the first
        # blank line.
        line = resp.fp.readline().decode()
        assert line.startswith("data: "), f"unexpected: {line!r}"
        payload = json.loads(line[len("data: "):].strip())
        assert payload == {"recording": False, "rms_dbfs": None}
    finally:
        conn.close()


def test_level_sse_does_not_require_csrf(backend, running_server_port: int) -> None:
    """The SSE endpoint is read-only — like /api/status — and must
    NOT 403 when the X-CSRF-Token header is absent. (Browsers can't
    send custom headers on EventSource connections anyway.)"""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    # No X-CSRF-Token header — must still get 200
    conn.request("GET", "/api/recording/level")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mutating-request routing — route-check before CSRF-check (the wizard
# convention in jasper/web/_common.py: bogus paths return 404 without
# revealing CSRF state). Pre-fix, do_POST/do_DELETE checked CSRF first and
# 403'd on unknown paths.
# ---------------------------------------------------------------------------


def test_post_unknown_path_404s_without_revealing_csrf_state(running_server_port: int) -> None:
    assert _mutating_status(running_server_port, "POST", "/api/nope") == 404


def test_post_known_path_without_token_403s(running_server_port: int) -> None:
    assert _mutating_status(running_server_port, "POST", "/api/session") == 403


def test_post_known_path_disallowed_host_403s(running_server_port: int) -> None:
    """guard_mutating_host rejects on the Host axis even carrying a
    token that would otherwise pass — the host guard runs before the
    token compare (_check_csrf's documented ordering)."""
    assert (
        _mutating_status(
            running_server_port,
            "POST",
            "/api/session",
            token=TEST_CSRF_TOKEN,
            extra_headers={"Host": "evil.example"},
        )
        == 403
    )


def test_post_known_path_with_non_ascii_token_403s(running_server_port):
    """A latin-1 byte in the token header must 403 like any wrong token,
    never abort the connection (secrets.compare_digest raises TypeError
    on non-ASCII str)."""
    assert (
        _mutating_status(
            running_server_port,
            "POST",
            "/api/session",
            token="\u00e9" * 32,
        )
        == 403
    )


@pytest.mark.parametrize(
    ("body", "content_length", "expected_error", "expected_reads"),
    [
        (b"{", 1, "invalid JSON body", [1]),
        (b"\xff", 1, "invalid JSON body", [1]),
        (b"[]", 2, "body must be a JSON object", [2]),
        (b"{}", 3, "invalid JSON body", [3]),
        (b"{}", "invalid", "invalid Content-Length", []),
        (b"{}", -1, "invalid body length", []),
        (b"{}", wake_corpus_setup._JSON_BODY_LIMIT + 1, "invalid body length", []),
    ],
)
def test_post_rejects_invalid_json_framing_before_dispatch(
    monkeypatch,
    body,
    content_length,
    expected_error,
    expected_reads,
):
    handler, captured = _corpus_post_handler(
        monkeypatch,
        body=body,
        content_length=content_length,
    )

    handler.do_POST()

    assert captured["status"] == 400
    error = json.loads(handler.wfile.getvalue())["error"]
    if expected_error == "invalid JSON body":
        assert error.startswith(expected_error)
    else:
        assert error == expected_error
    assert handler.rfile.read_calls == expected_reads


def test_post_request_body_oserror_remains_distinct_from_client_json_errors(
    monkeypatch,
):
    handler, captured = _corpus_post_handler(
        monkeypatch,
        body=b"{}",
        content_length=2,
        read_fails=True,
    )

    with pytest.raises(OSError, match="request body read failed"):
        handler.do_POST()

    assert captured["status"] is None


def test_delete_unknown_path_404s_without_revealing_csrf_state(running_server_port: int) -> None:
    assert _mutating_status(running_server_port, "DELETE", "/api/nope") == 404


def test_delete_known_route_shape_without_token_403s(running_server_port: int) -> None:
    assert _mutating_status(running_server_port, "DELETE", "/api/clip/some-id") == 403


# ---------------------------------------------------------------------------
# /api/sessions HTTP endpoint
# ---------------------------------------------------------------------------


def test_api_sessions_returns_empty_list(backend, running_server_port: int) -> None:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/sessions")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        raw_body = resp.read()
        assert resp.getheader("Content-Type") == "application/json"
        assert resp.getheader("Content-Length") == str(len(raw_body))
        assert resp.getheader("Cache-Control") == "no-store"
        assert raw_body == b'{"sessions": []}'
        assert json.loads(raw_body) == {"sessions": []}
    finally:
        conn.close()


def test_api_session_load_round_trip(backend, running_server_port: int) -> None:
    """POST /api/session/load with a valid session_id switches the
    active session. Use the same backend's begin_session to create
    the target so we don't need a separate disk fixture."""
    import http.client

    backend.begin_session("jasper", include_raw_mic_0=True)
    first_id = backend.session_id()
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    backend.begin_session("brittany")  # second session, now active

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session/load",
        json.dumps({"session_id": first_id}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["session_id"] == first_id
        assert body["include_raw_mic_0"] is True
    finally:
        conn.close()
    # Backend's active session swapped
    assert backend.session_id() == first_id
    marker = (
        backend._output_dir  # noqa: SLF001
        / "metadata"
        / wake_corpus_setup.ACTIVE_SESSION_MARKER
    )
    assert json.loads(marker.read_text())["session_id"] == first_id


def test_api_session_unload_round_trip(backend, running_server_port: int) -> None:
    import http.client

    backend.begin_session("jasper")
    sid = backend.session_id()

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session/unload",
        json.dumps({}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["unloaded_session"] == sid
    finally:
        conn.close()
    assert backend.session_id() is None


def test_api_session_delete_round_trip(backend, running_server_port: int) -> None:
    import http.client

    backend.begin_session("jasper")
    backend.start_recording("quiet", "near")
    time.sleep(0.05)
    backend.stop_recording()
    sid = backend.session_id()

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "DELETE", f"/api/session/{sid}",
        headers={"X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["deleted_session"] == sid
    finally:
        conn.close()
    # Active state cleared
    assert backend.session_id() is None


def test_api_status_includes_include_raw_mic_0(
    backend, monkeypatch: pytest.MonkeyPatch,
    running_server_port: int,
) -> None:
    """status payload must surface include_raw_mic_0 so the UI's
    session-id label can show the raw-mic-0 marker.

    voice_daemon_active() shells out to systemctl which doesn't exist
    on dev macs — monkeypatch it so the test runs hardware-free.
    """
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    backend.begin_session("jasper", include_raw_mic_0=True)
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/status")
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert body["include_raw_mic_0"] is True
        assert body["include_dtln"] is True
        assert body["enabled_legs"] == ["on", "off", "dtln", "raw0"]
    finally:
        conn.close()


def test_api_status_includes_include_usb_mic(
    backend, monkeypatch: pytest.MonkeyPatch,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    backend.begin_session("jasper", include_usb_mic=True)
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/status")
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert body["include_usb_mic"] is True
        assert body["include_usb_dtln"] is False
        assert body["enabled_legs"] == [
            "on", "off", "dtln", "ref", "usb_raw", "usb_webrtc",
        ]
    finally:
        conn.close()


def test_api_capture_plan_previews_selected_layers(
    backend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(monkeypatch, tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/capture-plan",
        json.dumps({
            "corpus_profile": wake_corpus_setup.PROFILE_CHIP_AEC_COMPARISON,
            "include_usb_mic": True,
            "include_usb_dtln": True,
            "include_xvf_raw0_dtln": True,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        plan = body["capture_plan"]
        assert plan["recipe"] == "chip_aec_comparison_extended"
        assert set(plan["selected_physical_mics"]) == {"xvf3800", "usb_mic"}
        assert "usb_dtln" in plan["software_transforms"]["dtln"]
        assert plan["resource"]["level"] in {"high", "unsafe"}
    finally:
        conn.close()


def test_api_session_stores_applied_capture_plan(
    backend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session",
        json.dumps({
            "member": "jasper",
            "include_dtln": False,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        plan = body["capture_plan"]
        assert plan["state"] == "session"
        assert plan["plan_id"]
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in bridge_path.read_text().splitlines()
        }
        assert values["JASPER_WAKE_CORPUS_PLAN_ID"] == plan["plan_id"]
        assert values["JASPER_WAKE_CORPUS_EXPECTED_LEGS"] == "on,off"

        metadata = list((tmp_path / "out" / "metadata").glob("enroll_*.json"))
        assert len(metadata) == 1
        data = json.loads(metadata[0].read_text())
        assert data["capture_plan"]["plan_id"] == plan["plan_id"]
    finally:
        conn.close()


def test_api_status_includes_aec3_sweep(
    backend, monkeypatch: pytest.MonkeyPatch,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    backend.begin_session(
        "jasper", include_dtln=False, include_aec3_sweep=True,
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/status")
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert body["include_aec3_sweep"] is True
        assert body["include_usb_mic"] is True
        assert body["aec3_sweep_source"] == "usb"
        assert body["aec3_sweep_variants"] == wake_corpus_setup.variant_metadata(
            input_source="usb",
        )
        assert body["enabled_legs"] == [
            "on", "off", "ref", "usb_raw", "usb_webrtc",
            *wake_corpus_setup.AEC3_SWEEP_LEGS,
        ]
    finally:
        conn.close()


def test_api_session_begin_accepts_dtln_flags(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session",
        json.dumps({
            "member": "jasper",
            "include_dtln": False,
            "include_usb_dtln": True,
            "enable_bridge_outputs": True,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["include_dtln"] is False
        assert body["include_usb_dtln"] is True
        assert body["enabled_legs"] == [
            "on", "off", "ref", "usb_raw", "usb_dtln",
        ]
    finally:
        conn.close()


def test_api_session_begin_accepts_aec3_sweep(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1\n"
            "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE=usb\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=1\n"
        ),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session",
        json.dumps({
            "member": "jasper",
            "include_dtln": False,
            "include_aec3_sweep": True,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["include_aec3_sweep"] is True
        assert body["include_usb_mic"] is True
        assert body["aec3_sweep_source"] == "usb"
        assert body["enabled_legs"] == [
            "on", "off", "ref", "usb_raw", "usb_webrtc",
            *wake_corpus_setup.AEC3_SWEEP_LEGS,
        ]
    finally:
        conn.close()


def test_api_status_includes_bridge_output_status(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_REF_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_ENABLED=0\n"
        ),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/status")
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert body["bridge_outputs"] == {
            "dtln": True,
            "ref": True,
            "usb": False,
            "usb_dtln": False,
            "chip_aec": False,
            "xvf_raw0_webrtc_aec3": False,
            "xvf_raw0_dtln": False,
            "outputd_ref": False,
            "aec3_sweep": False,
            "aec3_sweep_source": "xvf",
            "env_path": str(tmp_path / "wake_corpus_bridge.env"),
            "recorder_outputs": {
                "dtln": True,
                "ref": True,
                "usb": False,
                "usb_dtln": False,
                "chip_aec": False,
                "xvf_raw0_webrtc_aec3": False,
                "xvf_raw0_dtln": False,
                "outputd_ref": False,
                "aec3_sweep": False,
                "aec3_sweep_source": "xvf",
            },
            "active": True,
        }
    finally:
        conn.close()


def test_api_bridge_outputs_disable(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_CORPUS_USB_ENABLED=1\n",
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/bridge-outputs",
        json.dumps({"action": "disable"}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["bridge_outputs"]["active"] is False
        assert restarts == ["restart"]
    finally:
        conn.close()


def test_systemd_unit_active_is_bounded_and_parses_stable_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="active\n")

    monkeypatch.setattr(runtime_probe.subprocess, "run", fake_run)

    assert runtime_probe.systemd_unit_active("example.service") is True
    assert calls == [
        (
            ["systemctl", "is-active", "example.service"],
            {"capture_output": True, "text": True, "timeout": 1.5},
        ),
    ]

    for state in ("inactive", "failed"):
        monkeypatch.setattr(
            runtime_probe.subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 3, stdout=f"{state}\n",
            ),
        )
        assert runtime_probe.systemd_unit_active("example.service") is False


@pytest.mark.parametrize(
    ("result", "expected_detail"),
    [
        (
            subprocess.CompletedProcess(
                ["systemctl"],
                1,
                stdout="",
                stderr="Failed to connect to bus: Permission denied\n",
            ),
            "Failed to connect to bus: Permission denied",
        ),
        (
            subprocess.CompletedProcess(
                ["systemctl"], 3, stdout="activating\n", stderr="",
            ),
            "state=activating",
        ),
        (
            subprocess.CompletedProcess(
                ["systemctl"], 4, stdout="unknown\n", stderr="",
            ),
            "state=unknown",
        ),
        (
            subprocess.CompletedProcess(
                ["systemctl"], 1, stdout="", stderr="",
            ),
            "state=<empty>",
        ),
    ],
)
def test_systemd_unit_active_rejects_unavailable_or_unstable_state(
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess,
    expected_detail: str,
) -> None:
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda cmd, **kwargs: result,
    )

    with pytest.raises(
        OSError,
        match=rf"systemctl is-active example.service.*{expected_detail}",
    ):
        runtime_probe.systemd_unit_active("example.service")


@pytest.mark.parametrize(
    ("wrapper_name", "unit"),
    [
        ("voice_daemon_active", bridge_session.VOICE_UNIT),
        ("aec_bridge_active", bridge_session.BRIDGE_UNIT),
    ],
)
def test_unit_active_wrappers_use_shared_probe(
    monkeypatch: pytest.MonkeyPatch,
    wrapper_name: str,
    unit: str,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        runtime_probe,
        "systemd_unit_active",
        lambda candidate: seen.append(candidate) or True,
    )

    assert getattr(bridge_session, wrapper_name)() is True
    assert seen == [unit]


@pytest.mark.parametrize(
    "probe_error",
    [
        OSError("systemctl unavailable"),
        subprocess.TimeoutExpired(["systemctl", "is-active"], 1.5),
    ],
)
@pytest.mark.parametrize(
    "wrapper_name",
    ["voice_daemon_active", "aec_bridge_active"],
)
def test_unit_active_wrappers_fail_soft_on_probe_error(
    monkeypatch: pytest.MonkeyPatch,
    wrapper_name: str,
    probe_error: BaseException,
) -> None:
    def fail_probe(unit: str) -> bool:
        raise probe_error

    monkeypatch.setattr(runtime_probe, "systemd_unit_active", fail_probe)

    assert getattr(bridge_session, wrapper_name)() is False


@pytest.mark.parametrize(
    ("probe_error", "expected_detail"),
    [
        (OSError("systemctl unavailable"), "systemctl unavailable"),
        (
            subprocess.TimeoutExpired(["systemctl", "is-active"], 1.5),
            "systemctl probe timed out after 1.5s",
        ),
    ],
)
def test_enter_corpus_test_mode_aborts_before_mutation_when_voice_unknown(
    monkeypatch: pytest.MonkeyPatch,
    probe_error: BaseException,
    expected_detail: str,
) -> None:
    mutations: list[str] = []

    def fail_probe(unit: str) -> bool:
        assert unit == bridge_session.VOICE_UNIT
        raise probe_error

    monkeypatch.setattr(runtime_probe, "systemd_unit_active", fail_probe)
    monkeypatch.setattr(
        bridge_session,
        "set_voice_daemon_state",
        lambda action: mutations.append(f"voice:{action}"),
    )
    monkeypatch.setattr(
        bridge_session,
        "set_bridge_outputs_for_session",
        lambda **kwargs: mutations.append("bridge"),
    )

    with pytest.raises(
        OSError,
        match=(
            rf"could not determine whether {bridge_session.VOICE_UNIT} "
            rf"is active: {expected_detail}"
        ),
    ):
        bridge_session.enter_corpus_test_mode(
            include_dtln=False,
            include_usb_mic=False,
            include_usb_dtln=False,
        )

    assert mutations == []


def test_api_corpus_test_mode_enter_reports_voice_probe_timeout_without_mutation(
    backend,
    monkeypatch: pytest.MonkeyPatch,
    running_server_port: int,
) -> None:
    import http.client

    mutations: list[str] = []

    def timeout_probe(unit: str) -> bool:
        assert unit == bridge_session.VOICE_UNIT
        raise subprocess.TimeoutExpired(["systemctl", "is-active", unit], 1.5)

    monkeypatch.setattr(runtime_probe, "systemd_unit_active", timeout_probe)
    monkeypatch.setattr(
        bridge_session,
        "set_voice_daemon_state",
        lambda action: mutations.append(f"voice:{action}"),
    )
    monkeypatch.setattr(
        bridge_session,
        "set_bridge_outputs_for_session",
        lambda **kwargs: mutations.append("bridge"),
    )

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST",
        "/api/corpus-test-mode",
        json.dumps({"action": "enter", "include_dtln": False}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 500
        assert body == {
            "error": (
                "corpus test mode enter failed: could not determine whether "
                "jasper-voice is active: systemctl probe timed out "
                "after 1.5s"
            ),
        }
    finally:
        conn.close()

    assert mutations == []


def test_api_corpus_test_mode_enter_rejects_manager_error_without_mutation(
    backend,
    monkeypatch: pytest.MonkeyPatch,
    running_server_port: int,
) -> None:
    import http.client

    mutations: list[str] = []
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="Failed to connect to bus: Permission denied\n",
        ),
    )
    monkeypatch.setattr(
        bridge_session,
        "set_voice_daemon_state",
        lambda action: mutations.append(f"voice:{action}"),
    )
    monkeypatch.setattr(
        bridge_session,
        "set_bridge_outputs_for_session",
        lambda **kwargs: mutations.append("bridge"),
    )

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST",
        "/api/corpus-test-mode",
        json.dumps({"action": "enter", "include_dtln": False}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 500
        assert body == {
            "error": (
                "corpus test mode enter failed: could not determine whether "
                "jasper-voice is active: systemctl is-active jasper-voice "
                "returned rc=1: Failed to connect to bus: Permission denied"
            ),
        }
    finally:
        conn.close()

    assert mutations == []


def test_api_corpus_test_mode_enter_stops_voice_and_sets_outputs(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env=(
            "JASPER_AEC_DTLN_ENABLED=1\n"
            "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1\n"
        ),
    )
    voice_active = {"value": True}
    voice_actions: list[str] = []
    restarts: list[str] = []
    monkeypatch.setattr(
        runtime_probe,
        "systemd_unit_active",
        lambda unit: voice_active["value"],
    )

    def fake_voice(action: str) -> None:
        voice_actions.append(action)
        voice_active["value"] = action == "start"

    monkeypatch.setattr(bridge_session, "set_voice_daemon_state", fake_voice)
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/corpus-test-mode",
        json.dumps({
            "action": "enter",
            "include_dtln": False,
            "include_usb_mic": True,
            "include_usb_dtln": False,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["voice_daemon_active"] is False
        assert voice_actions == ["stop"]
        assert restarts == ["restart"]
        text = bridge_path.read_text()
        assert "JASPER_AEC_CORPUS_REF_ENABLED=1" in text
        assert "JASPER_AEC_CORPUS_USB_ENABLED=1" in text
        assert "JASPER_AEC_DTLN_ENABLED" not in text
        assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED" not in text
    finally:
        conn.close()


def test_api_corpus_test_mode_enter_can_enable_aec3_sweep(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    _, bridge_path = _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        system_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    voice_active = {"value": True}
    monkeypatch.setattr(
        runtime_probe,
        "systemd_unit_active",
        lambda unit: voice_active["value"],
    )
    monkeypatch.setattr(
        bridge_session,
        "set_voice_daemon_state",
        lambda action: voice_active.__setitem__("value", action == "start"),
    )
    monkeypatch.setattr(bridge_session, "restart_aec_bridge", lambda: None)

    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/corpus-test-mode",
        json.dumps({
            "action": "enter",
            "include_dtln": False,
            "include_aec3_sweep": True,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["voice_daemon_active"] is False
        text = bridge_path.read_text()
        assert "JASPER_AEC_DTLN_ENABLED=0" in text
        assert "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED=1" in text
    finally:
        conn.close()


def test_api_corpus_test_mode_exit_disables_outputs_and_starts_voice(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    backend.begin_session("jasper", include_usb_mic=True)
    sid = backend.session_id()
    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_CORPUS_USB_ENABLED=1\n",
    )
    voice_active = {"value": False}
    voice_actions: list[str] = []
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "voice_daemon_active",
        lambda: voice_active["value"],
    )

    def fake_voice(action: str) -> None:
        voice_actions.append(action)
        voice_active["value"] = action == "start"

    monkeypatch.setattr(bridge_session, "set_voice_daemon_state", fake_voice)
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/corpus-test-mode",
        json.dumps({"action": "exit"}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["voice_daemon_active"] is True
        assert body["bridge_outputs"]["active"] is False
        assert body["action"] == "exit"
        assert voice_actions == ["start"]
        assert restarts == ["restart"]
    finally:
        conn.close()
    assert backend.session_id() is None
    assert sid is not None


def test_voice_start_can_disable_bridge_outputs_first(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    _use_tmp_bridge_env(
        monkeypatch,
        tmp_path,
        corpus_env="JASPER_AEC_DTLN_ENABLED=1\n",
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    # WS1 Phase 3: the handler starts jasper-voice via the restart broker
    # (manage_units), not a direct systemctl. The is-active readback
    # (voice_daemon_active) stays a direct read-only systemctl probe.
    voice_calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_manage(*units, **kwargs):
        voice_calls.append((units, kwargs.get("verb")))
        return {"ok": True}

    monkeypatch.setattr(wake_corpus_setup, "manage_units", fake_manage)
    monkeypatch.setattr(
        wake_corpus_setup.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="active\n"),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/voice-daemon",
        json.dumps({"action": "start", "disable_bridge_outputs": True}),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["bridge_outputs"]["active"] is False
        assert restarts == ["restart"]
        assert voice_calls == [((wake_corpus_setup.VOICE_UNIT,), "start")]
    finally:
        conn.close()


def test_api_usb_mic_status_endpoint(
    backend, monkeypatch: pytest.MonkeyPatch,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session,
        "usb_mic_status",
        lambda: {
            "device": "USB PnP Sound Device",
            "hardware_agc": {
                "control": "Auto Gain Control",
                "mixer_card": "4",
                "available": True,
                "enabled": True,
            },
        },
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request("GET", "/api/usb-mic/status")
    resp = conn.getresponse()
    try:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["hardware_agc"]["enabled"] is True
        assert body["hardware_agc"]["control"] == "Auto Gain Control"
    finally:
        conn.close()


def test_api_session_offers_bridge_enable_for_missing_outputs(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _use_tmp_bridge_env(monkeypatch, tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session",
        json.dumps({
            "member": "jasper",
            "include_dtln": True,
            "include_usb_mic": True,
            "include_usb_dtln": True,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 409
        assert body["can_enable_bridge_outputs"] is True
        assert body["missing_bridge_outputs"] == [
            "dtln", "ref", "usb", "usb_dtln",
        ]
        assert backend.session_id() is None
    finally:
        conn.close()


def test_api_session_enable_bridge_outputs_then_begins(
    backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    running_server_port: int,
) -> None:
    import http.client

    monkeypatch.setattr(
        bridge_session, "voice_daemon_active", lambda: False,
    )
    _, bridge_path = _use_tmp_bridge_env(monkeypatch, tmp_path)
    restarts: list[str] = []
    monkeypatch.setattr(
        bridge_session,
        "restart_aec_bridge",
        lambda: restarts.append("restart"),
    )
    conn = http.client.HTTPConnection("127.0.0.1", running_server_port, timeout=2)
    conn.request(
        "POST", "/api/session",
        json.dumps({
            "member": "jasper",
            "include_dtln": True,
            "include_usb_mic": True,
            "include_usb_dtln": True,
            "enable_bridge_outputs": True,
        }),
        {"Content-Type": "application/json", "X-CSRF-Token": "test-token"},
    )
    resp = conn.getresponse()
    try:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["member"] == "jasper"
        assert body["enabled_legs"] == [
            "on", "off", "dtln", "ref", "usb_raw",
            "usb_webrtc", "usb_dtln",
        ]
        assert restarts == ["restart"]
        text = bridge_path.read_text()
        assert "JASPER_AEC_CORPUS_USB_ENABLED=1" in text
        assert "JASPER_AEC_CORPUS_USB_DTLN_ENABLED=1" in text
    finally:
        conn.close()
