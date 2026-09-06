# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`jasper.voice.control_socket.serve` — the six commands that had no pin
against the real socket before this file (START, END, STATUS, CUE_PLAY,
MUTE, UNMUTE), plus the unknown-command and handler-exception wire shapes
and the 0660 socket mode. MEASURE_PAUSE/MEASURE_RESUME are already pinned
against the real socket in test_voice_daemon_measurement_inflight.py.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile

from jasper.control.uds import _voice_socket_command
from jasper.voice.control_socket import serve
from jasper.voice_daemon import WakeLoop

from .test_voice_daemon_manual_start_guard import _SpyCues


def _wake_loop() -> tuple[WakeLoop, _SpyCues]:
    wl = WakeLoop.for_tests()
    spy = _SpyCues()
    wl._cues = spy
    return wl, spy


@contextlib.asynccontextmanager
async def _running_socket(wake_loop: WakeLoop):
    """Serve `wake_loop` on a short tmp path (kept out of pytest's deeper
    `tmp_path` tree — AF_UNIX paths are capped at ~108 bytes)."""
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-voice-control-socket-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await serve(wake_loop, socket_path)
    try:
        yield socket_path
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_start_returns_the_loop_result_for_an_unrecognized_source() -> None:
    wl, _spy = _wake_loop()
    async with _running_socket(wl) as socket_path:
        response = await _voice_socket_command(socket_path, "START badsource")
    assert response == {"result": "UNKNOWN_SOURCE"}


async def test_end_returns_the_loop_result_with_no_session_open() -> None:
    wl, _spy = _wake_loop()
    async with _running_socket(wl) as socket_path:
        response = await _voice_socket_command(socket_path, "END")
    assert response == {"result": "NO_SESSION"}


async def test_status_returns_session_status_unwrapped() -> None:
    wl, _spy = _wake_loop()
    async with _running_socket(wl) as socket_path:
        response = await _voice_socket_command(socket_path, "STATUS")
    assert "result" not in response
    assert response == wl.session_status()


async def test_cue_play_reaches_the_configured_cue_manager() -> None:
    wl, spy = _wake_loop()
    async with _running_socket(wl) as socket_path:
        response = await _voice_socket_command(
            socket_path, "CUE_PLAY cant_connect",
        )
    assert response == {"result": "ok"}
    assert spy.played == ["cant_connect"]


async def test_mute_and_unmute_are_reflected_in_status(tmp_path) -> None:
    wl, _spy = _wake_loop()
    wl._cfg.mic_mute_state_path = str(tmp_path / "mic_mute.env")

    async def _noop_click(going_on: bool) -> None:
        return None

    wl._play_mute_click = _noop_click
    async with _running_socket(wl) as socket_path:
        mute_response = await _voice_socket_command(socket_path, "MUTE")
        muted_status = await _voice_socket_command(socket_path, "STATUS")
        unmute_response = await _voice_socket_command(socket_path, "UNMUTE")
        unmuted_status = await _voice_socket_command(socket_path, "STATUS")
    assert mute_response == {"result": "ok"}
    assert muted_status["mic_muted"] is True
    assert unmute_response == {"result": "ok"}
    assert unmuted_status["mic_muted"] is False


async def test_unknown_command_names_itself_in_the_reply() -> None:
    wl, _spy = _wake_loop()
    async with _running_socket(wl) as socket_path:
        response = await _voice_socket_command(socket_path, "BOGUS")
    assert response == {"result": "UNKNOWN", "command": "BOGUS"}


async def test_a_handler_exception_returns_the_bare_error_shape() -> None:
    wl, _spy = _wake_loop()

    def _boom() -> dict:
        raise RuntimeError("boom")

    wl.session_status = _boom
    async with _running_socket(wl) as socket_path:
        response = await _voice_socket_command(socket_path, "STATUS")
    assert response == {"result": "ERROR"}


async def test_socket_is_created_with_mode_0660() -> None:
    wl, _spy = _wake_loop()
    async with _running_socket(wl) as socket_path:
        mode = os.stat(socket_path).st_mode & 0o777
    assert mode == 0o660
