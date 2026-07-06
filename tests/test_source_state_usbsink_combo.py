# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Combo-mode USB liveness primitives.

On a USB *combo* box (``JASPER_FANIN_USB_DIRECT=enabled``) jasper-fanin
DIRECT-captures the gadget and the jasper-usbsink bridge runs in standby: it
opens no PCM, so its published ``playing`` / ``rms_dbfs`` are frozen idle
defaults that describe nothing. The live audio flows through fan-in's DIRECT
lane, whose only liveness signal is a cumulative ``frames_read`` counter.

These tests pin:
  * ``usbsink_bridge_in_standby`` — the cheap combo gate off the bridge's own
    ``standby`` flag (mirrors PR #1177's ``_usbsink_in_combo_mode`` fallback).
  * ``usbsink_direct_frames_read`` — the DIRECT-lane frames extractor (returns
    the counter only when ``source == "direct"``).
  * ``jasper.mux.step_combo_liveness`` — the pure frames-delta debounce the mux
    tick runs (fast start, debounced stop, reset re-baseline, STATUS-miss hold).
"""
from __future__ import annotations

from jasper.mux import ComboLiveness, USBSINK_COMBO_STOP_TICKS, step_combo_liveness
from jasper.source_state import (
    usbsink_bridge_in_standby,
    usbsink_direct_frames_read,
)


def _fanin_status(source: str, *, frames: int = 0, **extra):
    """A fan-in STATUS snapshot with one non-USB lane plus the usbsink lane."""
    return {
        "inputs": [
            {"label": "spotify", "source": "lane", "frames_read": 999},
            {"label": "usbsink", "source": source, "frames_read": frames, **extra},
        ]
    }


# --------------------------------------------------------------------------
# usbsink_bridge_in_standby — the combo gate
# --------------------------------------------------------------------------


def test_standby_flag_true_is_combo():
    assert usbsink_bridge_in_standby({"standby": True, "playing": False}) is True


def test_no_standby_flag_is_solo():
    # A solo box's bridge omits standby (or writes it false) — its RMS-gated
    # `playing` is the truth, so this must NOT be treated as combo.
    assert usbsink_bridge_in_standby({"playing": True}) is False
    assert usbsink_bridge_in_standby({"standby": False, "playing": True}) is False


def test_standby_missing_or_bad_state_is_solo():
    # Feature off (no state file) / a non-dict root must fail closed to solo.
    assert usbsink_bridge_in_standby(None) is False
    assert usbsink_bridge_in_standby(["nope"]) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# usbsink_direct_frames_read — the DIRECT-lane frames extractor
# --------------------------------------------------------------------------


def test_frames_read_from_direct_lane():
    assert usbsink_direct_frames_read(_fanin_status("direct", frames=60768)) == 60768


def test_frames_read_none_for_aloop_lane():
    # source=="lane" is a solo/aloop lane — not combo; the extractor returns
    # None so mux falls back to the bridge's own RMS-gated flag.
    assert usbsink_direct_frames_read(_fanin_status("lane", frames=60768)) is None


def test_frames_read_none_when_status_unavailable_or_no_lane():
    assert usbsink_direct_frames_read(None) is None
    assert usbsink_direct_frames_read({}) is None
    assert usbsink_direct_frames_read({"inputs": []}) is None
    assert usbsink_direct_frames_read({"inputs": "nope"}) is None


def test_frames_read_rejects_non_int_and_bool():
    # A bool is an int subclass — must be rejected (the counter is a u64).
    assert usbsink_direct_frames_read(_fanin_status("direct", frames=True)) is None  # type: ignore[arg-type]
    status = {"inputs": [{"label": "usbsink", "source": "direct", "frames_read": "12"}]}
    assert usbsink_direct_frames_read(status) is None
    status_missing = {"inputs": [{"label": "usbsink", "source": "direct"}]}
    assert usbsink_direct_frames_read(status_missing) is None


def test_frames_read_zero_is_returned_not_none():
    # frames_read==0 (host connected, PCM open, but no host clocking yet — the
    # live jts.local idle state) is a real reading, not "unavailable".
    assert usbsink_direct_frames_read(_fanin_status("direct", frames=0)) == 0


# --------------------------------------------------------------------------
# step_combo_liveness — the pure frames-delta debounce
# --------------------------------------------------------------------------

STOP = USBSINK_COMBO_STOP_TICKS


def _run(frames_seq, *, start=ComboLiveness(), stop_ticks=STOP):
    """Fold a sequence of frames readings through the stepper, returning the
    list of `playing` verdicts (one per tick)."""
    state = start
    out = []
    for frames in frames_seq:
        state = step_combo_liveness(state, frames, stop_ticks=stop_ticks)
        out.append(state.playing)
    return out


def test_first_reading_is_not_playing():
    # One sample can't establish a delta — needs a second, advancing tick.
    assert _run([48000]) == [False]


def test_advancing_frames_flip_playing_on_the_second_tick():
    # 0 -> 48000 -> 96000: fast start on the first ADVANCING tick.
    assert _run([0, 48000, 96000]) == [False, True, True]


def test_flat_frames_never_play():
    # Host connected, PCM open, but no frames flowing (idle) → never playing.
    assert _run([0, 0, 0, 0]) == [False, False, False, False]


def test_stop_is_debounced_by_stop_ticks_flat_readings():
    # Playing, then the counter goes flat: hold for (STOP-1) flat ticks, drop on
    # the STOP-th. Guards against an audible dropout from one transient miss.
    seq = [0, 48000] + [48000] * STOP
    verdicts = _run(seq)
    assert verdicts[:2] == [False, True]
    # First STOP-1 flat ticks still playing; the STOP-th flat tick drops it.
    assert verdicts[2 : 2 + STOP - 1] == [True] * (STOP - 1)
    assert verdicts[-1] is False


def test_single_status_miss_does_not_drop_a_live_winner():
    # A single None reading (fan-in STATUS unreachable this tick) must NOT drop
    # USB when STOP >= 2 — it counts as one non-advancing tick, then recovers.
    assert STOP >= 2
    assert _run([0, 48000, None, 96000]) == [False, True, True, True]


def test_two_consecutive_misses_drop_after_debounce():
    verdicts = _run([0, 48000] + [None] * STOP)
    assert verdicts[-1] is False


def test_counter_reset_rebaselines_without_spurious_advance():
    # fan-in restarted: frames_read resets to a small value. A raw `frames >
    # prev` would suppress detection until the counter re-climbed past the stale
    # high value (minutes). The stepper re-baselines instead, so the next
    # advancing tick from the new baseline is detected promptly.
    # 0 -> 48000 (playing) -> reset to 100 (< prev; re-baseline, 1 idle tick) ->
    # 48100 (> new baseline 100 -> advancing -> still playing).
    assert _run([0, 48000, 100, 48100]) == [False, True, True, True]


def test_reset_state_baseline_is_the_new_low_value():
    state = ComboLiveness()
    for frames in (0, 48000):
        state = step_combo_liveness(state, frames, stop_ticks=STOP)
    assert state.prev_frames == 48000 and state.playing is True
    # A reset to a lower value re-baselines prev_frames to that low value.
    state = step_combo_liveness(state, 100, stop_ticks=STOP)
    assert state.prev_frames == 100


def test_status_miss_keeps_prev_frames_baseline():
    # A None reading must not corrupt the baseline (keeps prev_frames) so the
    # next real advancing reading is still comparable.
    state = ComboLiveness(prev_frames=48000, idle_ticks=0, playing=True)
    state = step_combo_liveness(state, None, stop_ticks=STOP)
    assert state.prev_frames == 48000


def test_advance_resets_idle_counter():
    # A flat tick then an advance must reset idle_ticks so the debounce restarts.
    state = ComboLiveness(prev_frames=48000, idle_ticks=0, playing=True)
    state = step_combo_liveness(state, 48000, stop_ticks=STOP)  # flat -> idle=1
    assert state.idle_ticks == 1 and state.playing is True
    state = step_combo_liveness(state, 96000, stop_ticks=STOP)  # advance
    assert state.idle_ticks == 0 and state.playing is True
