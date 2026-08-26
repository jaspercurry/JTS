# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Route tests for ``jasper.control.handlers.voice``.

/mic, /session/*, /cue/play — the voice-daemon UDS proxy routes.
"""

from __future__ import annotations


from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _get,
    _isolate_household_secret,
    _post,
    server_with_coordinator,
    server_with_voice_socket,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
    server_with_voice_socket,
)


# --- /session/* endpoints (phase 3) ---


def test_session_start_proxies_to_voice_socket(server_with_voice_socket):
    base, voice_responses, received = server_with_voice_socket
    voice_responses.append({"result": "OK"})
    status, body = _post(f"{base}/session/start", None)
    assert status == 200
    assert body["result"] == "OK"
    assert received == ["START"]


def test_session_start_source_proxies_to_voice_socket(server_with_voice_socket):
    base, voice_responses, received = server_with_voice_socket
    voice_responses.append({"result": "OK"})
    status, body = _post(
        f"{base}/session/start",
        {"source": "wiim_remote_2"},
    )
    assert status == 200
    assert body["result"] == "OK"
    assert received == ["START wiim_remote_2"]


def test_session_start_rejects_invalid_source_token(server_with_voice_socket):
    base, _voice_responses, received = server_with_voice_socket
    status, body = _post(
        f"{base}/session/start",
        {"source": "wiim remote 2"},
    )
    assert status == 400
    assert "source" in body["error"]
    assert received == []


def test_session_start_unknown_source_400(server_with_voice_socket):
    base, voice_responses, _received = server_with_voice_socket
    voice_responses.append({"result": "UNKNOWN_SOURCE"})
    status, body = _post(
        f"{base}/session/start",
        {"source": "missing_remote"},
    )
    assert status == 400
    assert body["result"] == "UNKNOWN_SOURCE"


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


def test_cue_play_busy_409(server_with_voice_socket):
    base, voice_responses, received = server_with_voice_socket
    voice_responses.append({"result": "busy"})

    status, body = _post(f"{base}/cue/play", {"slug": "cant_connect"})

    assert status == 409
    assert body["result"] == "busy"
    assert received == ["CUE_PLAY cant_connect"]


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


def test_session_end_already_ended_is_idempotent_200(server_with_voice_socket):
    base, voice_responses, _ = server_with_voice_socket
    voice_responses.append({"result": "ALREADY_ENDED"})
    status, body = _post(f"{base}/session/end", None)
    assert status == 200
    assert body["result"] == "ALREADY_ENDED"


def test_session_endpoint_503_when_voice_socket_missing(server_with_coordinator):
    base, _ = server_with_coordinator
    # Fixture passes /nonexistent.sock — connect will FileNotFoundError.
    status, body = _post(f"{base}/session/start", None)
    assert status == 503
    assert "voice_daemon" in body["error"]


def test_get_mic_reports_voice_starting_when_socket_missing(
    monkeypatch, server_with_coordinator,
):
    """A restart/provider switch can remove the UDS socket before voice is ready.
    While systemd says jasper-voice is activating, /mic reports a temporary
    starting state instead of the permanent-offline 503 shape."""
    import jasper.control.server as srv_mod

    async def missing_socket(_socket_path, _cmd, **_kwargs):
        raise FileNotFoundError(_socket_path)

    monkeypatch.setattr(srv_mod, "_voice_socket_command", missing_socket)
    monkeypatch.setattr(
        srv_mod,
        "_voice_starting_mic_payload",
        lambda: {
            "status": "starting",
            "reason": "voice_daemon_starting",
            "available": False,
            "muted": True,
            "message": "Voice control is restarting",
        },
    )

    base, _fake = server_with_coordinator
    status, body = _get(f"{base}/mic")

    assert status == 200
    assert body["status"] == "starting"
    assert body["available"] is False
    assert body["muted"] is True
    assert "restarting" in body["message"]


def test_get_mic_reports_offline_when_socket_missing_and_unit_not_starting(
    monkeypatch, server_with_coordinator,
):
    import jasper.control.server as srv_mod

    async def missing_socket(_socket_path, _cmd, **_kwargs):
        raise FileNotFoundError(_socket_path)

    monkeypatch.setattr(srv_mod, "_voice_socket_command", missing_socket)
    monkeypatch.setattr(srv_mod, "_voice_starting_mic_payload", lambda: None)

    base, _fake = server_with_coordinator
    status, body = _get(f"{base}/mic")

    assert status == 503
    assert body["status"] == "offline"
    assert body["available"] is False
    assert body["reason"] == "voice_daemon_unreachable"


def test_voice_starting_mic_payload_reads_transient_systemd_state():
    import subprocess as sp
    import jasper.control.server as srv_mod

    def fake_run(argv, **_kw):
        return sp.CompletedProcess(
            argv,
            0,
            stdout=(
                "LoadState=loaded\n"
                "ActiveState=activating\n"
                "SubState=start-post\n"
                "Result=success\n"
            ),
            stderr="",
        )

    payload = srv_mod._voice_starting_mic_payload(
        read_unit=lambda unit: srv_mod._systemd_show_unit(unit, run=fake_run),
    )

    assert payload is not None
    assert payload["status"] == "starting"
    assert payload["available"] is False
    assert payload["unit"]["active_state"] == "activating"
    assert payload["unit"]["sub_state"] == "start-post"


def test_voice_starting_mic_payload_ignores_failed_systemd_state():
    import jasper.control.server as srv_mod

    assert srv_mod._voice_starting_mic_payload(
        read_unit=lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "failed",
            "SubState": "failed",
        },
    ) is None
