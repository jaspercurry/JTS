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
mocks `_wake_event_store`, and inspects the kwargs passed to
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
    wl = WakeLoop.for_tests()
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

    kwargs = wl._wake_event_store.begin_event.await_args.kwargs
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
    local_mic_present=None,
    manual_mic_sources=None,
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
        local_mic_present=local_mic_present,
        manual_mic_sources=manual_mic_sources or {},
    )


@pytest.mark.parametrize(
    ("cfg_kwargs", "expected"),
    [
        pytest.param(
            {"mic_device": "Array"},
            [("on", "Array")],
            id="configured_wake_legs_single_stream",
        ),
        pytest.param(
            {"mic_device": "udp:9876", "mic_device_raw": "udp:9877"},
            [("on", "udp:9876"), ("off", "udp:9877")],
            id="configured_wake_legs_dual_stream",
        ),
        pytest.param(
            {
                "mic_device": "udp:9876",
                "mic_device_raw": "udp:9877",
                "mic_device_dtln": "udp:9878",
            },
            [("on", "udp:9876"), ("off", "udp:9877"), ("dtln", "udp:9878")],
            id="configured_wake_legs_triple_stream",
        ),
        pytest.param(
            {
                "mic_device": "udp:9876",
                "mic_device_chip_aec_150": "udp:9887",
                "mic_device_chip_aec_210": "udp:9888",
            },
            [
                ("on", "udp:9876"),
                ("chip_aec_150", "udp:9887"),
                ("chip_aec_210", "udp:9888"),
            ],
            id="configured_wake_legs_chip_legs_built_when_set",
        ),
    ],
)
def test_configured_wake_legs(cfg_kwargs, expected):
    """Each leg is built, with its device, exactly when its device var
    is set: "on" alone for a bare primary device; software (off, dtln)
    or chip-beam legs join it as their vars are set."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(**cfg_kwargs))
    assert [(s.token, dev) for s, dev in legs] == expected


@pytest.mark.parametrize(
    ("cfg_kwargs", "expected_tokens"),
    [
        pytest.param(
            {
                "mic_device": "udp:9876",
                "mic_device_raw": "",
                "mic_device_dtln": "udp:9878",
            },
            ["on", "dtln"],
            id="configured_wake_legs_independent_gating",
        ),
        pytest.param(
            {"mic_device": ""},
            ["on"],
            id="configured_wake_legs_primary_always_present",
        ),
        pytest.param(
            {"mic_device": "udp:9876", "mic_device_chip_aec_150": "udp:9887"},
            ["on", "chip_aec_150"],
            id="configured_wake_legs_chip_beams_gate_independently",
        ),
    ],
)
def test_configured_wake_legs_tokens_only(cfg_kwargs, expected_tokens):
    """Optional legs gate independently — voice never opens a UDP
    listener for an unconfigured leg. "on" is always present, even with
    an empty device (the AEC reconciler owns making it real, or parking
    voice), so `self._legs["on"]` never KeyErrors."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(**cfg_kwargs))
    assert [s.token for s, _ in legs] == expected_tokens


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


def test_session_status_surfaces_tool_pack_outcomes():
    """session_status surfaces the per-pack tool-registration outcomes so a
    pack that silently failed to build (event=tool_pack.build_failed) is
    visible in /state.voice + jasper-doctor, not only the journal. The
    field is opaque passthrough — whatever outcomes_to_state produced."""
    wl = _make_wake_loop_triple()
    _prep_session_status(wl)
    packs = [
        {"name": "audio", "status": "registered", "tool_count": 5,
         "error": None},
        {"name": "spotify", "status": "failed", "tool_count": 0,
         "error": "ImportError('spotipy')"},
    ]
    wl._tool_packs = packs
    assert wl.session_status()["tool_packs"] == packs


def test_session_status_tool_packs_defaults_empty():
    """Built without the pack walk (the test seam / a caller that omits
    tool_packs), the field is an empty list, never missing."""
    wl = _make_wake_loop_triple()
    _prep_session_status(wl)
    assert wl.session_status()["tool_packs"] == []


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

    wl = WakeLoop.for_tests()
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


