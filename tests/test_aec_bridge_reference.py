# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins for `jasper.aec.bridge_reference`.

The far-end reference is what AEC3 subtracts from the mic. These pin the
transport around the conversion — the queue publish that must drop rather
than block when the AEC loop falls behind, and the clip and frame-age
accounting it feeds. The conversion itself is pinned in
`test_aec_reference_converter.py`.
"""
from __future__ import annotations

import logging
from queue import Full, Queue

import numpy as np
import pytest

from jasper.aec import bridge_reference
from jasper.aec.bridge_engines import FRAME_SAMPLES
from jasper.aec.bridge_reference import (
    ReferenceFrameBatch,
    enqueue_reference_frames,
)
from jasper.aec.bridge_telemetry import DropLogDebouncer, _BridgeStats
from tests._aec_bridge_helpers import IDENTITY


class _AlwaysFullQ:
    def put_nowait(self, _frame):
        raise Full


@pytest.fixture(autouse=True)
def _reset_ref_clip_counters():
    bridge_reference.reset_ref_clip_counters()
    yield
    bridge_reference.reset_ref_clip_counters()


def test_reference_enqueue_counts_and_debounces_full_queue(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(bridge_reference.time, "monotonic", lambda: 10.0)
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
    assert bridge_reference.ref_clip_percent() == pytest.approx(
        100.0 * 4 / (3 * FRAME_SAMPLES)
    )
    assert "ref queue full, dropped 3 frames in last 1.0s" in caplog.text


def test_reference_input_age_advances_and_new_input_resets(monkeypatch):
    clock = 100.0
    monkeypatch.setattr(
        bridge_reference.time, "monotonic", lambda: clock,
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
