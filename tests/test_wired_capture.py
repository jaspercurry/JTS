# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The wired capture engine's promises (#2662 W2b).

What is pinned, and why each pin exists:

* **Registry-anchored device resolution** — only a curated calibration-registry
  USB id with a capture stream resolves; a voice array (the real XVF3800 id)
  must NEVER select the wired source, and a playback-only device must not
  either.
* **Counter exactness** — the four frame-ledger counters come from real read
  accounting: an injected overrun changes ``block_gaps``/``block_gap_frames``
  by exactly the injected amount, and the clean path balances the ledger.
  These are the counters the host's screening ladder grades, so they are
  mutation-sensitive by design (drop the accounting and a test here names it).
* **Zero-run thresholds** — the browser's ≥128-exact-zeros dropout detector,
  re-homed: 127 is not a run, 128 is, offsets/lengths are exact, and the
  record cap keeps the count exact past it.
* **Format honesty** — S32 in, 32-bit PCM WAV out, byte-exact (no dither, no
  truncation, no resample anywhere in the engine).
* **The pre-roll guarantee** — ``start()`` does not return until real audio
  arrived, and fails loudly (before any excitation could play) when none does.
"""
from __future__ import annotations

import struct
import wave
import io

import pytest

from jasper.active_speaker.crossover_v2.capture_source import (
    INTEGRITY_COUNTER_KEYS,
)
from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_RENDER_GAPS,
    REPORT_KEY_RENDER_GAP_FRAMES,
    reconcile_capture_frames,
)
from jasper.audio_measurement.wired_capture import (
    WiredCaptureError,
    WiredRecorder,
    ZERO_RUN_MIN_SAMPLES,
    ZERO_RUN_RECORD_CAP,
    build_capture_integrity_report,
    encode_wav_s32,
    resolve_wired_mic,
    scan_zero_runs,
    select_capture_channel,
)
from jasper.mics.xvf3800 import USB_VID_PID as XVF_USB_VID_PID
from tests.wired_capture_fixtures import FakePcm

UMIK2_USB_ID = "2752:002b"


# --------------------------------------------------------------------------- #
# device resolution
# --------------------------------------------------------------------------- #


def _make_card(root, index, *, usbid=None, card_id=None, capture=True):
    card = root / f"card{index}"
    card.mkdir(parents=True)
    if usbid is not None:
        (card / "usbid").write_text(usbid + "\n")
    if card_id is not None:
        (card / "id").write_text(card_id + "\n")
    if capture:
        (card / "pcm0c").mkdir()
    return card


def test_resolves_a_umik2_by_registry_usb_id(tmp_path):
    _make_card(tmp_path, 0, usbid="1d50:0000", card_id="Other")
    _make_card(tmp_path, 2, usbid=UMIK2_USB_ID, card_id="UMIK2")
    device = resolve_wired_mic(proc_asound=tmp_path)
    assert device is not None
    assert device.card_id == "UMIK2"
    assert device.card_index == 2
    assert device.usb_id == UMIK2_USB_ID
    assert device.model_key == "minidsp_umik2"
    assert device.pcm == "hw:CARD=UMIK2,DEV=0"


def test_the_usb_id_match_is_case_insensitive(tmp_path):
    # The kernel writes lowercase hex; a registry entry or a future kernel
    # spelling difference must not break the match.
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID.upper(), card_id="UMIK2")
    device = resolve_wired_mic(proc_asound=tmp_path)
    assert device is not None and device.model_key == "minidsp_umik2"


def test_a_voice_array_never_resolves(tmp_path):
    """The sharpest negative: the real XVF3800 is a USB CAPTURE card, and it
    must never be mistaken for a measurement microphone. Anchored to the
    mic module's own VID:PID constant, not a re-spelling."""
    _make_card(tmp_path, 0, usbid=XVF_USB_VID_PID, card_id="Array")
    assert resolve_wired_mic(proc_asound=tmp_path) is None


def test_a_playback_only_device_never_resolves(tmp_path):
    _make_card(
        tmp_path, 0, usbid=UMIK2_USB_ID, card_id="Weird", capture=False
    )
    assert resolve_wired_mic(proc_asound=tmp_path) is None


def test_no_cards_resolves_none(tmp_path):
    assert resolve_wired_mic(proc_asound=tmp_path) is None
    assert resolve_wired_mic(proc_asound=tmp_path / "missing") is None


def test_non_usb_cards_are_skipped(tmp_path):
    # An I2S DAC has no usbid file at all; the probe must skip it, not die.
    _make_card(tmp_path, 0, card_id="sndrpihifiberry")
    _make_card(tmp_path, 1, usbid=UMIK2_USB_ID, card_id="UMIK2")
    device = resolve_wired_mic(proc_asound=tmp_path)
    assert device is not None and device.card_index == 1


