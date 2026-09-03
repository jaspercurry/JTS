# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring stall alarm: writer heartbeat FRESH while reader heartbeat STALE.

The design's D1 ruling, pinned. The alarm's whole correctness argument is that
it partners ``writer_heartbeat_ns`` with ``reader_heartbeat_ns`` and NOT with
``read_seq`` — because the writer advances ``read_seq`` on the absent reader's
behalf at demotion, so a ``read_seq``-flat clause goes false exactly when the
drops begin. :func:`test_the_alarm_survives_the_read_seq_inversion` is that
argument as a test rather than as prose.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jasper import ring_assets
from jasper.ring_assets import RING_LIVENESS_TIMEOUT_NS, ring_stall_verdict

REPO_ROOT = Path(__file__).resolve().parents[1]

NOW = 100_000_000_000  # an arbitrary CLOCK_MONOTONIC "now", 100 s
FRESH = NOW - 5_000_000  # 5 ms behind — comfortably live
STALE = NOW - 3_000_000_000  # 3 s behind — past the 2 s window


def _ring_file(
    tmp_path: Path,
    *,
    writer_hb: int,
    reader_hb: int,
    read_seq: int = 0,
    write_seq: int = 0,
    magic: int = 0x4A52_494E,
    version: int = 1,
) -> str:
    """A synthetic 128-byte v1 ring header with the runtime fields set."""
    head = bytearray(ring_assets._RING_HEADER_BYTES)
    struct.pack_into("<I", head, ring_assets._RING_OFF_MAGIC, magic)
    struct.pack_into("<I", head, ring_assets._RING_OFF_VERSION, version)
    struct.pack_into("<I", head, ring_assets._RING_OFF_RATE, 48000)
    struct.pack_into("<I", head, ring_assets._RING_OFF_CHANNELS, 4)
    struct.pack_into("<I", head, ring_assets._RING_OFF_SAMPLE_FORMAT, 2)
    struct.pack_into("<I", head, ring_assets._RING_OFF_PERIOD_FRAMES, 128)
    struct.pack_into("<I", head, ring_assets._RING_OFF_N_SLOTS, 2)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_WRITE_SEQ, write_seq)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_READ_SEQ, read_seq)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_WRITER_HEARTBEAT_NS, writer_hb)
    struct.pack_into("<Q", head, ring_assets._RING_OFF_READER_HEARTBEAT_NS, reader_hb)
    path = tmp_path / "active-content.ring"
    path.write_bytes(bytes(head))
    return str(path)


# --- the threshold is not ours to choose ------------------------------------


def test_the_liveness_window_is_the_ioplugs_own_number():
    """SINGLE SOURCE OF TRUTH, across a language boundary.

    The observer must apply the SAME staleness window the C ioplug's
    ``reader_is_live`` applies when it decides to demote a reader and free-run.
    An observer with its own threshold would eventually disagree with the writer
    about who is alive — reporting a stall the writer is not acting on, or
    staying silent through one it is.
    """
    header = (REPO_ROOT / "c" / "jts-ring-ioplug" / "jts_ring_shm.h").read_text(
        encoding="utf-8"
    )
    assert "#define JTS_RING_WRITER_LIVENESS_TIMEOUT_NS 2000000000ull" in header, (
        "the C liveness window moved; re-derive RING_LIVENESS_TIMEOUT_NS"
    )
    assert RING_LIVENESS_TIMEOUT_NS == 2_000_000_000


# --- the conjunction ---------------------------------------------------------


def test_writer_live_and_reader_stale_is_the_alarm(tmp_path):
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=FRESH, reader_hb=STALE), now_ns=NOW
    )
    assert verdict.present is True
    assert verdict.stalled is True
    assert "free-running" in verdict.detail
    assert "Audio is being lost" in verdict.detail


def test_writer_live_and_reader_never_stamped_is_the_alarm(tmp_path):
    """A reader that attached and died before its first period — or never
    attached at all while the writer publishes."""
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=FRESH, reader_hb=0), now_ns=NOW
    )
    assert verdict.stalled is True
    assert "never stamped" in verdict.detail


def test_both_ends_live_is_silent(tmp_path):
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=FRESH, reader_hb=FRESH), now_ns=NOW
    )
    assert verdict.present is True
    assert verdict.stalled is False
    assert "both ends live" in verdict.detail


# --- the negatives that keep it off the whole fleet -------------------------


def test_an_idle_ring_is_not_a_stall(tmp_path):
    """THE LOAD-BEARING NEGATIVE. A ring nobody is writing is idle, not stalled.

    Without this the alarm would fire on every box carrying a leftover ring file
    with no daemon attached — which is most of the fleet most of the time.
    """
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=STALE, reader_hb=STALE), now_ns=NOW
    )
    assert verdict.present is False
    assert verdict.stalled is False
    assert "not a stall" in verdict.detail


