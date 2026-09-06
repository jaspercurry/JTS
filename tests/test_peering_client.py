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

Full wake-handler integration (`WakeLoop._arbitrate_acquire_drain`) is
covered by tests/test_voice_daemon_peering.py and by the existing
voice-daemon-on-Pi smoke tests; it depends on real openWakeWord + real
audio I/O which can't run on CI.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from jasper.voice.peering_client import PeeringClient

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
