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


def test_runtime_integrity_flags_camilla_clipping_delta(monkeypatch):
    from jasper.correction import runtime_integrity

    monkeypatch.setattr(runtime_integrity, "_read_fanin_status", lambda: None)
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
