"""Unit tests for the triple-stream wake-word OR-gate.

Extends the dual-stream test patterns from
`test_voice_daemon_wake_dual_stream.py` to cover the DTLN-aec leg
added 2026-05-23. The critical regression these tests pin down:

  - When `leg="dtln"` fires, `trigger_kind` must be `"fire_dtln"`
    (not `"fire_aec_off"`) and the score must land in
    `peak_score_dtln_aec` (not corrupt `peak_score_aec_off`).
  - All three legs' offsets + RMSes flow to the wake_events store.

Constructs WakeLoop via `__new__` (no real mic, model, or daemon),
mocks `_wake_event_store`, and inspects the kwargs passed to
`begin_event`.
"""
from __future__ import annotations

import asyncio
import sys
import types as _types
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# voice_daemon → audio_io → sounddevice (eager module-level import).
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = _types.ModuleType("sounddevice")

from jasper.voice_daemon import WakeLoop, _LegRuntime  # noqa: E402
from jasper.wake_legs import by_token  # noqa: E402


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
    spend_allowed: bool = True,
    conn_paused: bool = False,
) -> WakeLoop:
    """Three-leg WakeLoop with a mocked wake_event_store. Bypasses
    __init__ — only the attrs `_handle_wake_frame` touches are
    populated, plus the telemetry store stub we assert on."""
    wl = WakeLoop.__new__(WakeLoop)
    wl._cfg = MagicMock()
    wl._cfg.peering_enabled = False
    wl._cfg.wake_model = "test_model"
    wl._cfg.voice_provider = "gemini"
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
    wl._wake_fire_lock = asyncio.Lock()
    wl._refractory_until = 0.0
    wl._acquiring = False
    wl._acquire_buffer = MagicMock()
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
    # TtsVolumeTracker stub — used to read the music anchor for context.
    wl._tts_volume_tracker = None
    # Mocked telemetry store. begin_event is an AsyncMock so the
    # `await store.begin_event(...)` call resolves without real DB I/O.
    store = MagicMock()
    store.begin_event = AsyncMock()
    wl._wake_event_store = store
    wl._current_event_id = None

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
    assert wl._wake_event_store.begin_event.await_count == 1
    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_dtln", (
        f"DTLN fire mis-recorded as {kwargs['trigger_kind']!r}; the bug"
        " is back — check the if/elif chain in _handle_wake_frame's"
        " telemetry block."
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

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
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

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_aec_on"
    assert kwargs["peak_score_aec_on"] == pytest.approx(0.91)


async def test_aec_off_fire_still_records_fire_aec_off():
    """Regression on the other non-broken path."""
    detector_off = _make_detector(threshold=0.5)
    detector_off.score_frame.return_value = 0.88
    wl = _make_wake_loop_triple(detector_off=detector_off)

    await wl._handle_wake_frame(_frame(), leg="off")

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_aec_off"
    assert kwargs["peak_score_aec_off"] == pytest.approx(0.88)


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

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
    assert kwargs["trigger_kind"] == "fire_dtln"  # DTLN won the race
    legs = set(kwargs["fired_legs"].split(","))
    assert legs == {"on", "off", "dtln"}, kwargs["fired_legs"]


def test_leg_db_covers_all_wake_input_legs():
    """Every wake-input leg in the registry must have a _LEG_DB telemetry
    mapping — otherwise _handle_wake_frame would KeyError on a leg present
    in self._legs but missing from _LEG_DB. (voice_daemon also guards this
    at import; this gives a targeted, discoverable failure if it drifts.)"""
    from jasper.voice_daemon import _LEG_DB
    from jasper.wake_legs import wake_input_legs

    missing = {leg.token for leg in wake_input_legs()} - set(_LEG_DB)
    assert not missing, f"wake legs missing _LEG_DB mapping: {sorted(missing)}"
