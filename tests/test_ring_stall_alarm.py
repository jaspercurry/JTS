# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Doctor integration for the shared-header ring stall alarm."""

from __future__ import annotations

import time

import pytest

from jasper import ring_assets
from jasper.cli.doctor import audio_runtime_ring
from tests.test_ring_header import _ring_file

# --- the doctor surface ------------------------------------------------------


def test_the_doctor_check_reports_a_stalled_active_ring(tmp_path, monkeypatch):
    """The doctor takes its own ``time.monotonic_ns()``, so this fixture's
    heartbeats are anchored to the real clock."""
    real_now = time.monotonic_ns()
    stalled = _ring_file(
        tmp_path,
        writer_hb=real_now - 5_000_000,
        reader_hb=max(real_now - 3_000_000_000, 1),
    )
    monkeypatch.setattr(ring_assets, "RING_A_PROGRAM_FILE", str(tmp_path / "absent-a"))
    monkeypatch.setattr(ring_assets, "RING_B_CONTENT_FILE", str(tmp_path / "absent-b"))
    monkeypatch.setattr(ring_assets, "RING_ACTIVE_CONTENT_FILE", stalled)
    result = audio_runtime_ring.check_ring_reader_stall()
    assert result.status == "warn"
    assert result.reason == audio_runtime_ring.REASON_RING_READER_STALLED


def test_the_doctor_check_is_silent_on_an_unarmed_box(tmp_path, monkeypatch):
    """Every box in the fleet today has no ring files at all."""
    for attr in (
        "RING_A_PROGRAM_FILE",
        "RING_B_CONTENT_FILE",
        "RING_ACTIVE_CONTENT_FILE",
    ):
        monkeypatch.setattr(ring_assets, attr, str(tmp_path / f"absent-{attr}"))
    result = audio_runtime_ring.check_ring_reader_stall()
    assert result.status == "skipped"
    assert result.reason == audio_runtime_ring.REASON_RING_READER_NO_LIVE_RING


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