# --------------------------------------------------------------------------- #
# the recorder + counters (fake ALSA)
# --------------------------------------------------------------------------- #

RATE = 48_000
CHANNELS = 2


class FakeClock:
    """Monotonic-ns clock the test advances explicitly per read."""

    def __init__(self, step_ns):
        self._now = 0
        self._step = step_ns

    def __call__(self):
        self._now += self._step
        return self._now


def _record(script, *, max_capture_s=10.0, clock_ns=None, tail_s=0.0):
    recorder = WiredRecorder(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=max_capture_s,
        pcm_factory=lambda: FakePcm(script),
        clock_ns=clock_ns or FakeClock(1_000_000),  # 1 ms per read
    )
    recorder.start(ready_timeout_s=5.0)
    return recorder.finish(tail_s=tail_s)


def test_clean_capture_counts_exactly_and_balances():
    frames = [(i, -i) for i in range(1, 97)]
    recording = _record([(32, frames[:32]), (32, frames[32:64]), (32, frames[64:])])
    # The reader's own accumulation is exact...
    assert recording.frames >= 96  # idle silence may extend it
    assert recording.gap_count == 0
    assert recording.gap_frames == 0
    assert recording.truncated is False
    # ...and the report built from it balances the REAL ledger.
    _channel, mono, _rms = select_capture_channel(recording)
    wav, encoded = encode_wav_s32(mono, sample_rate_hz=RATE)
    report = build_capture_integrity_report(
        recording, encoded_frames=encoded,
        zero_run_count=0, zero_runs=[],
    )
    ledger = reconcile_capture_frames(report, received_frames=encoded)
    assert ledger.balanced
    assert ledger.render_gap_evaluated  # reported, never "not evaluated"
    assert ledger.render_gap_frames == 0


def test_an_injected_overrun_books_exactly_one_gap_with_clock_frames():
    # 1 ms clock step per read; the overrun's window is one step, so the
    # clock-derived loss estimate is exactly 48 frames (1 ms at 48 kHz).
    good = [(0, 0)] * 32
    recording = _record([(32, good), "overrun", (32, good)])
    assert recording.gap_count == 1
    assert recording.gap_frames == 48
    report = build_capture_integrity_report(
        recording, encoded_frames=recording.frames,
        zero_run_count=0, zero_runs=[],
    )
    assert report[REPORT_KEY_RENDER_GAPS] == 1
    assert report[REPORT_KEY_RENDER_GAP_FRAMES] == 48
    # The ledger grades it exactly as a browser render gap: FAIL material.
    ledger = reconcile_capture_frames(report, received_frames=recording.frames)
    assert ledger.render_gap_frames == 48
    assert "render_graph" in ledger.lost_at


def test_two_overruns_book_two_gaps():
    good = [(0, 0)] * 32
    recording = _record([(32, good), "overrun", (16, good[:16]), "overrun", (32, good)])
    assert recording.gap_count == 2
    assert recording.gap_frames == 96  # two 1 ms windows at 48 kHz


def test_gap_frames_floor_is_one_even_on_a_frozen_clock():
    # A clock that does not advance across the overrun still books ≥1 frame:
    # any nonzero fails identically, and a detected loss must never round to
    # "nothing lost".
    class FrozenClock:
        def __call__(self):
            return 7_000_000

    good = [(0, 0)] * 32
    recording = _record(
        [(32, good), "overrun", (32, good)], clock_ns=FrozenClock()
    )
    assert recording.gap_count == 1
    assert recording.gap_frames == 1


def test_an_empty_read_counts_as_a_gap():
    good = [(1, 1)] * 32
    recording = _record([(32, good), "empty", (32, good)])
    assert recording.gap_count == 1


def test_persistent_read_failure_raises_loudly():
    with pytest.raises(WiredCaptureError, match="consecutive reads"):
        _record([(32, [(1, 1)] * 32)] + ["overrun"] * 64)


def test_start_fails_loudly_when_no_audio_arrives():
    # A dead-but-open device BLOCKS its reads (real blocking ALSA capture
    # with no data), so nothing arrives and no read returns: start()'s own
    # ready timeout is the guard that fires. (A device returning rapid
    # empties fails through the consecutive-failures guard instead —
    # test_persistent_read_failure_raises_loudly — either way loud, before
    # any excitation plays.)
    class DeadPcm:
        def read(self):
            import time as _time

            _time.sleep(0.2)
            return 0, b""

        def close(self):
            pass

    recorder = WiredRecorder(
        "fake:dead",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=1.0,
        pcm_factory=DeadPcm,
    )
    with pytest.raises(WiredCaptureError, match="not delivering samples"):
        recorder.start(ready_timeout_s=0.05)


