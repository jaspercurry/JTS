# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the triple-stream wake-word OR-gate.

Extends the dual-stream test patterns from
`test_voice_daemon_wake_dual_stream.py` to cover the DTLN-aec leg
added 2026-05-23. The critical regression these tests pin down:

  - When `leg="dtln"` fires, `trigger_kind` must be `"fire_dtln"`
    (not `"fire_aec_off"`) and the score must land in
    `peak_score_dtln_aec` (not corrupt `peak_score_aec_off`).
  - All three legs' offsets + RMSes flow to the wake_events store.

Constructs WakeLoop via `for_tests()` (no real mic, model, or daemon),
mocks the wake-telemetry store, and inspects the kwargs passed to
`begin_event`.
"""
from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from jasper.voice_daemon import WakeLoop, _LegRuntime
from jasper.wake_legs import by_token
from tests._log_events import event_fields


def _make_detector(threshold: float = 0.5) -> MagicMock:
    d = MagicMock()
    d.threshold = threshold
    d.score_frame = MagicMock(return_value=0.0)
    d.reset = MagicMock()
    return d


def _make_wake_loop_triple(
    *,
    detector_off=None,
    detector_dtln=None,
    detector_chip_aec_150=None,
    detector_chip_aec_210=None,
    spend_allowed: bool = True,
    conn_paused: bool = False,
) -> WakeLoop:
    """Multi-leg WakeLoop with a mocked wake_event_store. Starts from
    the test seam and overrides the attrs `_handle_wake_frame` touches,
    plus the telemetry store stub we assert on.

    The chip-AEC beam legs are opt-in (pass a detector to wire one in),
    mirroring the optional off/dtln legs."""
    # Mocked telemetry store. begin_event is an AsyncMock so the
    # `await store.begin_event(...)` call resolves without real DB I/O.
    store = MagicMock()
    store.begin_event = AsyncMock()
    wl = WakeLoop.for_tests(wake_event_store=store)
    wl._cfg = MagicMock()
    wl._cfg.peering_enabled = False
    wl._detector = _make_detector()
    # Build the leg collection the refactored _handle_wake_frame reads.
    # capture_ring=None is fine — _tail_frame_rms_dbfs tolerates None.
    wl._legs = {
        "on": _LegRuntime(by_token("on"), MagicMock(), wl._detector, None),
    }
    if detector_off is not None:
        wl._legs["off"] = _LegRuntime(
            by_token("off"), MagicMock(), detector_off, None,
        )
    if detector_dtln is not None:
        wl._legs["dtln"] = _LegRuntime(
            by_token("dtln"), MagicMock(), detector_dtln, None,
        )
    if detector_chip_aec_150 is not None:
        wl._legs["chip_aec_150"] = _LegRuntime(
            by_token("chip_aec_150"), MagicMock(), detector_chip_aec_150, None,
        )
    if detector_chip_aec_210 is not None:
        wl._legs["chip_aec_210"] = _LegRuntime(
            by_token("chip_aec_210"), MagicMock(), detector_chip_aec_210, None,
        )
    wl._wake_fire_lock = asyncio.Lock()
    from jasper.wake_fusion import WakeFuser
    wl._fuser = WakeFuser()
    wl._current_condition = "quiet"
    wl._condition_refreshed_at = 0.0
    wl._refractory_until = 0.0
    wl._acquiring = False
    wl._acquire_buffer = MagicMock()
    wl._fire_and_forget = set()
    wl._wake_event_at_monotonic = 0.0
    wl._spend_cap = MagicMock()
    wl._spend_cap.allowed = MagicMock(return_value=spend_allowed)
    wl._connection = MagicMock()
    wl._connection.is_paused = MagicMock(return_value=conn_paused)
    wl._mic_muted = False
    # Capture rings — empty deques; _tail_frame_rms_dbfs handles None.
    wl._capture_ring_on = None
    wl._capture_ring_off = None
    wl._capture_ring_dtln = None
    wl._content_activity = MagicMock()
    wl._content_activity.music_dbfs = None

    async def _noop(**kwargs):
        return None
    wl._arbitrate_acquire_drain = MagicMock(side_effect=_noop)

    # Snapshot helper used by the capture finalize task; not exercised
    # here (we never reach the finalize path), but stub anyway so any
    # attribute lookup is safe.
    wl._snapshot_ring = MagicMock(return_value=None)
    return wl


def _frame(samples: int = 1280) -> np.ndarray:
    return np.zeros(samples, dtype=np.int16)


# ---------------------------------------------------------------------------
# Bug 1 regression — DTLN-only fire correctly attributed
# ---------------------------------------------------------------------------


async def test_dtln_only_fire_records_trigger_kind_fire_dtln():
    """The bug: if/elif handled only "on" and "off"; "dtln" fell into
    the else branch and recorded `trigger_kind="fire_aec_off"` with
    `peak_off=score`, corrupting the AEC OFF leg's data. The fix
    adds an explicit elif for "dtln" → `trigger_kind="fire_dtln"`
    and `peak_dtln=score`.

    Without this fix, the whole point of the triple-stream architecture
    is undermined: DTLN solo-fires (the cases that prove the third
    leg's distinct value) would be silently misattributed as AEC OFF
    fires, and the AEC OFF leg's peak_score distribution would be
    polluted with DTLN's scores.
    """
    detector_dtln = _make_detector(threshold=0.5)
    detector_dtln.score_frame.return_value = 0.82
    wl = _make_wake_loop_triple(detector_dtln=detector_dtln)

    await wl._handle_wake_frame(_frame(), leg="dtln")

    # Exactly one begin_event call, with correct attribution.
    assert wl._wake_telemetry.store.begin_event.await_count == 1
    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_dtln", (
        f"DTLN fire mis-recorded as {kwargs['trigger_kind']!r}; the bug"
        " is back — check the per-leg column routing in"
        " WakeTelemetry.on_fire."
    )
    # DTLN score lands in the DTLN column, NOT the AEC OFF column.
    assert kwargs["peak_score_dtln_aec"] == pytest.approx(0.82)
    # AEC OFF column should be None (no detector_off was configured,
    # so peak_off was never set above zero in this test).
    assert kwargs["peak_score_aec_off"] in (None, 0.0)


async def test_dtln_fire_passes_all_three_leg_telemetry_fields():
    """All three legs' offset_ms + RMS fields are passed when DTLN
    fires — the dual-stream version would have left peak_offset_ms_dtln
    and mic_rms_dbfs_dtln as kwargs that begin_event never receives."""
    detector_dtln = _make_detector(threshold=0.5)
    detector_dtln.score_frame.return_value = 0.75
    wl = _make_wake_loop_triple(detector_dtln=detector_dtln)

    await wl._handle_wake_frame(_frame(), leg="dtln")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    # The DTLN-specific kwargs must be present (even if None).
    assert "peak_offset_ms_dtln" in kwargs
    assert "mic_rms_dbfs_dtln" in kwargs
    # fired_legs should include "dtln" (the firing leg always is).
    assert "dtln" in kwargs["fired_legs"].split(","), kwargs["fired_legs"]


async def test_aec_on_fire_still_records_fire_aec_on():
    """Regression on the non-broken path — make sure adding the dtln
    branch didn't change AEC ON behavior."""
    wl = _make_wake_loop_triple()
    wl._detector.score_frame.return_value = 0.91

    await wl._handle_wake_frame(_frame(), leg="on")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_aec_on"
    assert kwargs["peak_score_aec_on"] == pytest.approx(0.91)


async def test_aec_off_fire_still_records_fire_aec_off():
    """Regression on the other non-broken path."""
    detector_off = _make_detector(threshold=0.5)
    detector_off.score_frame.return_value = 0.88
    wl = _make_wake_loop_triple(detector_off=detector_off)

    await wl._handle_wake_frame(_frame(), leg="off")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_aec_off"
    assert kwargs["peak_score_aec_off"] == pytest.approx(0.88)


async def test_non_primary_fire_records_firing_leg_effective_threshold():
    """begin_event must store the threshold the firing leg actually had
    to cross. AEC ON can keep the base threshold while AEC OFF is raised
    for the current condition; if AEC OFF wins, the row should record
    the raised AEC OFF threshold, not the primary detector's base value."""
    from jasper.wake_fusion import WakeFuser

    detector_off = _make_detector(threshold=0.5)
    detector_off.score_frame.return_value = 0.72
    wl = _make_wake_loop_triple(detector_off=detector_off)
    wl._fuser = WakeFuser({("off", "music"): 0.2})
    wl._current_condition = "music"
    wl._condition_refreshed_at = asyncio.get_event_loop().time()

    await wl._handle_wake_frame(_frame(), leg="off")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_aec_off"
    assert kwargs["fired_legs"] == "off"
    assert kwargs["threshold"] == pytest.approx(0.7)


async def test_dtln_fire_with_other_legs_above_threshold_records_all_in_fired_legs():
    """When DTLN wins the OR-gate race but AEC ON / AEC OFF were also
    above their thresholds at the same instant, `fired_legs` should
    reflect all three. `trigger_kind` stays the winner ("fire_dtln")
    because only one leg can claim the lock."""
    detector_off = _make_detector(threshold=0.5)
    detector_dtln = _make_detector(threshold=0.5)
    detector_dtln.score_frame.return_value = 0.92
    wl = _make_wake_loop_triple(
        detector_off=detector_off, detector_dtln=detector_dtln,
    )
    # AEC ON + AEC OFF have very recent above-threshold scores —
    # within the STALE_SEC window (0.32 s).
    now = asyncio.get_event_loop().time()
    wl._legs["on"].recent_score = 0.87
    wl._legs["on"].recent_score_at = now
    wl._legs["off"].recent_score = 0.95
    wl._legs["off"].recent_score_at = now

    await wl._handle_wake_frame(_frame(), leg="dtln")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_dtln"  # DTLN won the race
    legs = set(kwargs["fired_legs"].split(","))
    assert legs == {"on", "off", "dtln"}, kwargs["fired_legs"]


# ---------------------------------------------------------------------------
# Chip-AEC beam legs — the promotion's fire-path / telemetry wiring
# ---------------------------------------------------------------------------


async def test_chip_aec_150_fire_records_trigger_and_score():
    """A chip-AEC beam fire routes through its own LEG_DB entry:
    trigger_kind="fire_chip_aec_150" and the score lands in
    peak_score_chip_aec_150 (not a software-leg column). Pins the
    chip-AEC promotion's telemetry wiring the way the DTLN test pins
    the third leg's."""
    detector_chip = _make_detector(threshold=0.5)
    detector_chip.score_frame.return_value = 0.79
    wl = _make_wake_loop_triple(detector_chip_aec_150=detector_chip)

    await wl._handle_wake_frame(_frame(), leg="chip_aec_150")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_chip_aec_150"
    assert kwargs["peak_score_chip_aec_150"] == pytest.approx(0.79)
    assert "chip_aec_150" in kwargs["fired_legs"].split(","), kwargs["fired_legs"]
    # Sibling beam + software-leg score columns stay None when unconfigured.
    assert kwargs["peak_score_chip_aec_210"] in (None, 0.0)
    assert kwargs["peak_score_aec_off"] in (None, 0.0)


async def test_chip_beam_corroborates_in_fired_legs_when_software_leg_fires():
    """When the AEC-on leg wins the race but a chip beam was fresh + above
    its threshold at the same instant, fired_legs includes the chip beam —
    the OR-gate corroboration is leg-count-agnostic and counts chip beams,
    and the corroborating beam's recent score lands in its own column."""
    detector_chip = _make_detector(threshold=0.5)
    wl = _make_wake_loop_triple(detector_chip_aec_150=detector_chip)
    wl._detector.score_frame.return_value = 0.90  # "on" wins the race
    now = asyncio.get_event_loop().time()
    wl._legs["chip_aec_150"].recent_score = 0.81
    wl._legs["chip_aec_150"].recent_score_at = now

    await wl._handle_wake_frame(_frame(), leg="on")

    kwargs = wl._wake_telemetry.store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_aec_on"  # "on" claimed the lock
    assert set(kwargs["fired_legs"].split(",")) == {"on", "chip_aec_150"}, (
        kwargs["fired_legs"]
    )
    assert kwargs["peak_score_chip_aec_150"] == pytest.approx(0.81)


async def test_finalize_event_audio_attaches_chip_beam_rings(monkeypatch):
    """Wake-event audio capture follows the configured leg set. In chip-AEC
    mode, both chip beam capture rings are persisted as explicit per-leg
    WAV payloads rather than only recording the historical `audio_on` path."""
    wl = _make_wake_loop_triple(
        detector_chip_aec_150=_make_detector(),
        detector_chip_aec_210=_make_detector(),
    )
    monkeypatch.setattr("jasper.voice.wake_telemetry.CAPTURE_POST_SEC", 0.0)
    frame_on = np.full(4, 1, dtype=np.int16)
    frame_150 = np.full(4, 150, dtype=np.int16)
    frame_210 = np.full(4, 210, dtype=np.int16)
    wl._legs["on"].capture_ring = deque([frame_on])
    wl._legs["chip_aec_150"].capture_ring = deque([frame_150])
    wl._legs["chip_aec_210"].capture_ring = deque([frame_210])
    wl._snapshot_ring = WakeLoop._snapshot_ring
    wl._wake_telemetry.store.attach_audio = AsyncMock()

    await wl._wake_telemetry.finalize_event_audio(
        "evt-chip", snapshot=wl._snapshot_leg_audio,
    )

    kwargs = wl._wake_telemetry.store.attach_audio.await_args.kwargs
    assert kwargs["event_id"] == "evt-chip"
    assert kwargs["audio_on"] == frame_on.tobytes()
    assert kwargs["audio_off"] is None
    assert kwargs["audio_dtln"] is None
    assert kwargs["audio_chip_aec_150"] == frame_150.tobytes()
    assert kwargs["audio_chip_aec_210"] == frame_210.tobytes()


async def test_wake_log_omits_unconfigured_leg_scores(caplog):
    """Adaptivity (mic/leg-set-driven, not the static universe): a
    single-stream install logs only the leg it actually built — no
    score_off / score_dtln / score_chip_aec_* noise for legs it isn't
    running. Guards against the log regressing to iterating every possible
    leg regardless of hardware."""
    import logging
    wl = _make_wake_loop_triple()  # "on" only — no off/dtln/chip detectors
    wl._detector.score_frame.return_value = 0.91
    with caplog.at_level(logging.INFO):
        await wl._handle_wake_frame(_frame(), leg="on")
    fields = event_fields(caplog, "wake.detected")
    assert fields["score_on"] == "0.91"
    assert "score_off" not in fields
    assert "score_dtln" not in fields
    assert "score_chip_aec_150" not in fields
    assert "score_chip_aec_210" not in fields


async def test_wake_log_emits_only_active_legs_with_chip(caplog):
    """A chip-AEC install (on + the two chip beams, no software off/DTLN —
    the reconciler's mutual exclusion) logs exactly those three legs, and
    does NOT emit score_off / score_dtln for legs it isn't running."""
    import logging
    wl = _make_wake_loop_triple(
        detector_chip_aec_150=_make_detector(),
        detector_chip_aec_210=_make_detector(),
    )
    wl._detector.score_frame.return_value = 0.88
    with caplog.at_level(logging.INFO):
        await wl._handle_wake_frame(_frame(), leg="on")
    fields = event_fields(caplog, "wake.detected")
    assert fields["score_on"] == "0.88"
    assert "score_chip_aec_150" in fields and "score_chip_aec_210" in fields
    assert "score_off" not in fields
    assert "score_dtln" not in fields
