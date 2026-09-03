# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins for `jasper.cli.aec_bridge_reference`.

The far-end reference is what AEC3 subtracts from the mic. These pin the
transport around the conversion: delivery-boundary invariance, the stereo
fold, post-gain clip accounting, and the queue publish that must drop rather
than block when the AEC loop falls behind.
"""
from __future__ import annotations

import logging
from queue import Full, Queue

import numpy as np
import pytest

from jasper.cli import aec_bridge_reference
from jasper.cli.aec_bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.cli.aec_bridge_reference import (
    REF_RATE,
    ReferenceFrameBatch,
    ReferenceFrameConverter,
    enqueue_reference_frames,
)
from jasper.cli.aec_bridge_telemetry import (
    DropLogDebouncer,
    StatsIdentity,
    _BridgeStats,
)

CAPTURE_BLOCK = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
IDENTITY = StatsIdentity(
    sample_rate_hz=SAMPLE_RATE,
    frame_samples=FRAME_SAMPLES,
    reference_source="outputd_udp",
    reference_endpoint="127.0.0.1:9891",
)


class _AlwaysFullQ:
    def put_nowait(self, _frame):
        raise Full


def _stereo_samples(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return np.column_stack((left, right)).reshape(-1).astype(np.int16)


@pytest.fixture(autouse=True)
def _reset_ref_clip_counters():
    aec_bridge_reference.reset_ref_clip_counters()
    yield
    aec_bridge_reference.reset_ref_clip_counters()


def test_reference_converter_is_fragmentation_invariant_and_keeps_remainder():
    sample_count = 2 * CAPTURE_BLOCK + 137
    phase = np.arange(sample_count, dtype=np.float64)
    left = np.rint(12000 * np.sin(2 * np.pi * 1200 * phase / 48000)).astype(
        np.int16
    )
    right = np.rint(8000 * np.cos(2 * np.pi * 700 * phase / 48000)).astype(
        np.int16
    )
    stereo = _stereo_samples(left, right)

    whole = ReferenceFrameConverter(ref_gain_db=0, ref_hpf_hz=125)
    whole_batch = whole.feed(stereo)

    fragmented = ReferenceFrameConverter(ref_gain_db=0, ref_hpf_hz=125)
    fragments = (
        stereo[:622],
        stereo[622:2414],
        stereo[2414:],
    )
    fragmented_frames: list[bytes] = []
    fragmented_clipped = 0
    fragmented_total = 0
    for fragment in fragments:
        batch = fragmented.feed(fragment)
        fragmented_frames.extend(batch.frames)
        fragmented_clipped += batch.clipped_samples
        fragmented_total += batch.total_samples

    assert tuple(fragmented_frames) == whole_batch.frames
    assert len(whole_batch.frames) == 2
    assert all(len(frame) == FRAME_SAMPLES * 2 for frame in whole_batch.frames)
    assert fragmented_clipped == whole_batch.clipped_samples
    assert fragmented_total == whole_batch.total_samples == 2 * FRAME_SAMPLES

    fill = np.zeros(2 * (CAPTURE_BLOCK - 137), dtype=np.int16)
    whole_tail = whole.feed(fill)
    fragmented_tail = fragmented.feed(fill)
    assert whole_tail == fragmented_tail
    assert len(whole_tail.frames) == 1


def test_reference_converter_averages_stereo_before_resampling(monkeypatch):
    monkeypatch.setattr(
        aec_bridge_reference,
        "resample_poly",
        lambda samples, *, up, down: samples[::down],
    )
    monkeypatch.setattr(
        aec_bridge_reference,
        "sosfilt",
        lambda _sos, samples, *, zi: (samples, zi),
    )
    left = np.full(CAPTURE_BLOCK, 1000, dtype=np.int16)
    right = np.full(CAPTURE_BLOCK, 3000, dtype=np.int16)
    converter = ReferenceFrameConverter(ref_gain_db=0, ref_hpf_hz=125)

    batch = converter.feed(_stereo_samples(left, right))
    output = np.frombuffer(batch.frames[0], dtype=np.int16)

    assert output.shape == (FRAME_SAMPLES,)
    assert np.all(output == 2000)


def test_reference_converter_reports_post_gain_clipping(monkeypatch):
    monkeypatch.setattr(
        aec_bridge_reference,
        "resample_poly",
        lambda samples, *, up, down: samples[::down],
    )
    monkeypatch.setattr(
        aec_bridge_reference,
        "sosfilt",
        lambda _sos, samples, *, zi: (samples, zi),
    )
    hot = np.full(CAPTURE_BLOCK, 30000, dtype=np.int16)
    converter = ReferenceFrameConverter(ref_gain_db=20, ref_hpf_hz=125)

    batch = converter.feed(_stereo_samples(hot, hot))
    output = np.frombuffer(batch.frames[0], dtype=np.int16)

    assert batch.clipped_samples == FRAME_SAMPLES
    assert batch.total_samples == FRAME_SAMPLES
    assert np.all(output == 32767)


def test_reference_enqueue_counts_and_debounces_full_queue(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(aec_bridge_reference.time, "monotonic", lambda: 10.0)
    caplog.set_level(logging.WARNING, logger="jasper.aec_bridge")
    stats = _BridgeStats(IDENTITY)
    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    batch = ReferenceFrameBatch(
        frames=(frame, frame, frame),
        clipped_samples=4,
        total_samples=3 * FRAME_SAMPLES,
    )

    enqueue_reference_frames(
        _AlwaysFullQ(),
        batch,
        stats=stats,
        drop_log=DropLogDebouncer(),
        drop_message="ref queue full, dropped %d frames in last %.1fs",
    )

    snapshot = stats.snapshot()
    assert snapshot["counters"]["queue_drops"]["ref"] == 3
    assert snapshot["reference_input"]["frames_enqueued"] == 0
    assert snapshot["reference_input"]["last_frame_age_ms"] is None
    assert aec_bridge_reference.ref_clip_percent() == pytest.approx(
        100.0 * 4 / (3 * FRAME_SAMPLES)
    )
    assert "ref queue full, dropped 3 frames in last 1.0s" in caplog.text


def test_reference_input_age_advances_and_new_input_resets(monkeypatch):
    clock = 100.0
    monkeypatch.setattr(
        aec_bridge_reference.time, "monotonic", lambda: clock,
    )
    stats = _BridgeStats(IDENTITY)
    stats.reset(
        reference_source="outputd_udp",
        reference_endpoint="127.0.0.1:9891",
    )
    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    ref_q: Queue[bytes] = Queue(maxsize=4)

    enqueue_reference_frames(
        ref_q,
        ReferenceFrameBatch(
            frames=(frame, frame),
            clipped_samples=0,
            total_samples=2 * FRAME_SAMPLES,
        ),
        stats=stats,
        drop_log=DropLogDebouncer(),
        drop_message="unused %d %.1f",
    )
    first = stats.snapshot()["reference_input"]
    assert first["frames_enqueued"] == 2
    assert first["last_frame_age_ms"] == 0
    assert first["snapshot_monotonic_ms"] == 100_000
    assert first["process_age_ms"] == 0

    clock = 101.25
    assert (
        stats.snapshot()["reference_input"]["last_frame_age_ms"] == 1250
    )

    ref_q.get_nowait()
    ref_q.get_nowait()
    enqueue_reference_frames(
        ref_q,
        ReferenceFrameBatch(
            frames=(frame,),
            clipped_samples=0,
            total_samples=FRAME_SAMPLES,
        ),
        stats=stats,
        drop_log=DropLogDebouncer(),
        drop_message="unused %d %.1f",
    )
    latest = stats.snapshot()["reference_input"]
    assert latest["frames_enqueued"] == 3
    assert latest["last_frame_age_ms"] == 0
    assert latest["snapshot_monotonic_ms"] == 101_250
    assert latest["process_age_ms"] == 1_250
