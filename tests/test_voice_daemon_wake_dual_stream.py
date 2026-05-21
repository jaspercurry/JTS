"""Unit tests for the dual-stream wake-word OR-gate.

WakeLoop's `_handle_wake_frame(frame, leg)` is the single entry point
that both the primary (AEC ON, called from `run()`) and the secondary
(AEC OFF, called from `_wake_secondary_loop`) feed frames into. The
OR-gate semantics live here:

  - Either leg can fire wake.
  - The shared `_wake_fire_lock` + `_refractory_until` ensure a single
    user attempt produces at most one wake event regardless of which
    leg(s) cross threshold first.
  - The wake event payload carries BOTH legs' most-recent peak scores,
    with stale-leg suppression so a stopped AEC OFF stream surfaces as
    `none` rather than as a misleading 3-second-old score.

These tests exercise that critical section without spinning up real
mics, real models, or the rest of the daemon. Mirrors the construction
pattern from tests/test_voice_daemon_peering.py.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types as _types
from unittest.mock import MagicMock

import numpy as np
import pytest

# voice_daemon → audio_io → sounddevice (lazy at use, but module-level
# `import sounddevice as sd` in audio_io evaluates eagerly). Stub it.
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = _types.ModuleType("sounddevice")

from jasper.voice_daemon import WakeLoop, State  # noqa: E402


def _make_detector(threshold: float = 0.5) -> MagicMock:
    """Stub WakeWordDetector with controllable score_frame return + a
    spy on `reset()`. Threshold attribute mirrors the production type."""
    d = MagicMock()
    d.threshold = threshold
    d.score_frame = MagicMock(return_value=0.0)
    d.reset = MagicMock()
    return d


def _make_wake_loop(
    *,
    detector_off=None,
    spend_allowed: bool = True,
    conn_paused: bool = False,
) -> WakeLoop:
    """Construct a minimal WakeLoop bypassing the heavy real
    constructor. Only the attrs `_handle_wake_frame` touches are
    populated; everything else is mocked to detect accidental use."""
    wl = WakeLoop.__new__(WakeLoop)
    wl._cfg = MagicMock()
    wl._cfg.peering_enabled = False
    wl._detector = _make_detector()
    wl._detector_off = detector_off
    wl._recent_score_on = 0.0
    wl._recent_score_off = 0.0
    wl._recent_score_on_at = 0.0
    wl._recent_score_off_at = 0.0
    wl._wake_fire_lock = asyncio.Lock()
    wl._refractory_until = 0.0
    wl._acquiring = False
    wl._acquire_buffer = MagicMock()
    wl._wake_event_at_monotonic = 0.0
    wl._spend_cap = MagicMock()
    wl._spend_cap.allowed = MagicMock(return_value=spend_allowed)
    wl._connection = MagicMock()
    wl._connection.is_paused = MagicMock(return_value=conn_paused)
    # Spy on the arbitrate flow — we don't exercise it here; we just
    # care that wake-fire dispatches it. A no-op coroutine satisfies
    # the `asyncio.create_task(_arbitrate_acquire_drain(...))` call.
    async def _noop(**kwargs):
        return None
    wl._arbitrate_acquire_drain = MagicMock(side_effect=_noop)
    return wl


def _frame(samples: int = 1280) -> np.ndarray:
    """Zero-filled int16 frame of OUTPUT_FRAME_SAMPLES — matches what
    UdpMicCapture yields. The bytes don't matter because the detector
    is mocked; score_frame's return value drives the test."""
    return np.zeros(samples, dtype=np.int16)


# ---------------------------------------------------------------------------
# Single-leg behavior (regression — no dual-stream wiring present)
# ---------------------------------------------------------------------------


async def test_single_stream_on_fires_when_threshold_crossed(caplog):
    """No detector_off → existing single-stream behavior. AEC ON
    frame with score >= threshold fires wake exactly once and sets
    refractory_until."""
    wl = _make_wake_loop(detector_off=None)
    wl._detector.score_frame.return_value = 0.85

    with caplog.at_level(logging.INFO):
        await wl._handle_wake_frame(_frame(), leg="on")

    wl._detector.score_frame.assert_called_once()
    assert wl._refractory_until > 0
    wl._detector.reset.assert_called_once()
    wl._arbitrate_acquire_drain.assert_called_once()
    # Log line includes both per-leg scores and the firing leg
    assert any(
        "event=wake.detected" in r.message and "leg=on" in r.message
        for r in caplog.records
    )


async def test_subthreshold_frame_updates_recent_score_but_does_not_fire():
    """A score below threshold updates _recent_score_on (so the OTHER
    leg's eventual fire can attach it as context) but does NOT fire
    wake or set refractory."""
    wl = _make_wake_loop(detector_off=None)
    wl._detector.score_frame.return_value = 0.07

    await wl._handle_wake_frame(_frame(), leg="on")

    assert wl._recent_score_on == pytest.approx(0.07)
    assert wl._refractory_until == 0.0
    wl._detector.reset.assert_not_called()
    wl._arbitrate_acquire_drain.assert_not_called()


# ---------------------------------------------------------------------------
# Dual-stream OR-gate
# ---------------------------------------------------------------------------


async def test_aec_off_alone_fires_wake():
    """When the AEC OFF leg crosses threshold and AEC ON did not,
    wake still fires — that's the whole point of the OR-gate. Both
    detectors get reset (loser's primed state could phantom-fire
    next window)."""
    detector_off = _make_detector(threshold=0.5)
    detector_off.score_frame.return_value = 0.62
    wl = _make_wake_loop(detector_off=detector_off)
    # AEC ON's recent score was sub-threshold from a previous frame;
    # it should appear in the wake event payload regardless.
    wl._recent_score_on = 0.08
    wl._recent_score_on_at = asyncio.get_event_loop().time()

    await wl._handle_wake_frame(_frame(), leg="off")

    detector_off.score_frame.assert_called_once()
    wl._detector.reset.assert_called_once()
    detector_off.reset.assert_called_once()
    assert wl._refractory_until > 0
    wl._arbitrate_acquire_drain.assert_called_once()


