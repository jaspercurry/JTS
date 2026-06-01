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
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# voice_daemon → audio_io → sounddevice (eager module-level import).
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = _types.ModuleType("sounddevice")
if "rapidfuzz" not in sys.modules:
    rapidfuzz = _types.ModuleType("rapidfuzz")
    rapidfuzz.fuzz = _types.SimpleNamespace()
    sys.modules["rapidfuzz"] = rapidfuzz

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
    detector_chip_aec_150=None,
    detector_chip_aec_210=None,
    spend_allowed: bool = True,
    conn_paused: bool = False,
) -> WakeLoop:
    """Multi-leg WakeLoop with a mocked wake_event_store. Bypasses
    __init__ — only the attrs `_handle_wake_frame` touches are
    populated, plus the telemetry store stub we assert on.

    The chip-AEC beam legs are opt-in (pass a detector to wire one in),
    mirroring the optional off/dtln legs."""
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


# ---------------------------------------------------------------------------
# Chip-AEC beam legs — the promotion's fire-path / telemetry wiring
# ---------------------------------------------------------------------------


async def test_chip_aec_150_fire_records_trigger_and_score():
    """A chip-AEC beam fire routes through its own _LEG_DB entry:
    trigger_kind="fire_chip_aec_150" and the score lands in
    peak_score_chip_aec_150 (not a software-leg column). Pins the
    chip-AEC promotion's telemetry wiring the way the DTLN test pins
    the third leg's."""
    detector_chip = _make_detector(threshold=0.5)
    detector_chip.score_frame.return_value = 0.79
    wl = _make_wake_loop_triple(detector_chip_aec_150=detector_chip)

    await wl._handle_wake_frame(_frame(), leg="chip_aec_150")

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
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

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
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
    monkeypatch.setattr("jasper.voice_daemon.CAPTURE_POST_SEC", 0.0)
    frame_on = np.full(4, 1, dtype=np.int16)
    frame_150 = np.full(4, 150, dtype=np.int16)
    frame_210 = np.full(4, 210, dtype=np.int16)
    wl._legs["on"].capture_ring = deque([frame_on])
    wl._legs["chip_aec_150"].capture_ring = deque([frame_150])
    wl._legs["chip_aec_210"].capture_ring = deque([frame_210])
    wl._snapshot_ring = WakeLoop._snapshot_ring
    wl._wake_event_store.attach_audio = AsyncMock()

    await wl._finalize_event_audio("evt-chip")

    kwargs = wl._wake_event_store.attach_audio.await_args.kwargs
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
    msg = next(
        r.message for r in caplog.records if "event=wake.detected" in r.message
    )
    assert "score_on=0.91" in msg
    assert "score_off" not in msg
    assert "score_dtln" not in msg
    assert "score_chip_aec" not in msg


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
    msg = next(
        r.message for r in caplog.records if "event=wake.detected" in r.message
    )
    assert "score_on=0.88" in msg
    assert "score_chip_aec_150" in msg and "score_chip_aec_210" in msg
    assert "score_off" not in msg
    assert "score_dtln" not in msg


def test_leg_db_covers_all_wake_input_legs():
    """Every wake-input leg in the registry must have a _LEG_DB telemetry
    mapping — otherwise _handle_wake_frame would KeyError on a leg present
    in self._legs but missing from _LEG_DB. (voice_daemon also guards this
    at import; this gives a targeted, discoverable failure if it drifts.)"""
    from jasper.voice_daemon import _LEG_DB
    from jasper.wake_legs import wake_input_legs

    missing = {leg.token for leg in wake_input_legs()} - set(_LEG_DB)
    assert not missing, f"wake legs missing _LEG_DB mapping: {sorted(missing)}"


# ---------------------------------------------------------------------------
# _configured_wake_legs — the pure leg-selection decision (0.3)
#
# run()'s AsyncExitStack wiring is not hardware-free-testable (it opens
# real mics), so the *decision* of which legs to build is factored into
# this pure function and covered here. The mic-open + lifecycle layer on
# top is exercised by the Pi smoke-test.
# ---------------------------------------------------------------------------


def _cfg(
    mic_device="udp:9876",
    mic_device_raw="",
    mic_device_dtln="",
    mic_device_chip_aec_150="",
    mic_device_chip_aec_210="",
):
    """Minimal Config stand-in for _configured_wake_legs (which reads each
    wake-input leg's device attr by name). SimpleNamespace, not MagicMock —
    a MagicMock's auto-created attrs are truthy and would defeat the
    empty-string gating the function under test relies on."""
    from types import SimpleNamespace
    return SimpleNamespace(
        mic_device=mic_device,
        mic_device_raw=mic_device_raw,
        mic_device_dtln=mic_device_dtln,
        mic_device_chip_aec_150=mic_device_chip_aec_150,
        mic_device_chip_aec_210=mic_device_chip_aec_210,
    )


