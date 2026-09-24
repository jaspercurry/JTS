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
  accounting: an injected overrun changes ``capture_gaps``/``capture_gap_frames``
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
* **The ONE answer mint** — every wired take in the product (the wizard's plan
  walk, the engine's play seam) becomes a
  ``WiredCaptureAnswer`` here, so the device block, the calibration reference
  and the integrity counters cannot differ by which door recorded it.
"""
from __future__ import annotations

import asyncio
import io
import math
import struct
import sys
import threading
import time
import wave
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.capture_source import (
    INTEGRITY_COUNTER_KEYS,
)
from jasper.active_speaker.crossover_v2.program_transaction import StimulusCaptureStopped
from jasper.active_speaker.crossover_v2 import wired_stimulus
from jasper.active_speaker.crossover_v2.wired_stimulus import WiredStimulusCapture
from jasper.audio_measurement.ramp import SPL_CEILING_EXCEEDED
from jasper.audio_measurement.frame_ledger import (
    REPORT_KEY_ENCODED_FRAMES,
    REPORT_KEY_FRAMES,
    REPORT_KEY_CAPTURE_GAPS,
    REPORT_KEY_CAPTURE_GAP_FRAMES,
    reconcile_capture_frames,
)
from jasper.audio_measurement.wired_capture import (
    CAPTURE_RING_PERIODS,
    CODE_WIRED_MIC_MISSING,
    MAX_CONSECUTIVE_READ_FAILURES,
    SPL_BATCH_S,
    WiredCaptureAnswer,
    WiredCaptureError,
    WiredMicDevice,
    WiredMicMissing,
    WiredRecorder,
    WiredSplCeilingExceeded,
    WiredSplMonitor,
    ZERO_RUN_MIN_SAMPLES,
    ZERO_RUN_RECORD_CAP,
    build_capture_integrity_report,
    decode_wav_to_mono,
    encode_wav_s32,
    make_wired_recorder,
    mint_wired_answer,
    require_wired_mic,
    resolve_wired_mic,
    scan_zero_runs,
    select_capture_channel,
    setup_from_hint,
)
from jasper.mics.xvf3800 import USB_VID_PID as XVF_USB_VID_PID
from tests._log_events import event_fields
from tests.wired_capture_fixtures import FakePcm

UMIK2_USB_ID = "2752:002b"


class _Sensitivity:
    def db_spl_from_dbfs(self, dbfs):
        return dbfs + 100.0


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


def _record(script, *, max_capture_s=10.0, clock_ns=time.monotonic_ns, tail_s=0.0):
    recorder = WiredRecorder(
        "fake:pcm",
        sample_rate_hz=RATE,
        channels=CHANNELS,
        max_capture_s=max_capture_s,
        pcm_factory=lambda: FakePcm(script),
        clock_ns=clock_ns,
    )
    recorder.start(ready_timeout_s=5.0)
    return recorder.finish(tail_s=tail_s)


class _PacedMic:
    """A mic whose clock runs with its audio. A step ``(samples, late_s)`` returns an int32
    ``(frames, channels)`` block once it was captured, and ``late_s`` later still (a reader held
    off the GIL while ALSA's ring filled); a negative int is the overrun that caused. Each
    scripted read blocks ``wall_s`` of real time first. After the script it trickles silence,
    one 1 ms read per 20 ms, so a test can stop the reader between reads."""

    def __init__(self, script, *, wall_s=0.0):
        self.now_ns = 0
        self.drained = threading.Event()
        self._script = list(script)
        self._wall_s = wall_s

    def clock_ns(self):
        return self.now_ns

    def read(self):
        if self._script:
            time.sleep(self._wall_s)
            samples, late_s = self._script.pop(0)
        else:
            self.drained.set()
            time.sleep(0.02)
            samples, late_s = _silence(48), 0.0
        if isinstance(samples, int):
            self.now_ns += round(late_s * 1e9)
            return samples, b""
        self.now_ns += round((len(samples) / RATE + late_s) * 1e9)
        return len(samples), samples.tobytes()

    def close(self):
        pass


def _silence(frames):
    return np.zeros((frames, CHANNELS), dtype="<i4")


def _paced_recorder(mic, **kwargs):
    return WiredRecorder(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS, pcm_factory=lambda: mic,
        clock_ns=mic.clock_ns, **{"max_capture_s": 10.0, **kwargs},
    )


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
    assert ledger.capture_gap_evaluated  # reported, never "not evaluated"
    assert ledger.capture_gap_frames == 0


@pytest.mark.parametrize("gaps,script", [
    # One read returns 0.75 s late: its audio is good, the ring behind it overflowed.
    (1, [(_silence(480), 0.0), (_silence(480), 0.75), (-32, 0.0)]),
    # The reader falls 25 ms further behind on every read until the ring overflows.
    (1, [(_silence(480), 0.025)] * 30 + [(-32, 0.0)]),
    # Two overruns: each loss counts from its own restart, never from the first.
    (2, [(_silence(480), 0.5), (-32, 0.0), (_silence(480), 0.25), (-32, 0.0)]),
])
def test_an_overrun_books_the_audio_the_stall_lost(gaps, script):
    # Each script loses 0.75 s: 36,000 frames at 48 kHz (#5632: a stall used to book 1).
    mic = _PacedMic(script)
    recorder = _paced_recorder(mic)
    recorder.start(ready_timeout_s=5.0)
    assert mic.drained.wait(5.0)
    recording = recorder.finish(tail_s=0)
    assert recording.gap_count == gaps
    assert recording.gap_frames == 36_000
    report = build_capture_integrity_report(
        recording, encoded_frames=recording.frames,
        zero_run_count=0, zero_runs=[],
    )
    assert report[REPORT_KEY_CAPTURE_GAPS] == gaps
    assert report[REPORT_KEY_CAPTURE_GAP_FRAMES] == 36_000
    # The ledger grades it exactly as a browser render gap: FAIL material.
    ledger = reconcile_capture_frames(report, received_frames=recording.frames)
    assert ledger.capture_gap_frames == 36_000
    assert "capture_overrun" in ledger.lost_at


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


@pytest.mark.parametrize("stop", ["spl", "budget"])
def test_guarded_recorder_failure_is_visible_before_playback_can_start(stop):
    loud = 2 ** (30 if stop == "spl" else 20)
    monitor = WiredSplMonitor(_Sensitivity(), 80.0, 0)
    recorder = WiredRecorder(
        "fake:pcm", sample_rate_hz=RATE, channels=CHANNELS,
        max_capture_s=1.0 if stop == "spl" else 32 / RATE,
        pcm_factory=lambda: FakePcm([(32, [(loud, loud)] * 32)]),
        spl_monitor=monitor,
    )
    error = WiredSplCeilingExceeded if stop == "spl" else WiredCaptureError
    with pytest.raises(error) as caught:
        recorder.start()
    assert recorder.failure is caught.value
    assert monitor.exceeded.is_set() == (stop == "spl")


def _noise(rng, frames, loud=False):
    """Uniform noise on both channels: ≈ 53 dB SPL quiet, ≈ 89 dB SPL loud on channel 0."""
    block = rng.integers(-(2 ** 24), 2 ** 24, size=(frames, CHANNELS)).astype("<i4")
    if loud:
        block[:, 0] = rng.integers(-(2 ** 30), 2 ** 30, size=frames)
    return block


def _judged_alone(periods):
    """The pre-batching watch: one observe per period, stopped by its first trip."""
    monitor = WiredSplMonitor(_Sensitivity(), 85.0, 0)
    for period in periods:
        if monitor.error is None:
            monitor.observe([period.tobytes()], CHANNELS, sample_rate_hz=RATE)
    return monitor


@pytest.mark.parametrize("sizes", [(1024,) * 5, (1024, 1024, 377, 1024, 1024)])
@pytest.mark.parametrize("loud", [(), (0,), (2,), (4,), (1, 3)])
def test_a_batch_judges_each_period_as_if_it_arrived_alone(sizes, loud):
    for seed in range(16):
        rng = np.random.default_rng(seed)
        periods = [_noise(rng, frames, loud=index in loud) for index, frames in enumerate(sizes)]
        batched = WiredSplMonitor(_Sensitivity(), 85.0, 0)
        batched.observe([period.tobytes() for period in periods], CHANNELS, sample_rate_hz=RATE)
        alone = _judged_alone(periods)
        # Bit-identical, not approximate: this is the value the commissioning stop compares.
        assert batched.max_window_db_spl == alone.max_window_db_spl
        assert batched.exceeded.is_set() == alone.exceeded.is_set() == bool(loud)
        if loud:
            assert batched.error.observed_db_spl == alone.error.observed_db_spl


@pytest.mark.parametrize("loud_at", ["batch_first", "batch_middle", "batch_last", "stop", "budget"])
def test_the_recorder_stops_on_the_same_period_within_one_batch(loud_at):
    period = 1024
    batch = math.ceil(SPL_BATCH_S * RATE / period)  # reads per judgement after the first
    # Read 0 is judged alone (the pre-roll refusal); reads 1..batch are the first batch. At
    # "stop" (finish) and "budget" the loop ends with the loud read still unjudged.
    loud = {"batch_first": 1, "batch_middle": 1 + batch // 2, "batch_last": batch, "stop": 1, "budget": 2}[loud_at]
    rng = np.random.default_rng(loud)
    periods = [_noise(rng, period, loud=index == loud) for index in range(loud + 1 if loud_at == "stop" else 3 * batch)]
    mic = _PacedMic([(block, 0.0) for block in periods])
    monitor = WiredSplMonitor(_Sensitivity(), 85.0, 0)
    budget = {"max_capture_s": (loud + 1) * period / RATE} if loud_at == "budget" else {}
    recorder = _paced_recorder(mic, spl_monitor=monitor, **budget)
    with pytest.raises(WiredSplCeilingExceeded) as caught:
        # The unpaced script can trip before start() returns; either call then raises it.
        recorder.start(ready_timeout_s=5.0)
        deadline = time.monotonic() + 5.0
        while recorder.failure is None and not mic.drained.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        recorder.finish(tail_s=0)
    alone = _judged_alone(periods)
    assert caught.value.observed_db_spl == alone.error.observed_db_spl
    assert monitor.max_window_db_spl == alone.max_window_db_spl
    # "At most one batch later": less than SPL_BATCH_S of audio was read past the loud period.
    assert 0 <= recorder._frames - (loud + 1) * period < SPL_BATCH_S * RATE


class _SlowMonitor(WiredSplMonitor):
    """A stop that judges slowly enough for the playback watcher to poll mid-judgement."""

    def observe(self, *args, **kwargs):
        time.sleep(0.3)
        super().observe(*args, **kwargs)


@pytest.mark.parametrize("exit_by", ["budget", "overruns"])
async def test_a_loud_read_waiting_at_a_reader_exit_stops_the_take_as_the_spl_stop(exit_by, caplog, tmp_path):
    rng = np.random.default_rng(0)
    script = [(_noise(rng, 1024), 0.0), (_noise(rng, 1024, loud=True), 0.0)]
    script += [(-32, 0.0)] * MAX_CONSECUTIVE_READ_FAILURES if exit_by == "overruns" else []
    budget = {"max_capture_s": 2 * 1024 / RATE} if exit_by == "budget" else {}
    capture = WiredStimulusCapture(
        device=None, bundle_dir=tmp_path, spl_monitor=_SlowMonitor(_Sensitivity(), 85.0, 0),
        # Reads paced like real ones, so the exit comes after start() returned and the
        # watcher is polling while the waiting read is judged.
        recorder_factory=lambda *_: _paced_recorder(_PacedMic(script, wall_s=0.05), **budget),
    )

    async def play():
        await asyncio.Event().wait()  # plays until the watcher stops it

    with pytest.raises(StimulusCaptureStopped) as caught:
        await capture.around(play, program=SimpleNamespace(sample_rate_hz=RATE, total_samples=RATE))
    assert caught.value.code == SPL_CEILING_EXCEEDED
    assert float(event_fields(caplog, "active_speaker.measurement_spl_ceiling_stop")["observed_db_spl"]) > 85



async def test_a_take_banks_how_the_playback_route_counters_moved(monkeypatch, tmp_path):
    """A wired take banks the route counters' deltas across its capture and the
    surfaces that answered both reads, so a playback fault is named per take
    (#5684)."""
    lane = {"label": "correction", "xrun_count": 2, "catchup_events": 5}
    snapshots = iter([{"fanin": {"inputs": [lane]}, "outputd": None},
                      {"fanin": {"inputs": [{**lane, "xrun_count": 3}]}, "outputd": None}])

    class Recorder:
        def start(self):
            pass

        def finish(self, **_):
            return None

        def abort(self):
            raise AssertionError("a completed take was aborted")

    monkeypatch.setattr(wired_stimulus, "mint_wired_answer", lambda *_, **__: WiredCaptureAnswer(wav=b"", wav_path="a.wav"))
    monkeypatch.setattr(wired_stimulus, "place_wired_answer", lambda _dir, answer, **_: answer)
    capture = WiredStimulusCapture(device=None, bundle_dir=tmp_path, recorder_factory=lambda *_: Recorder(),
                                   read_route_health=lambda: next(snapshots))

    async def play():
        pass

    await capture.around(play, program=SimpleNamespace(sample_rate_hz=RATE, total_samples=RATE, phase="measure"))
    assert capture.take_answer().capture_integrity["playback_path"] == {
        "read": ["fanin"], "deltas": {"fanin.inputs.0.xrun_count": 1.0}}


def test_spl_monitor_keeps_loudest_unweighted_period_below_ceiling():
    monitor = WiredSplMonitor(_Sensitivity(), 80.0, 0)
    quiet = (2 ** 26).to_bytes(4, "little", signed=True) * 32
    monitor.observe([quiet], 1, sample_rate_hz=RATE)
    assert not monitor.exceeded.is_set()
    assert monitor.max_window_db_spl == pytest.approx(69.9, abs=0.1)


def test_spl_monitor_accepts_a_one_hz_sample_clock():
    monitor = WiredSplMonitor(_Sensitivity(), 80.0, 0)
    monitor.observe([(2 ** 26).to_bytes(4, "little", signed=True)], 1, sample_rate_hz=1)
    assert monitor.loudest_half_second_db_spl == pytest.approx(69.9, abs=0.1)


@pytest.mark.parametrize("rate,block_frames,channel", [(48000, 1024, 0), (44100, 777, 1), (48000, 31001, 1)])
def test_spl_level_follows_the_loud_region_and_resets(rate, block_frames, channel):
    monitor = WiredSplMonitor(_Sensitivity(), 85.0, channel)
    signal = np.full((6 * rate, 2), 0.001)
    signal[:, 1 - channel] = 0.5
    signal[2 * rate:4 * rate, channel] = 0.01
    pcm = (signal * np.iinfo(np.int32).max).astype("<i4")
    for offset in range(0, len(pcm), block_frames):
        block = pcm[offset:offset + block_frames]
        monitor.observe([block.tobytes()], 2, sample_rate_hz=rate)
    assert monitor.loudest_half_second_db_spl == pytest.approx(60, abs=0.1)
    assert not monitor.exceeded.is_set()
    hot = np.full((1024, 2), 0.5 * np.iinfo(np.int32).max, dtype="<i4")
    monitor.observe([hot.tobytes()], 2, sample_rate_hz=rate)
    assert monitor.exceeded.is_set()
    assert isinstance(monitor.error, WiredSplCeilingExceeded)
    assert monitor.error.observed_db_spl == monitor.max_window_db_spl == pytest.approx(94, abs=0.1)
    assert monitor.loudest_half_second_db_spl == pytest.approx(60, abs=0.1)
    monitor.reset()
    assert monitor.loudest_half_second_db_spl == monitor.max_window_db_spl == -np.inf
    assert monitor.error is None and not monitor.exceeded.is_set()
    quiet = pcm[:rate // 2].copy()
    monitor.observe([quiet.tobytes()], 2, sample_rate_hz=rate)
    assert monitor.loudest_half_second_db_spl == pytest.approx(40, abs=0.1)


def test_spl_level_averages_sparse_clicks_over_the_room_floor():
    monitor = WiredSplMonitor(_Sensitivity(), 85.0, 0)
    signal = np.full(6 * 48000, 0.001)
    signal[2400::48000] = 0.07
    pcm = (signal * np.iinfo(np.int32).max).astype("<i4")
    for offset in range(0, len(pcm), 1024):
        block = pcm[offset:offset + 1024]
        monitor.observe([block.tobytes()], 1, sample_rate_hz=48000)
    assert monitor.loudest_half_second_db_spl == pytest.approx(40, abs=1.0)
    assert monitor.max_window_db_spl > 47


@pytest.mark.parametrize("tail_frames,expected", [(11999, 40), (12000, 60), (23999, 60), (24000, 60)])
def test_spl_level_counts_only_a_long_enough_final_window(tail_frames, expected):
    monitor = WiredSplMonitor(_Sensitivity(), 85.0, 0)
    for frames, amplitude in ((24000, .001), (tail_frames, .01)):
        pcm = np.full(frames, amplitude * np.iinfo(np.int32).max, dtype="<i4")
        monitor.observe([pcm.tobytes()], 1, sample_rate_hz=48000)
    assert monitor.loudest_half_second_db_spl == pytest.approx(expected, abs=.1)


def test_reading_a_partial_level_does_not_bank_it_as_a_complete_window():
    monitor = WiredSplMonitor(_Sensitivity(), 85.0, 0)
    for amplitude, expected in ((.01, 60), (0, 57)):
        pcm = np.full(12000, amplitude * np.iinfo(np.int32).max, dtype="<i4")
        monitor.observe([pcm.tobytes()], 1, sample_rate_hz=48000)
        assert monitor.loudest_half_second_db_spl == pytest.approx(expected, abs=.1)


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
    assert ledger.capture_gap_evaluated
    assert ledger.capture_gap_frames >= 1  # FAIL material — never clean
    assert "capture_overrun" in ledger.lost_at


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


@pytest.fixture
def fake_alsaaudio(monkeypatch):
    """pyalsaaudio as far as the capture path touches it; like the real one, its error
    derives from ``Exception``, not ``OSError``."""
    alsaaudio = ModuleType("alsaaudio")
    alsaaudio.PCM_CAPTURE, alsaaudio.PCM_NORMAL, alsaaudio.PCM_FORMAT_S32_LE = "capture", "normal", "s32"
    alsaaudio.ALSAAudioError = type("ALSAAudioError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "alsaaudio", alsaaudio)
    return alsaaudio


def test_the_capture_opens_a_deep_alsa_ring(fake_alsaaudio):
    """The ring, not the reader, absorbs a stall under web load (#5632)."""
    opened = []
    fake_alsaaudio.PCM = lambda **kwargs: opened.append(kwargs) or FakePcm([])
    recorder = make_wired_recorder(_umik2(), sample_rate_hz=RATE, max_capture_s=1.0)
    recorder.start(ready_timeout_s=5.0)
    recorder.abort()
    assert opened == [{
        "type": "capture", "mode": "normal", "device": "hw:CARD=UMIK2,DEV=0", "rate": RATE,
        "channels": 2, "format": "s32", "periodsize": 1024, "periods": CAPTURE_RING_PERIODS,
    }]
    assert CAPTURE_RING_PERIODS * 1024 / RATE >= 0.5  # seconds of ring


def test_an_alsa_read_error_mid_take_fails_the_reader_for_the_watcher(fake_alsaaudio):
    """An unplugged mic raises ``ALSAAudioError`` from ``read()``. Uncaught, it ended the reader
    silently: no failure for the watcher, so playback ran on with no SPL watch."""
    unplugged = fake_alsaaudio.ALSAAudioError("No such device [UMIK2]")
    fake_alsaaudio.PCM = lambda **_: FakePcm([(32, [(1, 1)] * 32), unplugged])
    recorder = make_wired_recorder(_umik2(), sample_rate_hz=RATE, max_capture_s=1.0)
    with pytest.raises(WiredCaptureError) as caught:
        recorder.start(ready_timeout_s=5.0)  # the error can land before start() returns
        deadline = time.monotonic() + 5.0
        while recorder.failure is None and time.monotonic() < deadline:
            time.sleep(0.001)
        recorder.finish(tail_s=0)
    assert recorder.failure is caught.value
    assert caught.value.__cause__ is unplugged


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


@pytest.mark.parametrize(
    "model_key, expected_channel, expected_sample",
    [
        ("minidsp_umik2", 0, 10_000),
        ("generic_measurement_mic", 1, 10_001),
    ],
)
def test_model_channel_is_stable_while_generic_capture_selects_strongest(
    model_key, expected_channel, expected_sample,
):
    recording = _record([(64, [(10_000, 10_001)] * 64)])
    answer = mint_wired_answer(
        recording, device=replace(_umik2(), model_key=model_key),
    )

    assert answer.device["channel_selected"] == expected_channel
    assert len(answer.device["channel_rms_dbfs"]) == 2
    mono, _rate = decode_wav_to_mono(answer.wav)
    assert mono[0] == pytest.approx(expected_sample / ((1 << 31) - 1))


@pytest.mark.parametrize("declared_channel", [-1, 2, True, "0"])
def test_declared_channel_must_fit_the_capture(declared_channel):
    recording = _record([(64, [(1, 2)] * 64)])

    with pytest.raises(WiredCaptureError):
        select_capture_channel(recording, declared_channel=declared_channel)


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
        REPORT_KEY_CAPTURE_GAPS: 0,
        REPORT_KEY_CAPTURE_GAP_FRAMES: 0,
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
    assert report[REPORT_KEY_CAPTURE_GAPS] == recording.gap_count == 1
    assert report[REPORT_KEY_CAPTURE_GAP_FRAMES] == recording.gap_frames


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


# --------------------------------------------------------------------------- #
# the shared refusal, and the ONE answer mint
# --------------------------------------------------------------------------- #


def test_absence_is_disclosed_with_the_way_forward(tmp_path):
    """ADR-0188: wired is THE acoustic-measurement path, so no mic is a named
    disclosure carrying its remedy — never a session measuring on something
    nobody chose. Every door raises THIS, and maps it to its own exit code."""
    with pytest.raises(WiredMicMissing) as caught:
        require_wired_mic(proc_asound=tmp_path)
    assert caught.value.code == CODE_WIRED_MIC_MISSING
    assert "measurement mic" in str(caught.value)


def _umik2() -> WiredMicDevice:
    return WiredMicDevice(
        card_id="UMIK2",
        card_index=2,
        usb_id=UMIK2_USB_ID,
        model_key="minidsp_umik2",
        model_label="miniDSP UMIK-2",
    )


@pytest.mark.parametrize(
    "hint, expected",
    [
        (
            SimpleNamespace(
                resolvable=True, calibration_id="cal-123", model="minidsp_umik2",
            ),
            {
                "calibration": {
                    "mode": "stored",
                    "calibration_id": "cal-123",
                    "model": "minidsp_umik2",
                }
            },
        ),
        (SimpleNamespace(resolvable=False, calibration_id="x", model="m"), None),
        (None, None),
    ],
    ids=["stored", "unresolvable", "no-record"],
)
def test_the_answer_carries_a_stored_calibration_reference_or_none(hint, expected):
    """The seam's reference shape, and the two ways there is none.

    A door with no household session in reach — the CLI doors — resolves no
    hint at all and mints with no ``setup``, which is the same
    annotated-uncalibrated path the unresolvable hint lands on.
    """
    assert setup_from_hint(hint) == expected
    answer = mint_wired_answer(
        _record([(32, [(7, 3)] * 32)]),
        device=_umik2(),
        setup=setup_from_hint(hint),
    )
    assert answer.setup == expected


def test_a_door_with_no_household_session_mints_an_uncalibrated_answer():
    """The CLI doors state no ``setup`` rather than carrying a reference they
    cannot resolve."""
    answer = mint_wired_answer(_record([(32, [(7, 3)] * 32)]), device=_umik2())
    assert answer.setup is None


def test_mint_wired_answer_is_the_whole_answer():
    """One recording in, all four seam fields out: the audio, the mic that
    heard it, the calibration reference, and the counters the analyzer grades
    the take by."""
    recording = _record([(32, [(7, 3)] * 32), "overrun", (32, [(7, 3)] * 32)])
    answer = mint_wired_answer(recording, device=_umik2())

    samples, rate = decode_wav_to_mono(answer.wav)
    assert rate == RATE
    assert samples.size == recording.frames
    assert answer.device["card"] == "UMIK2"
    assert answer.device["model_key"] == "minidsp_umik2"
    assert answer.device["channel_selected"] == 0
    assert answer.capture_integrity[REPORT_KEY_FRAMES] == recording.frames
    assert answer.capture_integrity[REPORT_KEY_CAPTURE_GAPS] == 1
    assert set(INTEGRITY_COUNTER_KEYS) <= set(answer.capture_integrity)
