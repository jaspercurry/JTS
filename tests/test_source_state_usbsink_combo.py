# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Combo-mode USB liveness primitives.

On a USB combo box (``JASPER_FANIN_USB_DIRECT=enabled``) jasper-fanin
DIRECT-captures the gadget and the jasper-usbsink bridge runs in standby. The
bridge's ``playing`` / ``rms_dbfs`` fields are frozen idle values; mux has to
infer liveness from fan-in's direct-lane counters.
"""
from __future__ import annotations

from jasper.mux import ComboLiveness, USBSINK_COMBO_STOP_TICKS, step_combo_liveness
from jasper.source_state import (
    usbsink_bridge_in_standby,
    usbsink_direct_frames_read,
)


def _fanin_status(source: str, *, frames: int = 0, resampler_frames=None, **extra):
    lane = {"label": "usbsink", "source": source, "frames_read": frames, **extra}
    if resampler_frames is not None:
        lane["resampler"] = {"input_frames": resampler_frames}
    return {
        "inputs": [
            {"label": "spotify", "source": "lane", "frames_read": 999},
            lane,
        ],
    }


def test_standby_flag_true_is_combo():
    assert usbsink_bridge_in_standby({"standby": True, "playing": False}) is True


def test_no_standby_flag_is_solo():
    assert usbsink_bridge_in_standby({"playing": True}) is False
    assert usbsink_bridge_in_standby({"standby": False, "playing": True}) is False


def test_standby_missing_or_bad_state_is_solo():
    assert usbsink_bridge_in_standby(None) is False
    assert usbsink_bridge_in_standby(["nope"]) is False  # type: ignore[arg-type]


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
        out.append(state.playing)
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
    assert state.prev_frames == 48_000 and state.playing is True
    state = step_combo_liveness(state, 100, stop_ticks=STOP)
    assert state.prev_frames == 100


def test_status_miss_keeps_prev_frames_baseline():
    state = ComboLiveness(prev_frames=48_000, idle_ticks=0, playing=True)
    state = step_combo_liveness(state, None, stop_ticks=STOP)
    assert state.prev_frames == 48_000


def test_advance_resets_idle_counter():
    state = ComboLiveness(prev_frames=48_000, idle_ticks=0, playing=True)
    state = step_combo_liveness(state, 48_000, stop_ticks=STOP)
    assert state.idle_ticks == 1 and state.playing is True
    state = step_combo_liveness(state, 96_000, stop_ticks=STOP)
    assert state.idle_ticks == 0 and state.playing is True