def test_a_never_written_ring_is_not_a_stall(tmp_path):
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=0, reader_hb=0), now_ns=NOW
    )
    assert verdict.present is False
    assert "never been written" in verdict.detail


def test_an_absent_or_torn_ring_is_not_judged(tmp_path):
    assert ring_stall_verdict(str(tmp_path / "nope.ring")).present is False
    torn = _ring_file(tmp_path, writer_hb=FRESH, reader_hb=STALE, magic=0xDEAD_BEEF)
    assert ring_stall_verdict(torn, now_ns=NOW).present is False
    wrong_version = _ring_file(tmp_path, writer_hb=FRESH, reader_hb=STALE, version=99)
    assert ring_stall_verdict(wrong_version, now_ns=NOW).present is False


def test_a_heartbeat_from_the_future_clamps_instead_of_alarming(tmp_path):
    """Saturating subtraction, mirroring the C ``reader_is_live`` comment.

    The reader stamps concurrently, so a heartbeat taken AFTER this observer
    sampled ``now_ns`` would underflow an unsigned subtraction into an enormous
    age and spuriously alarm on a perfectly live ring. Python's ints do not
    wrap, but the same clamp is needed for the same reason.
    """
    verdict = ring_stall_verdict(
        _ring_file(tmp_path, writer_hb=NOW + 1_000_000, reader_hb=NOW + 1_000_000),
        now_ns=NOW,
    )
    assert verdict.stalled is False
    assert verdict.reader_age_ns == 0
    assert verdict.writer_age_ns == 0


# --- D1: the discrimination argument, as a test ------------------------------


@pytest.mark.parametrize("write_seq", [0, 4096, 2**40])
@pytest.mark.parametrize("read_seq", [0, 1, 4096, 2**40])
def test_the_alarm_survives_the_read_seq_inversion(tmp_path, read_seq, write_seq):
    """THE D1 RULING. ``read_seq`` must not enter the verdict at ANY value.

    During the exact fault this alarm exists to catch, the writer advances
    ``read_seq`` on the absent reader's behalf so occupancy stays honest
    (``jts_ring_shm.c``'s ``atomic_store_explicit(&h->read_seq, rseq + 1, …)``
    under ``if (!reader_is_live(…))``; the Rust writer's
    ``free_run_drop_oldest`` does the same, and attach resync sets
    ``read_seq = write_seq`` outright). So a ``read_seq``-flat clause holds only
    inside the pre-demotion grace and goes FALSE exactly when the drops start —
    an alarm that switches itself off at the onset of its own fault.

    Sweeping BOTH sequence cursors across flat, advancing, and
    resynced-to-each-other while holding the heartbeats fixed proves the verdict
    is independent of either.
    """
    path = _ring_file(
        tmp_path,
        writer_hb=FRESH,
        reader_hb=STALE,
        read_seq=read_seq,
        write_seq=write_seq,
    )
    verdict = ring_stall_verdict(path, now_ns=NOW)
    assert verdict.stalled is True, (
        f"read_seq={read_seq} write_seq={write_seq} changed the verdict — the "
        "alarm has re-imported the self-silencing inversion D1 removed"
    )


# --- the doctor surface ------------------------------------------------------


def test_the_doctor_check_reports_a_stalled_active_ring(tmp_path, monkeypatch):
    """The doctor takes its own ``time.monotonic_ns()``, so this fixture's
    heartbeats are anchored to the REAL clock rather than the synthetic epoch
    the unit tests above use."""
    import time

    from jasper.cli.doctor import audio_runtime

    real_now = time.monotonic_ns()
    stalled = _ring_file(
        tmp_path,
        writer_hb=real_now - 5_000_000,
        reader_hb=max(real_now - 3_000_000_000, 1),
    )
    monkeypatch.setattr(ring_assets, "RING_A_PROGRAM_FILE", str(tmp_path / "absent-a"))
    monkeypatch.setattr(ring_assets, "RING_B_CONTENT_FILE", str(tmp_path / "absent-b"))
    monkeypatch.setattr(ring_assets, "RING_ACTIVE_CONTENT_FILE", stalled)
    result = audio_runtime.check_ring_reader_stall()
    assert result.status == "warn"
    assert result.reason == audio_runtime.REASON_RING_READER_STALLED


def test_the_doctor_check_is_silent_on_an_unarmed_box(tmp_path, monkeypatch):
    """Every box in the fleet today has no ring files at all."""
    from jasper.cli.doctor import audio_runtime

    for attr in (
        "RING_A_PROGRAM_FILE",
        "RING_B_CONTENT_FILE",
        "RING_ACTIVE_CONTENT_FILE",
    ):
        monkeypatch.setattr(ring_assets, attr, str(tmp_path / f"absent-{attr}"))
    result = audio_runtime.check_ring_reader_stall()
    assert result.status == "skipped"
    assert result.reason == audio_runtime.REASON_RING_READER_NO_LIVE_RING


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
