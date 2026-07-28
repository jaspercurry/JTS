# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import wave
from pathlib import Path

from jasper.correction.runtime_integrity import RuntimeIntegrityReport


def _write_silent_wav(path: Path, *, sample_rate: int, frames: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)


def test_runtime_integrity_records_capture_sample_sanity(tmp_path: Path):
    path = tmp_path / "captures" / "p0.wav"
    _write_silent_wav(path, sample_rate=48_000, frames=48_000)
    report = RuntimeIntegrityReport("abc123")

    issues = report.record_capture(
        path,
        capture_kind="measurement",
        position_index=0,
        artifact_path="captures/p0.wav",
        expected_sample_rate=48_000,
        expected_sweep_samples=48_000,
        expected_sweep_duration_s=1.0,
    )

    assert issues == []
    assert report.summary()["level"] == "ok"
    assert report.captures[0]["artifact_path"] == "captures/p0.wav"
    assert report.captures[0]["frames"] == 48_000
    assert report.captures[0]["sample_delta_vs_sweep"] == 0


def test_runtime_integrity_flags_truncated_capture(tmp_path: Path):
    path = tmp_path / "captures" / "p0.wav"
    _write_silent_wav(path, sample_rate=48_000, frames=24_000)
    report = RuntimeIntegrityReport("abc123")

    issues = report.record_capture(
        path,
        capture_kind="measurement",
        position_index=0,
        artifact_path="captures/p0.wav",
        expected_sample_rate=48_000,
        expected_sweep_samples=48_000,
        expected_sweep_duration_s=1.0,
    )

    assert report.summary()["level"] == "fail"
    assert issues[0]["code"] == "runtime_capture_too_short"
    assert issues[0]["severity"] == "fail"


def test_runtime_integrity_uses_ram_tier_aware_memory_thresholds(monkeypatch):
    from jasper.correction import runtime_integrity

    monkeypatch.setattr(
        runtime_integrity,
        "_read_meminfo_mb",
        lambda: {"total_mb": 2_000, "available_mb": 150, "used_mb": 1_850},
    )
    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(runtime_integrity, "_read_outputd_status", lambda: None)
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)

    report = RuntimeIntegrityReport("abc123")
    issues = report.record_snapshot("sweep_start")

    assert [issue["code"] for issue in issues] == ["memory_available_low"]
    assert issues[0]["details"]["threshold_mb"] == 200
    assert report.to_dict()["thresholds"] == {
        "load_per_core_warn": runtime_integrity.LOAD_PER_CORE_WARN,
        "mem_available_warn_mb": 200,
        "mem_available_fail_mb": 60,
        "capture_extra_seconds_warn": (
            runtime_integrity.CAPTURE_EXTRA_SECONDS_WARN
        ),
        "capture_extra_ratio_warn": runtime_integrity.CAPTURE_EXTRA_RATIO_WARN,
    }


def test_runtime_integrity_flags_camilla_clipping_delta(monkeypatch):
    from jasper.correction import runtime_integrity

    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start",
        capture_kind="measurement",
        position_index=0,
        camilla_status={"clipped_samples": 1},
    )

    issues = report.record_snapshot(
        "sweep_complete",
        capture_kind="measurement",
        position_index=0,
        camilla_status={"clipped_samples": 7},
    )

    assert issues[0]["code"] == "camilla_clipping_increased"
    assert issues[0]["details"]["delta"] == 6


def test_runtime_integrity_flags_outputd_content_fill_without_any_xrun(monkeypatch):
    """The #1765 shape: outputd zero-fills a short content read mid-capture.

    ``xrun_count`` never moves — that counter only advances on EPIPE/ESTRPIPE
    — so before #1768 this capture passed its own integrity check while the
    speaker had inserted samples into the emitted timeline.
    """
    from jasper.correction import runtime_integrity

    outputd_statuses = iter([
        {
            "content": {
                "pcm": "content", "frames_read": 1_000, "xrun_count": 2,
                "empty_periods": 4, "partial_periods": 5,
            },
            "dac": {"pcm": "dac", "frames_written": 1_000, "xrun_count": 0},
        },
        {
            "content": {
                "pcm": "content", "frames_read": 2_000, "xrun_count": 2,
                "empty_periods": 4, "partial_periods": 6,
            },
            "dac": {"pcm": "dac", "frames_written": 2_000, "xrun_count": 0},
        },
    ])
    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(
        runtime_integrity,
        "_read_outputd_status",
        lambda: next(outputd_statuses),
    )
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start",
        capture_kind="measurement",
        position_index=0,
        camilla_status=None,
    )

    issues = report.record_snapshot(
        "sweep_complete",
        capture_kind="measurement",
        position_index=0,
        camilla_status=None,
    )

    codes = [issue["code"] for issue in issues]
    assert "outputd_content_fill_increased" in codes
    # …and specifically NOT via the xrun path, which stayed flat.
    assert "outputd_xruns_increased" not in codes
    fill = next(i for i in issues if i["code"] == "outputd_content_fill_increased")
    assert fill["details"]["deltas"] == {"partial_periods": 1}
    assert report.snapshots[-1]["outputd"]["content"]["partial_periods"] == 6


