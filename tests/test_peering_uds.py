# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for jasper.peering.uds.

Spins up a real Unix-domain-socket server, talks to it via the
send_request client, asserts on the wire-level responses. Same shape
as tests/test_control_server.py (real server + monkey-patched
callbacks).
"""
from __future__ import annotations

import os
import secrets

import pytest
import pytest_asyncio

from jasper.peering import uds as uds_mod


# UDS sun_path is capped at 108 bytes on Linux, 104 on macOS. pytest's
# tmp_path on macOS is /private/var/folders/... — usually too long.
# A short /tmp path with a hex suffix dodges that.
def _short_socket_path() -> str:
    return f"/tmp/jts-pt-{secrets.token_hex(4)}.sock"


@pytest_asyncio.fixture
async def server_setup():
    """Start the UDS server with stub callbacks; capture invocations."""
    started_epochs: list[str] = []
    ended_calls: list[tuple[str, str]] = []
    arbitrate_calls: list[dict] = []
    arbitrate_response = {"result": "WIN", "epoch": "test-epoch"}

    async def arbitrate(req: dict) -> dict:
        arbitrate_calls.append(req)
        return arbitrate_response

    async def notify_started(epoch: str) -> None:
        started_epochs.append(epoch)

    async def notify_ended(epoch: str, reason: str) -> None:
        ended_calls.append((epoch, reason))

    async def status() -> dict:
        return {"state": "idle", "peers": [], "mode": "on"}

    sock_path = _short_socket_path()
    server = await uds_mod.serve(
        path=sock_path,
        arbitrate=arbitrate,
        notify_session_started=notify_started,
        notify_session_ended=notify_ended,
        status=status,
    )
    try:
        yield {
            "server": server,
            "path": sock_path,
            "arbitrate_calls": arbitrate_calls,
            "started_epochs": started_epochs,
            "ended_calls": ended_calls,
        }
    finally:
        server.close()
        await server.wait_closed()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


# ---------- Round-trip each command ----------


async def test_arbitrate_round_trip(server_setup):
    resp = await uds_mod.send_request(
        server_setup["path"],
        'ARBITRATE {"score":0.87,"snr_db":18.5,"rms_dbfs":-22.3,"can_serve":true}',
    )
    assert resp["result"] == "WIN"
    assert resp["epoch"] == "test-epoch"
    # Callback received the parsed dict.
    assert len(server_setup["arbitrate_calls"]) == 1
    call = server_setup["arbitrate_calls"][0]
    assert call["score"] == 0.87
    assert call["snr_db"] == 18.5
    assert call["can_serve"] is True


async def test_session_started_round_trip(server_setup):
    resp = await uds_mod.send_request(
        server_setup["path"], "SESSION_STARTED ep-42",
    )
    assert resp["result"] == "ok"
    assert server_setup["started_epochs"] == ["ep-42"]


async def test_session_ended_round_trip(server_setup):
    resp = await uds_mod.send_request(
        server_setup["path"], "SESSION_ENDED ep-42 silence",
    )
    assert resp["result"] == "ok"
    assert server_setup["ended_calls"] == [("ep-42", "silence")]


async def test_session_ended_no_reason(server_setup):
    """A missing reason should still succeed — caller's session might
    end for an unannounced cause."""
    resp = await uds_mod.send_request(
        server_setup["path"], "SESSION_ENDED ep-42",
    )
    assert resp["result"] == "ok"
    assert server_setup["ended_calls"] == [("ep-42", "")]


async def test_status_round_trip(server_setup):
    resp = await uds_mod.send_request(server_setup["path"], "STATUS")
    assert resp["result"] == "ok"
    assert resp["state"] == "idle"
    assert resp["mode"] == "on"


async def test_ping_round_trip(server_setup):
    resp = await uds_mod.send_request(server_setup["path"], "PING")
    assert resp["result"] == "pong"


# ---------- Error handling ----------


async def test_unknown_command(server_setup):
    resp = await uds_mod.send_request(server_setup["path"], "BANANAS")
    assert resp["result"] == "ERROR"
    assert "unknown command" in resp["error"]


async def test_arbitrate_with_garbage_arg(server_setup):
    """Malformed JSON in ARBITRATE shouldn't crash the daemon —
    return an error response so the caller can fall back."""
    resp = await uds_mod.send_request(
        server_setup["path"], "ARBITRATE this is not json",
    )
    assert resp["result"] == "ERROR"
    # And the arbitrate callback should NOT have been invoked.
    assert server_setup["arbitrate_calls"] == []


async def test_arbitrate_with_array_arg(server_setup):
    """A JSON array (not object) for ARBITRATE is rejected."""
    resp = await uds_mod.send_request(
        server_setup["path"], 'ARBITRATE ["not", "an", "object"]',
    )
    assert resp["result"] == "ERROR"


async def test_session_started_missing_epoch(server_setup):
    resp = await uds_mod.send_request(server_setup["path"], "SESSION_STARTED")
    assert resp["result"] == "ERROR"


async def test_socket_chmod_is_0660(server_setup):
    """Match the voice_daemon UDS pattern (root:root, group rw).
    A wider mode would be a security regression."""
    mode = os.stat(server_setup["path"]).st_mode & 0o777
    assert mode == 0o660


async def test_send_request_no_daemon():
    """Client should raise FileNotFoundError when the daemon isn't
    running — caller (voice) catches this and falls through to
    WIN (peering not available = act as if alone)."""
    with pytest.raises(FileNotFoundError):
        await uds_mod.send_request("/nonexistent/path.sock", "PING")