def test_start_confirms_audio_before_returning():
    """The pre-roll guarantee: by the time start() returns, at least one real
    chunk is already banked — playback beginning after start() cannot outrun
    the recorder."""
    recorder = WiredRecorder(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=10.0,
        pcm_factory=lambda: FakePcm([(32, [(5, 5)] * 32)]),
    )
    recorder.start(ready_timeout_s=5.0)
    assert recorder._frames >= 32
    recorder.abort()


def test_budget_stops_the_reader_and_a_truncated_take_fails_the_ladder():
    """S4 (#2720 gate): truncation is graded, never a bare disclosure — a
    truncated take is a splice by another name, so it books a discontinuity
    and FAILS the same render-gap check an overrun fails."""
    recording = _record(
        [(4800, [(1, 1)] * 4800), (4800, [(1, 1)] * 4800)],
        max_capture_s=0.1,  # 4,800 frames at 48 kHz
    )
    assert recording.truncated is True
    assert recording.frames == 4800
    assert recording.gap_count == 1
    assert recording.gap_frames >= 1
    report = build_capture_integrity_report(
        recording, encoded_frames=recording.frames,
        zero_run_count=0, zero_runs=[],
    )
    assert report["truncated"] is True
    ledger = reconcile_capture_frames(report, received_frames=recording.frames)
    assert ledger.render_gap_evaluated
    assert ledger.render_gap_frames >= 1  # FAIL material — never clean
    assert "render_graph" in ledger.lost_at


def test_open_failure_raises_wired_capture_error():
    def _factory():
        raise WiredCaptureError("could not open fake:pcm")

    recorder = WiredRecorder(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=1.0,
        pcm_factory=_factory,
    )
    with pytest.raises(WiredCaptureError):
        recorder.start()


# --------------------------------------------------------------------------- #
# channel selection
# --------------------------------------------------------------------------- #


def test_selects_the_channel_carrying_signal():
    loud = 1 << 24
    recording = _record([(64, [(0, loud)] * 64)])
    channel, mono, rms = select_capture_channel(recording)
    assert channel == 1
    assert rms[1] > (rms[0] if rms[0] == rms[0] else -999)  # ch1 louder
    assert int(mono[0]) == loud


def test_channel_tie_resolves_to_zero():
    recording = _record([(64, [(3, 3)] * 64)])
    channel, _mono, _rms = select_capture_channel(recording)
    assert channel == 0


# --------------------------------------------------------------------------- #
# zero-run scan (the re-homed #2557 detector)
# --------------------------------------------------------------------------- #


def _mono(values):
    import numpy as np

    return np.asarray(values, dtype="<i4")


def test_zero_run_below_threshold_is_not_counted():
    samples = _mono([7] + [0] * (ZERO_RUN_MIN_SAMPLES - 1) + [7])
    count, runs = scan_zero_runs(samples)
    assert count == 0
    assert runs == []


def test_zero_run_at_threshold_is_counted_with_exact_offset_and_length():
    samples = _mono([7, 7] + [0] * ZERO_RUN_MIN_SAMPLES + [7])
    count, runs = scan_zero_runs(samples)
    assert count == 1
    assert runs == [{"offset": 2, "len": ZERO_RUN_MIN_SAMPLES}]


def test_a_trailing_run_is_closed_at_the_buffer_end():
    samples = _mono([7] + [0] * 200)
    count, runs = scan_zero_runs(samples)
    assert count == 1
    assert runs == [{"offset": 1, "len": 200}]


def test_zero_run_count_stays_exact_past_the_record_cap():
    pieces = []
    for _ in range(ZERO_RUN_RECORD_CAP + 3):
        pieces.extend([1])
        pieces.extend([0] * ZERO_RUN_MIN_SAMPLES)
    count, runs = scan_zero_runs(_mono(pieces))
    assert count == ZERO_RUN_RECORD_CAP + 3
    assert len(runs) == ZERO_RUN_RECORD_CAP


def test_all_signal_has_no_runs():
    count, runs = scan_zero_runs(_mono([1, -1] * 1000))
    assert count == 0 and runs == []


# --------------------------------------------------------------------------- #
# WAV encode — byte-exact, format-preserving
# --------------------------------------------------------------------------- #


