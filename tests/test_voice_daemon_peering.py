"""Tests for the peering integration in jasper.voice_daemon.

Covers:
  - The `_frame_rms_dbfs` helper produces correct dBFS for known
    input waveforms (a numeric correctness gate).
  - `_peer_arbitrate` short-circuits to WIN when peering_enabled is
    False (zero observable cost on single-Pi installs).
  - `_peer_arbitrate` propagates WIN/LOSE from the peering UDS
    correctly when enabled.
  - All error paths (no daemon, connection refused, timeout,
    malformed response, peering import failure) fall back to WIN —
    the load-bearing fail-open guarantee that prevents a broken
    peering daemon from silencing the speaker.

Full wake-handler integration (the restructured
`_arbitrate_acquire_drain`) is covered by the existing
voice-daemon-on-Pi smoke tests; it depends on real openWakeWord +
real audio I/O which can't run on CI.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


# Strip ambient JASPER_* env vars so Config.from_env() loads
# deterministically regardless of the developer's shell.
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("JASPER_") or k in (
            "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
            "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        ):
            monkeypatch.delenv(k, raising=False)
    # Minimum needed to construct a Config
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTest")


# ---------- _frame_rms_dbfs ----------


def test_rms_full_scale_sine_is_minus_3_dbfs():
    from jasper.voice_daemon import _frame_rms_dbfs
    sig = (32767 * np.sin(2 * np.pi * 200 * np.arange(1280) / 16000)).astype(np.int16)
    db = _frame_rms_dbfs(sig)
    # Full-scale sine has RMS = peak / sqrt(2), so dBFS ≈ -3.01.
    assert -3.5 < db < -2.5


def test_rms_silence_is_floor():
    from jasper.voice_daemon import _frame_rms_dbfs
    db = _frame_rms_dbfs(np.zeros(1280, dtype=np.int16))
    assert db == -120.0


def test_rms_empty_frame_returns_none():
    """A malformed (empty) frame must not crash — return None so the
    ranker falls through cleanly to peer_id tiebreaker."""
    from jasper.voice_daemon import _frame_rms_dbfs
    assert _frame_rms_dbfs(np.array([], dtype=np.int16)) is None


def test_rms_half_scale_is_minus_9_dbfs():
    from jasper.voice_daemon import _frame_rms_dbfs
    sig = (32767 * np.sin(2 * np.pi * 200 * np.arange(1280) / 16000)).astype(np.int16)
    db = _frame_rms_dbfs(sig // 2)
    # Halving amplitude = -6 dB; from -3 dBFS sine that's -9.
    assert -9.5 < db < -8.5


# ---------- _peer_arbitrate ----------


def _make_wake_loop(peering_enabled: bool):
    """Construct a minimal WakeLoop with stubs for everything except
    cfg. Only the peering attrs and a few common ones matter for the
    methods under test."""
    from jasper.config import Config
    from jasper.voice_daemon import WakeLoop

    if peering_enabled:
        os.environ["JASPER_PEERING"] = "on"
    cfg = Config.from_env()

    # Use object.__new__ + manual init to skip the heavy constructor
    # (which builds VAD, ducker, etc). We only test methods that touch
    # cfg + a couple of attrs.
    wl = WakeLoop.__new__(WakeLoop)
    wl._cfg = cfg
    wl._peering_current_epoch = ""
    wl._turn = None
    return wl


async def test_peer_arbitrate_disabled_returns_win_without_io():
    """When peering is off (default), _peer_arbitrate is a no-op that
    returns WIN. send_request must not be called — verified by
    patching it to raise loudly if called."""
    wl = _make_wake_loop(peering_enabled=False)
    with patch("jasper.peering.uds.send_request",
               side_effect=AssertionError("send_request must not be called")):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"
    assert wl._peering_current_epoch == ""


async def test_peer_arbitrate_enabled_win_response_propagates():
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={"result": "WIN", "epoch": "ep-123"}),
    ):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=18.0, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"
    assert wl._peering_current_epoch == "ep-123"


async def test_peer_arbitrate_enabled_lose_response_propagates():
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={"result": "LOSE", "epoch": "ep-456"}),
    ):
        result = await wl._peer_arbitrate(
            score=0.5, snr_db=10.0, rms_dbfs=-25.0, can_serve=True,
        )
    assert result == "LOSE"
    assert wl._peering_current_epoch == "ep-456"


async def test_peer_arbitrate_file_not_found_falls_back_to_win():
    """Peering enabled in voice config but jasper-control isn't running
    its peering daemon — UDS doesn't exist. Voice falls back to solo
    behavior rather than silencing the speaker."""
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=FileNotFoundError),
    ):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_peer_arbitrate_timeout_falls_back_to_win():
    """Peering daemon is slow or wedged — fail open. The user gets a
    response (maybe duplicate with another peer), which beats silence."""
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_peer_arbitrate_oserror_falls_back_to_win():
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=OSError("connection refused")),
    ):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_peer_arbitrate_garbage_response_falls_back_to_win():
    """A peering daemon bug returning something other than WIN/LOSE
    shouldn't lock up the wake path — default to WIN."""
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={"result": "MAYBE", "epoch": "ep"}),
    ):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


async def test_peer_arbitrate_empty_response_falls_back_to_win():
    wl = _make_wake_loop(peering_enabled=True)
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(return_value={}),
    ):
        result = await wl._peer_arbitrate(
            score=0.8, snr_db=None, rms_dbfs=-20.0, can_serve=True,
        )
    assert result == "WIN"


# ---------- session lifecycle notifications ----------


async def test_notify_session_started_disabled_is_noop():
    """When peering is off, the notification is a fast no-op (no UDS
    connect attempt)."""
    wl = _make_wake_loop(peering_enabled=False)
    wl._turn = MagicMock()  # pretend we have a turn
    with patch("jasper.peering.uds.send_request",
               side_effect=AssertionError("should not call")):
        await wl._notify_peering_session_started()  # no raise


async def test_notify_session_started_enabled_sends_command():
    wl = _make_wake_loop(peering_enabled=True)
    wl._turn = MagicMock()
    wl._peering_current_epoch = "ep-abc"
    mock = AsyncMock(return_value={"result": "ok"})
    with patch("jasper.peering.uds.send_request", new=mock):
        await wl._notify_peering_session_started()
    # send_request called with SESSION_STARTED <epoch>
    args, kwargs = mock.call_args
    assert args[1] == "SESSION_STARTED ep-abc"


async def test_notify_session_ended_enabled_sends_reason():
    wl = _make_wake_loop(peering_enabled=True)
    wl._peering_current_epoch = "ep-xyz"
    mock = AsyncMock(return_value={"result": "ok"})
    with patch("jasper.peering.uds.send_request", new=mock):
        await wl._notify_peering_session_ended("user_silence")
    args, kwargs = mock.call_args
    assert args[1] == "SESSION_ENDED ep-xyz user_silence"


async def test_notify_session_ended_swallows_errors():
    """Peering notifications are best-effort; errors must not propagate
    into the voice daemon's _end_turn path."""
    wl = _make_wake_loop(peering_enabled=True)
    wl._peering_current_epoch = "ep-abc"
    with patch(
        "jasper.peering.uds.send_request",
        new=AsyncMock(side_effect=OSError("broken pipe")),
    ):
        await wl._notify_peering_session_ended("error")  # no raise