def test_runtime_integrity_flags_shm_ring_empty_reads(monkeypatch):
    """The ring topology must arm the SAME gate as the ALSA content hop.

    outputd drives exactly one content source per box, and the SHM ring is the
    resolved default on eligible stereo topologies — so a gate that only read
    `content.empty_periods` was inert on the default topology. Here the ALSA
    counters never move and the ring's do.
    """
    from jasper.correction import runtime_integrity

    content = {"pcm": "content", "frames_read": 1_000, "xrun_count": 0,
               "empty_periods": 0, "partial_periods": 0}
    outputd_statuses = iter([
        {
            "content": dict(content),
            "dac": {"pcm": "dac", "frames_written": 1_000, "xrun_count": 0},
            "shm_ring": {"enabled": True, "empty_reads": 7, "startup_empty_reads": 2},
        },
        {
            "content": dict(content, frames_read=2_000),
            "dac": {"pcm": "dac", "frames_written": 2_000, "xrun_count": 0},
            "shm_ring": {"enabled": True, "empty_reads": 9, "startup_empty_reads": 2},
        },
    ])
    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(
        runtime_integrity, "_read_outputd_status", lambda: next(outputd_statuses),
    )
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start", capture_kind="measurement", position_index=0, camilla_status=None,
    )

    issues = report.record_snapshot(
        "sweep_complete", capture_kind="measurement", position_index=0, camilla_status=None,
    )

    fill = next(i for i in issues if i["code"] == "outputd_content_fill_increased")
    assert fill["details"]["deltas"] == {"ring_empty_reads": 2}
    assert report.snapshots[-1]["outputd"]["shm_ring"]["empty_reads"] == 9


def test_runtime_integrity_ignores_ring_counters_when_ring_is_disabled(monkeypatch):
    """A box on the ALSA hop reports `shm_ring: {enabled: false}` with no
    counters; the gate must not invent keys or trip on their absence."""
    from jasper.correction import runtime_integrity

    outputd_statuses = iter([
        {
            "content": {"pcm": "c", "frames_read": 1_000, "xrun_count": 0,
                        "empty_periods": 4, "partial_periods": 5},
            "dac": {"pcm": "dac", "frames_written": 1_000, "xrun_count": 0},
            "shm_ring": {"enabled": False},
        },
        {
            "content": {"pcm": "c", "frames_read": 2_000, "xrun_count": 0,
                        "empty_periods": 4, "partial_periods": 5},
            "dac": {"pcm": "dac", "frames_written": 2_000, "xrun_count": 0},
            "shm_ring": {"enabled": False},
        },
    ])
    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(
        runtime_integrity, "_read_outputd_status", lambda: next(outputd_statuses),
    )
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start", capture_kind="measurement", position_index=0, camilla_status=None,
    )
    issues = report.record_snapshot(
        "sweep_complete", capture_kind="measurement", position_index=0, camilla_status=None,
    )

    assert [i["code"] for i in issues] == []
    assert report.snapshots[-1]["outputd"]["shm_ring"] == {"enabled": False}


def test_runtime_integrity_quiet_when_content_fill_counters_hold(monkeypatch):
    from jasper.correction import runtime_integrity

    content = {
        "pcm": "content", "frames_read": 1_000, "xrun_count": 2,
        "empty_periods": 4, "partial_periods": 5,
    }
    outputd_statuses = iter([
        {"content": dict(content), "dac": {"pcm": "dac", "frames_written": 1_000, "xrun_count": 0}},
        {
            "content": dict(content, frames_read=2_000),
            "dac": {"pcm": "dac", "frames_written": 2_000, "xrun_count": 0},
        },
    ])
    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(
        runtime_integrity,
        "_read_outputd_status",
        lambda: next(outputd_statuses),
    )
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start", capture_kind="measurement", position_index=0, camilla_status=None,
    )

    issues = report.record_snapshot(
        "sweep_complete", capture_kind="measurement", position_index=0, camilla_status=None,
    )

    assert [i["code"] for i in issues] == []


def test_runtime_integrity_flags_outputd_xrun_delta(monkeypatch):
    from jasper.correction import runtime_integrity

    outputd_statuses = iter([
        {
            "content": {"pcm": "content", "frames_read": 1_000, "xrun_count": 2},
            "dac": {"pcm": "dac", "frames_written": 1_000, "xrun_count": 0},
        },
        {
            "content": {"pcm": "content", "frames_read": 2_000, "xrun_count": 3},
            "dac": {"pcm": "dac", "frames_written": 2_000, "xrun_count": 0},
        },
    ])
    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(
        runtime_integrity,
        "_read_outputd_status",
        lambda: next(outputd_statuses),
    )
    monkeypatch.setattr(runtime_integrity, "_read_loadavg_1m", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start",
        capture_kind="measurement",
        position_index=0,
        camilla_status=None,
    )

    issues = report.record_snapshot(
        "sweep_complete",
        capture_kind="measurement",
        position_index=0,
        camilla_status=None,
    )

    assert issues[0]["code"] == "outputd_xruns_increased"
    assert issues[0]["details"]["deltas"] == {"content": 1}
    assert report.snapshots[-1]["outputd"]["dac"] == {
        "pcm": "dac",
        "frames_written": 2_000,
        "xrun_count": 0,
    }


def test_runtime_snapshot_omits_wizard_process_rss(monkeypatch):
    """The snapshot no longer carries the wizard process's RSS/threads:
    /proc/self/status measured the web worker, not the audio chain, so it
    was misleading runtime-health evidence."""
    from jasper.correction import runtime_integrity

    # The reader function is gone (platform-independent deletion pin).
    assert not hasattr(runtime_integrity, "_read_proc_status")

    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
    monkeypatch.setattr(runtime_integrity, "_read_outputd_status", lambda: None)
    report = RuntimeIntegrityReport("abc123")
    report.record_snapshot(
        "sweep_start",
        capture_kind="measurement",
        position_index=0,
        camilla_status=None,
    )
    snap = report.snapshots[-1]
    assert "process" not in snap
    assert "process_rss_mb" not in snap