# ---------------------------------------------------------------------------
# Push-to-talk-only speakers — no microphone of their own (today: a
# full-profile box whose mic is unplugged or never fitted, plus a WiiM
# Remote 2). Issue #2205: the start gate already lets these boxes run; these
# pin that the daemon then plans an input set it can actually open, and that
# a mic-BEARING speaker is never downgraded into the same shape by accident.
# ---------------------------------------------------------------------------


def test_no_local_mic_plus_accessory_plans_zero_wake_legs():
    """The published no-local-mic verdict + a published accessory source is
    the one shape that drops the primary leg.

    Without this the "on" leg is built against a card that is not there,
    run() re-raises InputDeviceUnavailable, and the daemon exits 66 before it
    ever reaches the manual-mic loop — the gate opens and the remote's button
    still does nothing.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="Array",
        local_mic_present=False,
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert legs == []


def test_leg_planner_never_infers_push_to_talk_from_an_empty_mic_device():
    """An empty or odd `JASPER_MIC_DEVICE` must NOT be read as "this is a
    push-to-talk speaker".

    `Config.from_env` defaults `mic_device` to the literal "Array" and the AEC
    reconciler writes a real candidate name on its no-mic paths to clear a
    stale udp: device, so "empty primary device" is not evidence of anything
    on a real box. Only the reconciler's published verdict may drop the leg.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="",
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert [(s.token, dev) for s, dev in legs] == [("on", "")]


