# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.voice.peering_client.PeeringClient.

Covers:
  - `arbitrate` short-circuits to WIN when peering is disabled (zero
    observable cost on single-Pi installs).
  - `arbitrate` propagates WIN/LOSE from the peering UDS correctly
    when enabled.
  - All error paths (no daemon, connection refused, timeout,
    malformed response, peering import failure) fall back to WIN —
    the load-bearing fail-open guarantee that prevents a broken
    peering daemon from silencing the speaker.
  - `session_started` / `session_ended` fire-and-forget notices.

Full wake-handler integration (`WakeLoop._arbitrate_acquire_drain`) is
covered by tests/test_voice_daemon_peering.py and by the existing
voice-daemon-on-Pi smoke tests; it depends on real openWakeWord + real
audio I/O which can't run on CI.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from jasper.voice.peering_client import PeeringClient

_SOCKET = "/tmp/jasper-peering-test.sock"


# ---------- arbitrate ----------


async def test_arbitrate_disabled_returns_win_without_io():
    """When peering is off (default), arbitrate is a no-op that
    returns WIN. send_request must not be called — verified by
    patching it to raise loudly if called."""
    client = PeeringClient(enabled=False, socket_path=_SOCKET)
    with patch("jasper.peering.uds.send_request",
               side_effect=AssertionError("send_request must not be called")):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"
    assert client._epoch == ""


async def test_arbitrate_enabled_win_response_propagates():
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={"result": "WIN", "epoch": "ep-123"}),
    ):
        result = await client.arbitrate(
            score=0.8, snr_db=18.0, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"
    assert client._epoch == "ep-123"


async def test_arbitrate_enabled_lose_response_propagates():
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={"result": "LOSE", "epoch": "ep-456"}),
    ):
        result = await client.arbitrate(
            score=0.5, snr_db=10.0, rms_dbfs=-25.0, can_serve=True,
        )
    assert result == "LOSE"
    assert client._epoch == "ep-456"


async def test_arbitrate_file_not_found_falls_back_to_win():
    """Peering enabled in voice config but jasper-control isn't running
    its peering daemon — UDS doesn't exist. Voice falls back to solo
    behavior rather than silencing the speaker."""
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=FileNotFoundError),
    ):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_arbitrate_timeout_falls_back_to_win():
    """Peering daemon is slow or wedged — fail open. The user gets a
    response (maybe duplicate with another peer), which beats silence."""
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_arbitrate_oserror_falls_back_to_win():
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=OSError("connection refused")),
    ):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_arbitrate_garbage_response_falls_back_to_win():
    """A peering daemon bug returning something other than WIN/LOSE
    shouldn't lock up the wake path — default to WIN."""
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={"result": "MAYBE", "epoch": "ep"}),
    ):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_arbitrate_empty_response_falls_back_to_win():
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={}),
    ):
        result = await client.arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


# ---------- session lifecycle notifications ----------


async def test_session_started_no_turn_is_noop():
    """When there's no active turn to announce, the notification is a
    fast no-op (no UDS connect attempt)."""
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    with patch("jasper.peering.uds.send_request",
               side_effect=AssertionError("should not call")):
        await client.session_started(has_turn=False)  # no raise


async def test_session_started_sends_command():
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    client._epoch = "ep-abc"
    mock = AsyncMock(return_value={"result": "ok"})
    with patch("jasper.peering.uds.send_request", new=mock):
        await client.session_started(has_turn=True)
    # send_request called with SESSION_STARTED <epoch>
    args, kwargs = mock.call_args
    assert args[1] == "SESSION_STARTED ep-abc"


async def test_session_ended_sends_reason():
    client = PeeringClient(enabled=True, socket_path=_SOCKET)
    client._epoch = "ep-xyz"
    mock = AsyncMock(return_value={"result": "ok"})
    with patch("jasper.peering.uds.send_request", new=mock):
        await client.session_ended("user_silence")
    args, kwargs = mock.call_args
    assert args[1] == "SESSION_ENDED ep-xyz user_silence"


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
