# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Combo-mode USB liveness primitives.

On a USB combo box (``JASPER_FANIN_USB_DIRECT=enabled``) jasper-fanin is the
sole live ingress owner and DIRECT-captures the gadget. Mux infers temporal
liveness from fan-in's direct-lane telemetry.
"""
from __future__ import annotations

from jasper.mux import ComboLiveness, USBSINK_COMBO_STOP_TICKS, step_combo_liveness
from jasper.source_state import (
    USBSINK_PLAYING_RMS_DBFS,
    usbsink_direct_audible,
    usbsink_direct_frames_read,
    usbsink_direct_muted,
    usbsink_direct_playing,
    usbsink_direct_rms_dbfs,
)


def _fanin_status(
    source: str, *, frames: int = 0, resampler_frames=None, rms_dbfs=None, **extra,
):
    lane = {"label": "usbsink", "source": source, "frames_read": frames, **extra}
    if resampler_frames is not None:
        lane["resampler"] = {"input_frames": resampler_frames}
    if rms_dbfs is not None:
        lane["rms_dbfs"] = rms_dbfs
    return {
        "inputs": [
            {"label": "spotify", "source": "lane", "frames_read": 999},
            lane,
        ],
    }


def test_direct_playing_requires_capturing_health_and_audible_level():
    assert (
        usbsink_direct_playing(
            _fanin_status(
                "direct",
                rms_dbfs=-12.0,
                direct={"health": "capturing"},
            ),
        )
        is True
    )
    assert (
        usbsink_direct_playing(
            _fanin_status(
                "direct",
                rms_dbfs=-90.0,
                direct={"health": "capturing"},
            ),
        )
        is False
    )
    assert (
        usbsink_direct_playing(
            _fanin_status(
                "direct",
                rms_dbfs=-12.0,
                direct={"health": "waiting"},
            ),
        )
        is False
    )


def test_direct_playing_older_snapshot_falls_back_to_audibility():
    assert usbsink_direct_playing(_fanin_status("direct", rms_dbfs=-12.0)) is True
    assert usbsink_direct_playing(_fanin_status("direct", rms_dbfs=-90.0)) is False
    assert usbsink_direct_playing(_fanin_status("direct")) is False
    assert usbsink_direct_playing(_fanin_status("lane", rms_dbfs=-12.0)) is None
    assert usbsink_direct_playing(None) is None


def test_direct_liveness_prefers_resampler_input_frames():
    """Real direct-mode bug shape: lane frames_read can freeze at zero."""
    status = _fanin_status("direct", frames=0, resampler_frames=1_137_920)
    assert usbsink_direct_frames_read(status) == 1_137_920


def test_direct_liveness_falls_back_to_lane_frames_read():
    assert usbsink_direct_frames_read(_fanin_status("direct", frames=60_768)) == 60_768


def test_direct_liveness_none_for_aloop_lane():
    assert (
        usbsink_direct_frames_read(
            _fanin_status("lane", frames=60_768, resampler_frames=60_768),
        )
        is None
    )


def test_direct_liveness_none_when_status_unavailable_or_no_lane():
    assert usbsink_direct_frames_read(None) is None
    assert usbsink_direct_frames_read({}) is None
    assert usbsink_direct_frames_read({"inputs": []}) is None
    assert usbsink_direct_frames_read({"inputs": "nope"}) is None


def test_direct_liveness_rejects_non_int_bool_and_negative():
    assert (
        usbsink_direct_frames_read(
            _fanin_status("direct", frames=12, resampler_frames=True),
        )
        == 12
    )
    status = {
        "inputs": [
            {
                "label": "usbsink",
                "source": "direct",
                "frames_read": "12",
                "resampler": {"input_frames": "12"},
            },
        ],
    }
    assert usbsink_direct_frames_read(status) is None
    assert (
        usbsink_direct_frames_read(_fanin_status("direct", frames=-1))
        is None
    )


def test_direct_liveness_zero_is_returned_not_none():
    assert usbsink_direct_frames_read(_fanin_status("direct", frames=0)) == 0
    assert (
        usbsink_direct_frames_read(
            _fanin_status("direct", frames=99, resampler_frames=0),
        )
        == 0
    )


STOP = USBSINK_COMBO_STOP_TICKS


def _run(frames_seq, *, start=ComboLiveness(), stop_ticks=STOP):
    state = start
    out = []
    for frames in frames_seq:
        state = step_combo_liveness(state, frames, stop_ticks=stop_ticks)
        out.append(state.streaming)
    return out


def test_first_reading_is_not_playing():
    assert _run([48_000]) == [False]


def test_advancing_frames_flip_playing_on_the_second_tick():
    assert _run([0, 48_000, 96_000]) == [False, True, True]


def test_flat_frames_never_play():
    assert _run([0, 0, 0, 0]) == [False, False, False, False]


def test_stop_is_debounced_by_stop_ticks_flat_readings():
    seq = [0, 48_000] + [48_000] * STOP
    verdicts = _run(seq)
    assert verdicts[:2] == [False, True]
    assert verdicts[2 : 2 + STOP - 1] == [True] * (STOP - 1)
    assert verdicts[-1] is False


def test_single_status_miss_does_not_drop_a_live_winner():
    assert STOP >= 2
    assert _run([0, 48_000, None, 96_000]) == [False, True, True, True]


def test_two_consecutive_misses_drop_after_debounce():
    assert _run([0, 48_000] + [None] * STOP)[-1] is False


def test_counter_reset_rebaselines_without_spurious_advance():
    assert _run([0, 48_000, 100, 48_100]) == [False, True, True, True]


def test_reset_state_baseline_is_the_new_low_value():
    state = ComboLiveness()
    for frames in (0, 48_000):
        state = step_combo_liveness(state, frames, stop_ticks=STOP)
    assert state.prev_frames == 48_000 and state.streaming is True
    state = step_combo_liveness(state, 100, stop_ticks=STOP)
    assert state.prev_frames == 100


def test_status_miss_keeps_prev_frames_baseline():
    state = ComboLiveness(prev_frames=48_000, idle_ticks=0, streaming=True)
    state = step_combo_liveness(state, None, stop_ticks=STOP)
    assert state.prev_frames == 48_000


def test_advance_resets_idle_counter():
    state = ComboLiveness(prev_frames=48_000, idle_ticks=0, streaming=True)
    state = step_combo_liveness(state, 48_000, stop_ticks=STOP)
    assert state.idle_ticks == 1 and state.streaming is True
    state = step_combo_liveness(state, 96_000, stop_ticks=STOP)
    assert state.idle_ticks == 0 and state.streaming is True


# ---- Per-lane level readers -------------------------------------------------


def test_direct_rms_reads_the_direct_lane_level():
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs=-6.5)) == -6.5


def test_direct_rms_none_for_aloop_lane():
    assert usbsink_direct_rms_dbfs(_fanin_status("lane", rms_dbfs=-6.5)) is None


def test_direct_rms_none_when_missing_or_non_numeric():
    assert usbsink_direct_rms_dbfs(_fanin_status("direct")) is None
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs="loud")) is None
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs=True)) is None
    assert usbsink_direct_rms_dbfs(_fanin_status("direct", rms_dbfs=float("-inf"))) is None
    assert usbsink_direct_rms_dbfs(None) is None


def test_direct_audible_gates_on_the_shared_threshold():
    assert usbsink_direct_audible(_fanin_status("direct", rms_dbfs=-12.0)) is True
    assert usbsink_direct_audible(_fanin_status("direct", rms_dbfs=-90.0)) is False
    # Exactly at the gate is NOT audible (strict >), matching the solo bridge.
    assert (
        usbsink_direct_audible(
            _fanin_status("direct", rms_dbfs=USBSINK_PLAYING_RMS_DBFS),
        )
        is False
    )
    # No level / no direct lane -> None (caller picks the fail-soft direction).
    assert usbsink_direct_audible(_fanin_status("direct")) is None
    assert usbsink_direct_audible(_fanin_status("lane", rms_dbfs=-6.0)) is None


# ---- Direct-lane MIX-MUTE state (mux combo arbitration) ---------------------


def test_direct_muted_reads_the_direct_lane_flag():
    assert usbsink_direct_muted(_fanin_status("direct", muted=True)) is True
    assert usbsink_direct_muted(_fanin_status("direct", muted=False)) is False


def test_direct_muted_none_for_non_direct_lane():
    # The fan-in mix mute is only meaningful on the DIRECT-capture lane; a
    # non-direct ("lane") usbsink input (USB Audio off / idle fallback) returns
    # None from the direct-only reader.
    assert usbsink_direct_muted(_fanin_status("lane", muted=True)) is None


def test_direct_muted_none_when_absent_or_non_bool():
    # Older fan-in (no per-lane `muted` key) or a malformed value → None, the
    # fail-soft "unknown" the state surface renders as null.
    assert usbsink_direct_muted(_fanin_status("direct")) is None
    assert usbsink_direct_muted(_fanin_status("direct", muted="yes")) is None
    assert usbsink_direct_muted(_fanin_status("direct", muted=1)) is None
    assert usbsink_direct_muted(None) is None
    assert usbsink_direct_muted({"inputs": "nope"}) is None


# ---- Combo liveness is frames-only (no audio-level gate) --------------------

# The old "frames-advanced AND audible (rms > -60)" gate was removed with the
# sticky-session rework (2026-07-17). USB liveness is now purely "is the host
# streaming frames to us" — a faint sound streams frames just like a loud one,
# and USB wins whenever it streams and no explicit session is active (the
# arbiter, not the level, keeps a silently-streaming host from stealing a cast).
# The per-lane rms readers below still exist for /state telemetry, but no longer
# gate liveness. See jasper.mux.step_combo_liveness and docs/HANDOFF-usbsink.md.


def test_step_combo_liveness_takes_no_level_argument():
    """The level gate is gone: step_combo_liveness ignores audio level entirely.

    Pins the removal so a future edit can't quietly reintroduce an rms kwarg and
    re-gate faint audio out of the pipeline."""
    import inspect

    params = inspect.signature(step_combo_liveness).parameters
    assert "rms_dbfs" not in params
    assert "rms_threshold_dbfs" not in params


def test_faint_streaming_wins_like_loud_streaming():
    """A near-silent host that keeps the counter advancing is 'streaming' —
    identical to a loud one. This is the faint-audio fix: the level never
    factors into liveness, so quiet content is not gated out."""
    assert _run([0, 48_000, 96_000]) == [False, True, True]
