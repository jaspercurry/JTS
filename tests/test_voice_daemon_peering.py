# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the peering integration in jasper.voice_daemon.

Covers:
  - The `_frame_rms_dbfs` helper produces correct dBFS for known
    input waveforms (a numeric correctness gate).
  - `_arbitrate_acquire_drain`'s late-cancel gates: a mic mute or an
    open measurement window between wake-frame dispatch and this task
    starting aborts before arbitration, cleanly.

PeeringClient's own arbitration and session-notice behavior (the
WIN/LOSE fail-open guarantee) is covered by
tests/test_peering_client.py. Full wake-handler integration is covered
by the existing voice-daemon-on-Pi smoke tests; it depends on real
openWakeWord + real audio I/O which can't run on CI.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

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


def _make_wake_loop(peering_enabled: bool):
    """Construct a minimal WakeLoop with stubs for everything except
    cfg. Only the peering attrs and a few common ones matter for the
    methods under test."""
    from jasper.config import Config
    from jasper.voice_daemon import WakeLoop

    if peering_enabled:
        os.environ["JASPER_PEERING"] = "on"
    cfg = Config.from_env()

    # Use the test constructor to skip hardware while still getting a
    # fully-shaped WakeLoop. We only test methods that touch cfg + a
    # couple of attrs.
    wl = WakeLoop.for_tests()
    wl._cfg = cfg
    wl._turn = None
    return wl


# ---------- _arbitrate_acquire_drain late-cancel gates ----------


async def test_arbitrate_acquire_drain_aborts_when_mic_muted():
    """If the user mutes the mic between wake-frame dispatch and this
    task starting (e.g. tapped the remote), don't open a session — the
    user just deliberately stopped listening."""
    from jasper.voice_daemon import State
    wl = _make_wake_loop(peering_enabled=False)
    wl._mic_muted = True
    wl._measurement_active = asyncio.Event()
    wl._acquiring = True  # set by caller (_handle_wake_frame)
    wl._acquire_buffer = MagicMock()
    wl._refractory_until = 0.0
    wl._state = State.WAKE

    # Patch out anything the WIN path would touch — they shouldn't be reached.
    wl._begin_turn = AsyncMock(side_effect=AssertionError("should not begin turn"))
    wl._play_listening_chirp = AsyncMock(side_effect=AssertionError("should not chirp"))

    await wl._arbitrate_acquire_drain(
        score=0.8, rms_dbfs=-20.0,
        spend_allowed=True, conn_paused=False, can_serve=True,
    )

    # finally clause ran cleanly
    assert wl._acquiring is False


async def test_arbitrate_acquire_drain_aborts_when_measurement_active():
    """Same shape as mute — MeasurementHold.pause_response is a deliberate
    'stop listening' signal from an open measurement window."""
    from jasper.voice_daemon import State
    wl = _make_wake_loop(peering_enabled=False)
    wl._mic_muted = False
    wl._measurement_active = asyncio.Event()
    wl._measurement_active.set()
    wl._acquiring = True
    wl._acquire_buffer = MagicMock()
    wl._refractory_until = 0.0
    wl._state = State.WAKE

    wl._begin_turn = AsyncMock(side_effect=AssertionError("should not begin turn"))
    wl._play_listening_chirp = AsyncMock(side_effect=AssertionError("should not chirp"))

    await wl._arbitrate_acquire_drain(
        score=0.8, rms_dbfs=-20.0,
        spend_allowed=True, conn_paused=False, can_serve=True,
    )
    assert wl._acquiring is False