def test_unresolved_local_mic_still_plans_the_primary_leg():
    """`unknown` (custom device, or no reconcile has run) is NOT `absent`.

    This is the property that keeps "this speaker has no room mic" separable
    from "the room mic should be here and isn't". Collapse them and a broken
    mic silently downgrades to push-to-talk on a box with no remote — a
    speaker that looks healthy and cannot hear.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="UMIK-2",
        local_mic_present=None,
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert [(s.token, dev) for s, dev in legs] == [("on", "UMIK-2")]


def test_no_local_mic_without_an_accessory_still_plans_the_primary_leg():
    """No local mic AND no accessory is a BROKEN speaker, not a PTT one.

    The gate marker should have parked it before Python ran; if it somehow
    starts, the planned leg fails to open and the daemon parks loudly on
    exit 66 rather than idling deaf with no wake detection.
    """
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="Array", local_mic_present=False,
    ))
    assert [(s.token, dev) for s, dev in legs] == [("on", "Array")]


def test_accessory_alongside_a_real_mic_keeps_wake_legs():
    """A push-to-talk remote on a speaker that DOES have a mic is additive:
    it adds a manual source without disabling wake detection."""
    from jasper.voice_daemon import _configured_wake_legs
    legs = _configured_wake_legs(_cfg(
        mic_device="udp:9876",
        mic_device_raw="udp:9877",
        local_mic_present=True,
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    ))
    assert [(s.token, dev) for s, dev in legs] == [
        ("on", "udp:9876"), ("off", "udp:9877"),
    ]


def _remote_runtime():
    from jasper.voice_daemon import _ManualMicRuntime
    return [_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")]


def test_push_to_talk_only_is_derived_from_resolved_runtime():
    """The daemon knows it is push-to-talk from what it actually opened —
    zero wake legs plus at least one manual mic source — never from a config
    string it might have inherited from a default."""
    from jasper.voice_daemon import WakeLoop

    assert WakeLoop.for_tests(
        legs=[], manual_mics=_remote_runtime(),
    )._push_to_talk_only is True
    # Zero legs and no manual source is a broken speaker, not a PTT one.
    assert WakeLoop.for_tests(legs=[])._push_to_talk_only is False
    # A remote on a speaker that also has a room mic is additive.
    assert WakeLoop.for_tests(
        manual_mics=_remote_runtime(),
    )._push_to_talk_only is False


def test_push_to_talk_only_is_the_single_derivation_its_consumers_read():
    """One fact, one derivation, and the sites that act on it read THAT.

    A field with a producer and no consumer is a claim nothing enforces. The
    two acting sites used to re-derive the mode from `self._mic is None`
    independently; forcing the field is now enough to move both, which is
    what makes it the owner of the fact rather than a parallel copy of it.
    """
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests(legs=[], manual_mics=_remote_runtime())
    _prep_session_status(wl)

    # /state, via session_status(). This is the observability half: an empty
    # `wake_legs` alone cannot tell "arms nothing on purpose" from "every leg
    # failed to open" — opposite diagnoses that render identically without it.
    status = wl.session_status()
    assert status["push_to_talk_only"] is True
    assert status["wake_legs"] == []
    assert status["manual_mic_sources"] == ["wiim_remote_2"]

    # A speaker WITH a room mic reports the mode off.
    other = WakeLoop.for_tests(manual_mics=_remote_runtime())
    _prep_session_status(other)
    assert other.session_status()["push_to_talk_only"] is False
    # The other two consumers — run()'s keepalive branch and the source-less
    # start refusal — are pinned by
    # test_zero_leg_run_ticks_the_heartbeat_without_a_primary_mic below and by
    # test_source_less_refusal_reads_the_single_derivation in
    # tests/test_voice_daemon_manual_start_guard.py, both of which move when
    # this one field moves.


def test_zero_leg_wakeloop_has_no_primary_mic_or_detector():
    """The primary-leg aliases must tolerate the absent "on" leg. `_mic` is
    what run() branches on; `_capture_ring_on` must still be a real deque so
    its readers need no special case."""
    from collections import deque

    from jasper.voice_daemon import _ManualMicRuntime, WakeLoop

    wl = WakeLoop.for_tests(
        legs=[],
        manual_mics=[_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
    )
    assert wl._mic is None
    assert wl._detector is None
    assert isinstance(wl._capture_ring_on, deque)


def _daemon_heartbeat_stale_threshold() -> float:
    """The stale threshold the DAEMON actually runs with.

    Read from `jasper/voice/daemon_main.py`'s own `Heartbeat(...)` call, not
    from the constructor's signature default: those two happen to be the same
    number today, so a guard that read the signature would be correct only by
    coincidence and would keep passing if the daemon started asking for a
    tighter threshold. Parsed with `ast` rather than by line number so a
    refactor moves it for free (AGENTS.md documentation rule 5).
    """
    import ast
    import inspect
    from pathlib import Path

    import jasper
    from jasper.watchdog import Heartbeat

    source = (
        Path(jasper.__file__).parent / "voice" / "daemon_main.py"
    ).read_text(encoding="utf-8")
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Heartbeat"
    ]
    assert len(calls) == 1, (
        f"expected exactly one Heartbeat(...) construction in daemon_main.py, "
        f"found {len(calls)} — this guard must read the live one"
    )
    for kw in calls[0].keywords:
        if kw.arg == "stale_threshold_sec":
            return float(ast.literal_eval(kw.value))
    # No explicit value: the daemon runs on the constructor default.
    return float(
        inspect.signature(Heartbeat).parameters["stale_threshold_sec"].default
    )


def test_ptt_keepalive_stays_inside_heartbeat_stale_threshold():
    """Load-bearing relationship: with no mic frames to bump the progress
    sentinel, the keepalive tick IS the liveness proof. If its interval ever
    drifts past the threshold the daemon asks for, the heartbeat thread stops
    patting systemd and WatchdogSec=30s reaps a perfectly healthy daemon."""
    from jasper.voice_daemon import PTT_KEEPALIVE_INTERVAL_SEC

    stale = _daemon_heartbeat_stale_threshold()
    assert PTT_KEEPALIVE_INTERVAL_SEC < stale, (
        f"keepalive {PTT_KEEPALIVE_INTERVAL_SEC}s must stay under the "
        f"{stale}s heartbeat stale threshold jasper-voice constructs with"
    )


def _zero_leg_loop_with_fast_keepalive(monkeypatch):
    """A PTT-only WakeLoop whose keepalive iterates promptly, plus a
    heartbeat spy. The cadence itself is pinned by the threshold test above;
    here we only need the loop to turn over quickly."""
    import asyncio

    from jasper.voice_daemon import _ManualMicRuntime, WakeLoop

    class _IdleMic:
        """A paired remote with its button not pressed — the steady state.

        Sends nothing and never ends, which is exactly why frame flow cannot
        prove an accessory is alive (issue #2243)."""

        async def frames(self):
            await asyncio.Event().wait()
            yield b""  # unreachable; keeps this an async generator

    wl = WakeLoop.for_tests(
        legs=[],
        manual_mics=[
            _ManualMicRuntime("wiim_remote_2", _IdleMic(), "udp:9892"),
        ],
    )
    ticked = asyncio.Event()
    bumps = []

    class _Heartbeat:
        def bump(self):
            bumps.append(1)
            ticked.set()

    wl._heartbeat = _Heartbeat()

    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        # Still yields to the event loop — a bare `return None` would let the
        # keepalive spin without ever suspending and starve the test.
        await real_sleep(0)

    monkeypatch.setattr("jasper.voice_daemon.asyncio.sleep", _fast_sleep)
    return wl, ticked, bumps


async def test_zero_leg_run_ticks_the_heartbeat_without_a_primary_mic(
    monkeypatch, caplog,
):
    """run() must keep the Tier-1 heartbeat alive on a speaker with no
    primary mic, and must not mistake a tick for audio.

    Without the keepalive the heartbeat's progress sentinel never advances,
    the thread stops patting systemd, and WatchdogSec=30s restarts a daemon
    that is working exactly as designed.
    """
    import asyncio
    import logging

    wl, ticked, bumps = _zero_leg_loop_with_fast_keepalive(monkeypatch)
    # A list, not a deque: if a tick ever reached the frame body it would be
    # appended here and the assertion below would see it.
    wl._pre_roll = []

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        task = asyncio.create_task(wl.run())
        await asyncio.wait_for(ticked.wait(), timeout=2.0)
        wl._stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert bumps
    assert wl._pre_roll == []
    # The mode announces itself once, by this exact name: it is what the
    # owed #2205 hardware run greps for in the journal to confirm the box
    # came up push-to-talk rather than silently mic-less.
    assert "event=voice.push_to_talk_only" in caplog.text
    assert "sources=wiim_remote_2" in caplog.text


async def test_zero_leg_run_ends_an_in_flight_turn_on_stop(monkeypatch):
    """SIGTERM mid-hold must still tear the turn down.

    `run()`'s stop branch is what calls `_end_turn` — duck restore,
    `end_input`, turn telemetry, the done-listening chirp. When the keepalive
    generator carried its OWN `_stop_event` check it ended the iteration
    first, so that branch was unreachable on this path and a stop during a
    button hold left the music ducked and the turn unfinished. The generator
    ticks unconditionally now; the consumer owns shutdown, exactly as it does
    for a real mic's frames().
    """
    import asyncio

    from jasper.voice_daemon import State

    wl, ticked, _bumps = _zero_leg_loop_with_fast_keepalive(monkeypatch)
    ended = asyncio.Event()
    reasons: list[str] = []

    async def _end_turn(reason: str = "ended"):
        reasons.append(reason)
        ended.set()

    wl._end_turn = _end_turn
    wl._state = State.SESSION  # a button turn is in flight

    task = asyncio.create_task(wl.run())
    await asyncio.wait_for(ticked.wait(), timeout=2.0)
    wl._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert ended.is_set(), (
        "stop during an in-flight turn must reach run()'s _end_turn branch"
    )
    # The reason a shutdown teardown gives itself: a turn with no answer
    # owes no failure cue when the daemon is the one going away.
    assert reasons == ["stopping"]


async def test_zero_leg_run_does_not_end_a_turn_that_is_not_running(
    monkeypatch,
):
    """Control for the test above: idle at stop → no teardown, so the
    assertion there is about the SESSION branch and not about `run()`
    calling `_end_turn` unconditionally on every shutdown."""
    import asyncio

    wl, ticked, _bumps = _zero_leg_loop_with_fast_keepalive(monkeypatch)
    calls = []

    async def _end_turn(reason: str = "ended"):
        calls.append(reason)

    wl._end_turn = _end_turn

    task = asyncio.create_task(wl.run())
    await asyncio.wait_for(ticked.wait(), timeout=2.0)
    wl._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert calls == []