def test_configured_wake_legs_single_stream():
    """Only the primary device set → only the "on" leg, with its device."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(mic_device="Array"))
    assert [(s.token, dev) for s, dev in legs] == [("on", "Array")]


def test_configured_wake_legs_dual_stream():
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(
        _cfg(mic_device="udp:9876", mic_device_raw="udp:9877"),
    )
    assert [(s.token, dev) for s, dev in legs] == [
        ("on", "udp:9876"), ("off", "udp:9877"),
    ]


def test_configured_wake_legs_triple_stream():
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876", mic_device_raw="udp:9877",
        mic_device_dtln="udp:9878",
    ))
    assert [(s.token, dev) for s, dev in legs] == [
        ("on", "udp:9876"), ("off", "udp:9877"), ("dtln", "udp:9878"),
    ]


def test_configured_wake_legs_independent_gating():
    """Optional legs gate independently: DTLN configured without the
    chip-direct ("off") leg yields on + dtln, no off — so voice never
    opens a UDP listener for an unconfigured leg."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876", mic_device_raw="", mic_device_dtln="udp:9878",
    ))
    assert [s.token for s, _ in legs] == ["on", "dtln"]


def test_configured_wake_legs_primary_always_present():
    """The "on" leg is always built — even with an empty device (the AEC
    reconciler owns ensuring the device is real, or parking voice). Keeps
    WakeLoop's `self._legs["on"]` alias invariant from KeyError-ing."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(mic_device=""))
    assert [s.token for s, _ in legs] == ["on"]


def test_configured_wake_legs_chip_legs_not_built_when_unset():
    """Byte-identical-when-off proof for the chip-AEC promotion: with the
    chip device vars empty (the default), the chip legs are NOT built — so
    an install that hasn't opted in opens no chip UDP listener and the
    configured leg set is exactly the pre-promotion software legs."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876", mic_device_raw="udp:9877",
        mic_device_dtln="udp:9878",
    ))
    tokens = [s.token for s, _ in legs]
    assert tokens == ["on", "off", "dtln"]
    assert "chip_aec_150" not in tokens
    assert "chip_aec_210" not in tokens


def test_configured_wake_legs_chip_legs_built_when_set():
    """Each chip beam leg is built (with its device) when its device var is
    non-empty. With only the chip vars set, the software off/dtln legs stay
    unbuilt (single-chip mutual exclusion is the reconciler's job; here we
    just confirm the per-leg gating threads the chip device through)."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876",
        mic_device_chip_aec_150="udp:9887",
        mic_device_chip_aec_210="udp:9888",
    ))
    assert [(s.token, dev) for s, dev in legs] == [
        ("on", "udp:9876"),
        ("chip_aec_150", "udp:9887"),
        ("chip_aec_210", "udp:9888"),
    ]


def test_configured_wake_legs_chip_beams_gate_independently():
    """One chip beam can be configured without the other — voice never opens
    a UDP listener for an unconfigured beam."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876", mic_device_chip_aec_150="udp:9887",
    ))
    assert [s.token for s, _ in legs] == ["on", "chip_aec_150"]