async def test_or_gate_dedupes_concurrent_fires_via_refractory():
    """If BOTH legs cross threshold in quick succession, only ONE
    wake event fires. The second leg sees `now < refractory_until`
    inside the lock and bows out cleanly."""
    detector_off = _make_detector(threshold=0.5)
    detector_off.score_frame.return_value = 0.71
    wl = _make_wake_loop(detector_off=detector_off)
    wl._detector.score_frame.return_value = 0.82

    # AEC ON fires first
    await wl._handle_wake_frame(_frame(), leg="on")
    # AEC OFF tries to fire immediately after (refractory window open)
    await wl._handle_wake_frame(_frame(), leg="off")

    # Exactly one arbitrate dispatch
    assert wl._arbitrate_acquire_drain.call_count == 1
    # Both detectors reset once each (on the first fire only — the
    # second call returns early at the refractory check before
    # reaching reset)
    wl._detector.reset.assert_called_once()
    detector_off.reset.assert_called_once()


async def test_both_legs_recent_scores_attached_when_fire(caplog):
    """The log line carries BOTH per-leg scores. When AEC OFF fires
    and AEC ON has a recent sub-threshold score, both appear so we
    can see the OR-gate's value at a glance."""
    detector_off = _make_detector(threshold=0.5)
    detector_off.score_frame.return_value = 0.55
    wl = _make_wake_loop(detector_off=detector_off)
    wl._recent_score_on = 0.09
    wl._recent_score_on_at = asyncio.get_event_loop().time()

    with caplog.at_level(logging.INFO):
        await wl._handle_wake_frame(_frame(), leg="off")

    wake_logs = [
        r.message for r in caplog.records
        if "event=wake.detected" in r.message
    ]
    assert len(wake_logs) == 1
    msg = wake_logs[0]
    assert "leg=off" in msg
    assert "score_off=0.55" in msg
    assert "score_on=0.09" in msg


async def test_stale_other_leg_score_reported_as_none(caplog):
    """If AEC OFF's last score is older than the staleness window
    (e.g. bridge crashed, no frames arriving on 9877), AEC ON
    firing should NOT lie about a 5-second-old AEC OFF score. The
    log shows `score_off=none` so the operator sees the leg dried up."""
    detector_off = _make_detector(threshold=0.5)
    wl = _make_wake_loop(detector_off=detector_off)
    wl._detector.score_frame.return_value = 0.91
    # AEC OFF "last scored" several seconds ago — beyond the 320ms
    # staleness threshold in _handle_wake_frame.
    wl._recent_score_off = 0.42
    wl._recent_score_off_at = asyncio.get_event_loop().time() - 5.0

    with caplog.at_level(logging.INFO):
        await wl._handle_wake_frame(_frame(), leg="on")

    wake_logs = [
        r.message for r in caplog.records
        if "event=wake.detected" in r.message
    ]
    assert len(wake_logs) == 1
    msg = wake_logs[0]
    assert "leg=on" in msg
    assert "score_on=0.91" in msg
    assert "score_off=none" in msg, msg


async def test_refractory_blocks_immediate_re_fire_on_same_leg():
    """Within the refractory window, a high-score frame on the
    same leg is silently swallowed — no double-fire even without
    the OR-gate dimension."""
    wl = _make_wake_loop(detector_off=None)
    wl._detector.score_frame.return_value = 0.9

    await wl._handle_wake_frame(_frame(), leg="on")
    first_fire_count = wl._arbitrate_acquire_drain.call_count
    # Immediately repeat — should be swallowed by refractory
    await wl._handle_wake_frame(_frame(), leg="on")

    assert wl._arbitrate_acquire_drain.call_count == first_fire_count
    # The detector was NOT re-scored on the second call (the
    # refractory early-out is before score_frame to save CPU).
    assert wl._detector.score_frame.call_count == 1


async def test_missing_detector_off_means_off_leg_is_noop():
    """Defense-in-depth: if `_handle_wake_frame(leg='off')` is
    called without a detector_off configured, the call returns
    cleanly rather than raising AttributeError. Important because
    the secondary loop might already be in flight when run()
    cleans up."""
    wl = _make_wake_loop(detector_off=None)

    # Should not raise
    await wl._handle_wake_frame(_frame(), leg="off")

    assert wl._refractory_until == 0.0
    wl._arbitrate_acquire_drain.assert_not_called()


# ---------------------------------------------------------------------------
# Construction-site config
# ---------------------------------------------------------------------------


def test_mic_device_raw_defaults_to_empty(monkeypatch):
    """Default JASPER_MIC_DEVICE_RAW is empty — single-stream is
    the rollout-safe default. Operators opt in via the env var."""
    from jasper.config import Config

    # Required env vars for from_env(); pin to known-empty raw mic.
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "stub-key")
    monkeypatch.delenv("JASPER_MIC_DEVICE_RAW", raising=False)

    cfg = Config.from_env()
    assert cfg.mic_device_raw == ""


def test_mic_device_raw_picks_up_env_setting(monkeypatch):
    """When set, mic_device_raw is propagated verbatim — accepts
    the same forms as JASPER_MIC_DEVICE."""
    from jasper.config import Config

    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "stub-key")
    monkeypatch.setenv("JASPER_MIC_DEVICE_RAW", "udp:9877")

    cfg = Config.from_env()
    assert cfg.mic_device_raw == "udp:9877"
