# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.voice.peering_client.PeeringClient.

Covers:
  - `arbitrate` short-circuits to WIN when peering is disabled (zero
    observable cost on single-Pi installs).
  - `arbitrate` propagates WIN/LOSE from the peering UDS correctly
    when enabled, and every error path (no daemon, connection refused,
    timeout, malformed response) falls back to WIN — the load-bearing
    fail-open guarantee that prevents a broken peering daemon from
    silencing the speaker.
  - `session_started` / `session_ended` fire-and-forget notices, their
    no-op paths, and that errors are swallowed.
  - the client's RPC read budget outlasts the daemon's own fail-open
    ARBITRATE timeout (#4332), including against a real, slow daemon.

Full wake-handler integration (`WakeLoop._arbitrate_acquire_drain`) is
covered by tests/test_voice_daemon_peering.py and by the existing
voice-daemon-on-Pi smoke tests; it depends on real openWakeWord + real
audio I/O which can't run on CI.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from jasper.peering import uds as uds_mod
from jasper.peering.config import ARBITRATE_RPC_TIMEOUT_SEC
from jasper.voice.peering_client import DEFAULT_RPC_TIMEOUT_SEC, PeeringClient
from tests._socket_paths import short_unix_socket_path as _short_socket_path

_SOCKET = "/tmp/jasper-peering-test.sock"


# ---------- arbitrate ----------


async def test_arbitrate_disabled_returns_win_without_io():
    """When peering is off (default), arbitrate is a no-op that
    returns WIN and never touches the peering UDS."""
    client = PeeringClient(enabled=False, socket_path=_SOCKET)
    mock = AsyncMock()
    with patch("jasper.peering.uds.send_request", new=mock):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"
    assert client._epoch == ""
    mock.assert_not_called()


@pytest.mark.parametrize(
    "return_value, side_effect, expected_result, expected_epoch", [
        pytest.param({"result": "WIN", "epoch": "ep-123"}, None,
                     "WIN", "ep-123", id="win"),
        pytest.param({"result": "LOSE", "epoch": "ep-456"}, None,
                     "LOSE", "ep-456", id="lose"),
        pytest.param(None, FileNotFoundError, "WIN", "", id="no_daemon"),
        pytest.param(None, asyncio.TimeoutError, "WIN", "", id="timeout"),
        pytest.param(None, OSError("connection refused"),
                     "WIN", "", id="oserror"),
        pytest.param({"result": "MAYBE", "epoch": "ep"}, None,
                     "WIN", "ep", id="garbage_result"),
        pytest.param({}, None, "WIN", "", id="empty_response"),
    ],
)
async def test_arbitrate_enabled_outcomes(
    return_value, side_effect, expected_result, expected_epoch,
):
    """Every UDS outcome — a clean WIN/LOSE reply, a missing daemon, a
    wedged/refused socket, or a malformed reply — resolves to a
    decision and the `_epoch` used to correlate session notices."""
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    mock = AsyncMock(return_value=return_value, side_effect=side_effect)
    with patch("jasper.peering.uds.send_request", new=mock):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == expected_result
    assert client._epoch == expected_epoch


# ---------- session lifecycle notifications ----------


@pytest.mark.parametrize("enabled, has_turn", [
    pytest.param(False, True, id="peering_disabled"),
    pytest.param(True, False, id="no_active_turn"),
])
async def test_session_started_noop(enabled, has_turn):
    """session_started sends nothing when peering is disabled or
    there's no active turn to announce — no UDS connect attempt."""
    client = PeeringClient(enabled=enabled, socket_path=_SOCKET)
    mock = AsyncMock()
    with patch("jasper.peering.uds.send_request", new=mock):
        await client.session_started(has_turn=has_turn)
    mock.assert_not_called()


@pytest.mark.parametrize("method, call_args, epoch, expected_cmd", [
    pytest.param("session_started", (True,), "ep-abc",
                 "SESSION_STARTED ep-abc", id="session_started"),
    pytest.param("session_ended", ("user_silence",), "ep-xyz",
                 "SESSION_ENDED ep-xyz user_silence", id="session_ended"),
])
async def test_session_notice_sends_command(
    method, call_args, epoch, expected_cmd,
):
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    client._epoch = epoch
    mock = AsyncMock(return_value={"result": "ok"})
    with patch("jasper.peering.uds.send_request", new=mock):
        await getattr(client, method)(*call_args)
    sent_args, _ = mock.call_args
    assert sent_args[1] == expected_cmd


async def test_session_ended_swallows_errors():
    """Peering notifications are best-effort; errors must not propagate
    into the voice daemon's _end_turn path."""
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    client._epoch = "ep-abc"
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=OSError("broken pipe")),
    ):
        await client.session_ended("error")  # no raise


# ---------- RPC read budget vs the daemon's fail-open timeout (#4332) ----------


def test_default_rpc_timeout_outlasts_daemon_fail_open():
    """The client's read budget must strictly exceed the daemon's own
    fail-open ARBITRATE timeout. Otherwise `_send`'s readline can
    expire while the daemon is still arbitrating, and the daemon's
    real reply — including its own fail-open StartSession/StandDown —
    is mistaken for silence, so every wake resolves WIN regardless of
    the actual decision."""
    assert DEFAULT_RPC_TIMEOUT_SEC > ARBITRATE_RPC_TIMEOUT_SEC


@pytest_asyncio.fixture
async def delayed_lose_server():
    """A real peering UDS server whose ARBITRATE handler doesn't reply
    until ARBITRATE_RPC_TIMEOUT_SEC has elapsed, then returns LOSE."""
    sock_path = _short_socket_path()

    async def arbitrate(_req: dict) -> dict:
        await asyncio.sleep(ARBITRATE_RPC_TIMEOUT_SEC)
        return {"result": "LOSE", "epoch": "ep-late"}

    async def notify_started(_epoch: str) -> None:
        pass

    async def notify_ended(_epoch: str, _reason: str) -> None:
        pass

    server = await uds_mod.serve(
        path=sock_path,
        arbitrate=arbitrate,
        notify_session_started=notify_started,
        notify_session_ended=notify_ended,
    )
    try:
        yield sock_path
    finally:
        server.close()
        await server.wait_closed()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


async def test_arbitrate_waits_out_a_slow_real_daemon(delayed_lose_server):
    """Against a real UDS server that takes until ARBITRATE_RPC_TIMEOUT_SEC
    to reply, the client must still receive the real LOSE rather than
    timing out first and failing open to WIN. With the old hardcoded
    0.5 s client budget this would have timed out client-side and
    returned WIN — silently defeating peering."""
    client = PeeringClient(enabled=True, socket_path=delayed_lose_server)
    result = await client.arbitrate(
        score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
    )
    assert result == "LOSE"