def test_leg_device_attr_covers_all_wake_input_legs():
    """Every wake-input leg must have a _LEG_DEVICE_ATTR entry, or
    _configured_wake_legs would KeyError at daemon startup."""
    from jasper.voice_daemon import _LEG_DEVICE_ATTR
    from jasper.wake_legs import wake_input_legs
    missing = {leg.token for leg in wake_input_legs()} - set(_LEG_DEVICE_ATTR)
    assert not missing, (
        f"wake legs missing _LEG_DEVICE_ATTR: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# session_status — runtime-armed legs surfaced in /state (observability)
# ---------------------------------------------------------------------------


def _prep_session_status(wl) -> None:
    """Set the few attrs session_status() reads beyond the fire path, so
    it can be called on a __new__-built WakeLoop."""
    from jasper.voice_daemon import State
    wl._state = State.WAKE
    wl._input_ended = False
    wl._ducker = MagicMock()
    wl._ducker.is_ducked = False
    wl._content_activity = MagicMock()
    wl._content_activity.music_dbfs = -32.0


def test_session_status_reports_armed_legs_triple():
    """session_status surfaces the actually-armed leg tokens (runtime
    truth, in jasper.wake_legs order) so a startup leg-skip is visible in
    /state.voice — /aec only shows configured intent from aec_mode.env."""
    wl = _make_wake_loop_triple(
        detector_off=_make_detector(), detector_dtln=_make_detector(),
    )
    _prep_session_status(wl)
    assert wl.session_status()["wake_legs"] == ["on", "off", "dtln"]


def test_session_status_reports_only_armed_legs_when_optional_absent():
    """Dual-stream (no DTLN leg) reports exactly the armed legs — the
    field reflects what the daemon opened, not what was configured."""
    wl = _make_wake_loop_triple(detector_off=_make_detector())
    _prep_session_status(wl)
    assert wl.session_status()["wake_legs"] == ["on", "off"]


# ---------------------------------------------------------------------------
# _ring_noise_floor_dbfs — fire-time ambient floor for the condition estimator
# ---------------------------------------------------------------------------


def test_ring_noise_floor_empty_or_none_is_none():
    from collections import deque
    from jasper.voice_daemon import _ring_noise_floor_dbfs
    assert _ring_noise_floor_dbfs(None) is None
    assert _ring_noise_floor_dbfs(deque()) is None


def test_ring_noise_floor_tracks_quiet_background_not_utterance():
    """The low percentile reflects the quiet majority (room floor), not the
    few loud frames (the wake utterance) — so it estimates ambient, not the
    speech that just fired."""
    from collections import deque
    from jasper.voice_daemon import _ring_noise_floor_dbfs
    quiet = np.full(1280, 30, dtype=np.int16)     # near-silent background
    loud = np.full(1280, 8000, dtype=np.int16)    # the "utterance" frames
    ring = deque([quiet] * 16 + [loud] * 4)        # utterance is the minority
    floor = _ring_noise_floor_dbfs(ring)
    assert floor is not None
    assert floor < -40.0  # 25th pct sits in the quiet group, far below loud


# --- Phase 1.3a: live-condition refresh (WakeLoop._read_music_dbfs +
# _maybe_refresh_condition) ---

def _wakeloop_for_condition(music_dbfs=-30.0):
    """A bare WakeLoop with only the attributes the condition-refresh path
    touches. music_dbfs=-30 reads as music (> -60 dBFS); the empty capture
    ring makes the noise floor None."""
    from collections import deque

    wl = WakeLoop.__new__(WakeLoop)
    wl._condition_refreshed_at = 0.0
    wl._current_condition = "quiet"
    wl._capture_ring_on = deque(maxlen=8)
    wl._content_activity = MagicMock()
    wl._content_activity.music_dbfs = music_dbfs
    return wl


def test_read_music_dbfs_reads_content_activity():
    assert _wakeloop_for_condition(music_dbfs=-30.0)._read_music_dbfs() == -30.0


def test_read_music_dbfs_none_when_unavailable():
    wl = _wakeloop_for_condition()
    wl._content_activity.music_dbfs = None
    assert wl._read_music_dbfs() is None


def test_maybe_refresh_condition_recomputes_when_elapsed():
    wl = _wakeloop_for_condition(music_dbfs=-30.0)  # > -60 dBFS -> music
    wl._maybe_refresh_condition(now_loop=5.0)
    assert wl._current_condition == "music"
    assert wl._condition_refreshed_at == 5.0


def test_maybe_refresh_condition_skips_within_window():
    wl = _wakeloop_for_condition(music_dbfs=-30.0)
    wl._condition_refreshed_at = 4.5
    wl._current_condition = "quiet"
    wl._maybe_refresh_condition(now_loop=5.0)  # 0.5 s < CONDITION_REFRESH_SEC
    assert wl._current_condition == "quiet"  # unchanged
    assert wl._condition_refreshed_at == 4.5  # unchanged


def test_maybe_refresh_condition_fail_soft_on_classify_error(monkeypatch):
    # The wake path must never break because ancillary condition estimation
    # raised. On error: keep the last good condition, advance the timer (so a
    # persistent failure retries at ~1 Hz, not every frame), do not propagate.
    wl = _wakeloop_for_condition(music_dbfs=-30.0)
    wl._current_condition = "ambient"  # last good

    def _boom(*_a, **_k):
        raise RuntimeError("classify blew up")

    monkeypatch.setattr("jasper.voice_daemon.classify_condition", _boom)
    wl._maybe_refresh_condition(now_loop=5.0)  # must not raise
    assert wl._current_condition == "ambient"  # stale condition kept
    assert wl._condition_refreshed_at == 5.0   # timer advanced -> ~1 Hz retry