def test_encode_wav_s32_is_byte_exact():
    values = [0, 1, -1, 2**31 - 1, -(2**31), 48_000]
    wav, encoded = encode_wav_s32(_mono(values), sample_rate_hz=RATE)
    assert encoded == len(values)
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 4  # 32-bit PCM — no width conversion
        assert reader.getframerate() == RATE
        payload = reader.readframes(reader.getnframes())
    assert payload == struct.pack(f"<{len(values)}i", *values)


def test_encoded_frames_is_counted_from_what_was_written():
    """The independent count: encode counts ITS OWN output, so a frame lost
    between read-accumulation and encode unbalances the ledger instead of
    vanishing (drop one sample and the report fails the frame_ledger check).
    """
    values = _mono([5] * 100)
    _wav, encoded = encode_wav_s32(values[:99], sample_rate_hz=RATE)
    report = {
        REPORT_KEY_FRAMES: 100,
        REPORT_KEY_ENCODED_FRAMES: encoded,
        REPORT_KEY_RENDER_GAPS: 0,
        REPORT_KEY_RENDER_GAP_FRAMES: 0,
    }
    ledger = reconcile_capture_frames(report, received_frames=encoded)
    assert not ledger.balanced
    assert "worklet->encoder" in ledger.lost_at


# --------------------------------------------------------------------------- #
# the integrity report — the seam's wire spelling
# --------------------------------------------------------------------------- #


def test_report_carries_every_seam_counter_key_and_the_zero_run_keys():
    recording = _record([(32, [(1, 1)] * 32)])
    report = build_capture_integrity_report(
        recording,
        encoded_frames=recording.frames,
        zero_run_count=2,
        zero_runs=[{"offset": 0, "len": 128}],
    )
    for key in INTEGRITY_COUNTER_KEYS:
        assert key in report, key
        assert isinstance(report[key], int)
    assert report["zero_run_count"] == 2
    assert report["zero_runs"] == [{"offset": 0, "len": 128}]
    assert report["zero_run_quantum"] == ZERO_RUN_MIN_SAMPLES
    assert report["capture_chain"] == "alsa_s32le"
    # Truncation is reported only when it happened (absence is the clean
    # state, mirroring the page's convention).
    assert "truncated" not in report


def test_report_keeps_the_two_frame_counts_independent():
    """``frames`` is the reader's accumulator, ``encoded_frames`` the
    encoder's own count — the report must never derive one from the other,
    or a frame lost between read and encode vanishes instead of unbalancing
    the ledger."""
    recording = _record([(32, [(1, 1)] * 32)])
    report = build_capture_integrity_report(
        recording, encoded_frames=recording.frames - 1,
        zero_run_count=0, zero_runs=[],
    )
    assert report[REPORT_KEY_FRAMES] == recording.frames
    assert report[REPORT_KEY_ENCODED_FRAMES] == recording.frames - 1
    ledger = reconcile_capture_frames(
        report, received_frames=recording.frames - 1
    )
    assert not ledger.balanced
    assert "worklet->encoder" in ledger.lost_at


def test_report_counters_are_the_recorders_own_numbers():
    recording = _record([(32, [(1, 1)] * 32), "overrun", (32, [(1, 1)] * 32)])
    report = build_capture_integrity_report(
        recording, encoded_frames=recording.frames,
        zero_run_count=0, zero_runs=[],
    )
    assert report[REPORT_KEY_FRAMES] == recording.frames
    assert report[REPORT_KEY_ENCODED_FRAMES] == recording.frames
    assert report[REPORT_KEY_RENDER_GAPS] == recording.gap_count == 1
    assert report[REPORT_KEY_RENDER_GAP_FRAMES] == recording.gap_frames


# --------------------------------------------------------------------------- #
# post-roll (finish grants the tail)
# --------------------------------------------------------------------------- #


def test_finish_grants_the_post_roll_tail():
    """finish(tail_s) keeps the reader alive for the tail window, so audio
    still in flight after the play call returns lands in the capture."""
    recorder = WiredRecorder(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=60.0,
        pcm_factory=lambda: FakePcm([(32, [(1, 1)] * 32)], idle_frames=64),
    )
    recorder.start(ready_timeout_s=5.0)
    at_play_end = recorder._frames
    recording = recorder.finish(tail_s=0.05)
    assert recording.frames > at_play_end


def test_abort_is_idempotent_and_closes_the_pcm():
    pcm = FakePcm([(32, [(1, 1)] * 32)])
    recorder = WiredRecorder(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=1.0,
        pcm_factory=lambda: pcm,
    )
    recorder.start(ready_timeout_s=5.0)
    recorder.abort()
    recorder.abort()
    assert pcm.closed is True
