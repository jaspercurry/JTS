# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.cli.wake_score.

Builds synthetic corpora + uses fake detectors so the test suite
stays hardware-free (no openwakeword, no real audio). The injected-
detector pattern in `score_corpus(..., detector=...)` makes the
end-to-end scoring path testable without loading any ONNX model.
"""
from __future__ import annotations

import csv
import logging
import wave
from pathlib import Path

import numpy as np
import pytest

from jasper.cli import wake_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav(
    path: Path,
    samples: int,
    *,
    value: int = 0,
    sample_rate: int = wake_score.SAMPLE_RATE_HZ,
    channels: int = 1,
    sampwidth: int = 2,
) -> None:
    """Write a WAV file with `samples` int16 samples all set to `value`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.full(samples, value, dtype=np.int16).tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


class _FakeDetector:
    """Returns a fixed score regardless of input.

    The simplest possible stand-in for `WakeWordDetector` — confirms
    `score_clip()`'s iteration logic without coupling to any audio
    content."""

    def __init__(self, score: float = 0.0) -> None:
        self._score = score
        self.calls: int = 0

    def score_frame(self, frame: np.ndarray) -> float:
        self.calls += 1
        return self._score


# ---------------------------------------------------------------------------
# parse_quadrant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("aec_on_nomusic", ("on", "nomusic")),
    ("aec_on_music", ("on", "music")),
    ("aec_off_nomusic", ("off", "nomusic")),
    ("aec_off_music", ("off", "music")),
    ("aec_dtln_nomusic", ("dtln", "nomusic")),
    ("aec_dtln_music", ("dtln", "music")),
])
def test_parse_quadrant_valid_names(name: str, expected: tuple[str, str]) -> None:
    assert wake_score.parse_quadrant(name) == expected


@pytest.mark.parametrize("name", [
    "summary.txt",       # corpus-root metadata file
    "manifest.csv",      # extract-wake-corpus output
    "data",              # accidental top-level dir
    "aec_xyz_quiet",     # bad leg
    "aec_on_loud",       # bad condition
    "aec_on",            # too few parts
    "on_nomusic",        # missing aec prefix
    "not_a_quadrant",
    "",
])
def test_parse_quadrant_invalid_returns_none(name: str) -> None:
    assert wake_score.parse_quadrant(name) is None


# ---------------------------------------------------------------------------
# walk_corpus
# ---------------------------------------------------------------------------


def test_walk_corpus_finds_clips_with_split_layout(tmp_path: Path) -> None:
    _make_wav(tmp_path / "aec_on_music" / "train" / "001.aec-on.wav", 1600)
    _make_wav(tmp_path / "aec_on_music" / "eval" / "002.aec-on.wav", 1600)
    _make_wav(tmp_path / "aec_off_nomusic" / "train" / "003.aec-off.wav", 1600)

    clips = list(wake_score.walk_corpus(tmp_path))
    assert len(clips) == 3
    by_name = {c.path.name: c for c in clips}
    assert by_name["001.aec-on.wav"].leg == "on"
    assert by_name["001.aec-on.wav"].condition == "music"
    assert by_name["001.aec-on.wav"].split == "train"
    assert by_name["002.aec-on.wav"].split == "eval"
    assert by_name["003.aec-off.wav"].leg == "off"
    assert by_name["003.aec-off.wav"].condition == "nomusic"


def test_walk_corpus_handles_flat_layout(tmp_path: Path) -> None:
    """Quadrant dirs without train/eval subdirs → split='unknown'."""
    _make_wav(tmp_path / "aec_on_music" / "001.wav", 1600)
    _make_wav(tmp_path / "aec_off_nomusic" / "002.wav", 1600)

    clips = list(wake_score.walk_corpus(tmp_path))
    assert len(clips) == 2
    assert all(c.split == "unknown" for c in clips)


def test_walk_corpus_mixed_split_and_flat(tmp_path: Path) -> None:
    """One quadrant has train/eval, another is flat — both should
    extract correctly with their own split tagging."""
    _make_wav(tmp_path / "aec_on_music" / "train" / "001.wav", 1600)
    _make_wav(tmp_path / "aec_off_nomusic" / "002.wav", 1600)

    clips = {c.path.name: c for c in wake_score.walk_corpus(tmp_path)}
    assert clips["001.wav"].split == "train"
    assert clips["002.wav"].split == "unknown"


def test_walk_corpus_skips_non_quadrant_dirs_and_files(tmp_path: Path) -> None:
    """Non-quadrant entries at corpus root must be ignored — operator
    might keep summary.txt / manifest.csv / random scratch files there."""
    _make_wav(tmp_path / "aec_on_music" / "train" / "ok.wav", 1600)
    (tmp_path / "summary.txt").write_text("text")
    (tmp_path / "manifest.csv").write_text("path,leg")
    (tmp_path / "random_dir").mkdir()
    _make_wav(tmp_path / "random_dir" / "ignored.wav", 1600)

    clips = list(wake_score.walk_corpus(tmp_path))
    assert len(clips) == 1
    assert clips[0].path.name == "ok.wav"


def test_walk_corpus_rejects_non_directory(tmp_path: Path) -> None:
    not_dir = tmp_path / "file.txt"
    not_dir.write_text("not a dir")
    with pytest.raises(ValueError, match="not a directory"):
        list(wake_score.walk_corpus(not_dir))


def test_walk_corpus_empty_returns_no_clips(tmp_path: Path) -> None:
    assert list(wake_score.walk_corpus(tmp_path)) == []


def test_walk_corpus_deterministic_order(tmp_path: Path) -> None:
    """Order must be reproducible across machines — sorted by dir then
    by filename. Operators rely on the CSV row order being stable when
    diffing runs."""
    for name in ("c.wav", "a.wav", "b.wav"):
        _make_wav(tmp_path / "aec_on_music" / "train" / name, 1600)

    order = [c.path.name for c in wake_score.walk_corpus(tmp_path)]
    assert order == ["a.wav", "b.wav", "c.wav"]


# ---------------------------------------------------------------------------
# read_pcm
# ---------------------------------------------------------------------------


def test_read_pcm_returns_int16_array(tmp_path: Path) -> None:
    path = tmp_path / "ok.wav"
    _make_wav(path, samples=1600, value=42)
    pcm = wake_score.read_pcm(path)
    assert pcm.dtype == np.int16
    assert len(pcm) == 1600
    assert (pcm == 42).all()


def test_read_pcm_rejects_wrong_sample_rate(tmp_path: Path) -> None:
    path = tmp_path / "bad_rate.wav"
    _make_wav(path, samples=1600, sample_rate=8000)
    with pytest.raises(ValueError, match="expected 16000 Hz"):
        wake_score.read_pcm(path)


def test_read_pcm_rejects_stereo(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    _make_wav(path, samples=1600, channels=2)
    with pytest.raises(ValueError, match="expected mono"):
        wake_score.read_pcm(path)


def test_read_pcm_rejects_8bit(tmp_path: Path) -> None:
    path = tmp_path / "8bit.wav"
    _make_wav(path, samples=1600, sampwidth=1)
    with pytest.raises(ValueError, match="expected 16-bit"):
        wake_score.read_pcm(path)


# ---------------------------------------------------------------------------
# score_clip
# ---------------------------------------------------------------------------


def test_score_clip_returns_peak_mean_count(tmp_path: Path) -> None:
    detector = _FakeDetector(score=0.7)
    pcm = np.zeros(wake_score.FRAME_SAMPLES * 3, dtype=np.int16)
    peak, mean, n_frames, fired = wake_score.score_clip(
        detector, pcm, threshold=0.5,
    )
    assert n_frames == 3
    assert detector.calls == 3
    assert peak == pytest.approx(0.7)
    assert mean == pytest.approx(0.7)
    assert fired is True


def test_score_clip_below_threshold_does_not_fire() -> None:
    detector = _FakeDetector(score=0.3)
    pcm = np.zeros(wake_score.FRAME_SAMPLES * 2, dtype=np.int16)
    peak, _, _, fired = wake_score.score_clip(detector, pcm, threshold=0.5)
    assert peak == pytest.approx(0.3)
    assert fired is False


def test_score_clip_skips_partial_tail() -> None:
    """1.5 frames worth → only the full frame gets scored. Otherwise
    an under-sized window distorts the peak."""
    detector = _FakeDetector(score=0.5)
    pcm = np.zeros(int(wake_score.FRAME_SAMPLES * 1.5), dtype=np.int16)
    _, _, n_frames, _ = wake_score.score_clip(detector, pcm, threshold=0.5)
    assert n_frames == 1
    assert detector.calls == 1


def test_score_clip_empty_pcm_returns_zeros() -> None:
    detector = _FakeDetector(score=0.9)
    pcm = np.array([], dtype=np.int16)
    peak, mean, n_frames, fired = wake_score.score_clip(
        detector, pcm, threshold=0.5,
    )
    assert peak == 0.0
    assert mean == 0.0
    assert n_frames == 0
    assert fired is False
    assert detector.calls == 0


def test_score_clip_peak_is_max_not_last() -> None:
    """If scores rise then fall, peak must be the highest value seen,
    not the most recent."""

    class _RisingFalling:
        def __init__(self) -> None:
            self.idx = 0
            self.scores = [0.10, 0.40, 0.90, 0.30, 0.10]

        def score_frame(self, frame: np.ndarray) -> float:
            s = self.scores[self.idx]
            self.idx += 1
            return s

    detector = _RisingFalling()
    pcm = np.zeros(wake_score.FRAME_SAMPLES * 5, dtype=np.int16)
    peak, mean, _, _ = wake_score.score_clip(detector, pcm, threshold=0.5)
    assert peak == pytest.approx(0.9)
    assert mean == pytest.approx(sum([0.10, 0.40, 0.90, 0.30, 0.10]) / 5)


def test_score_clip_threshold_at_peak_exact_match_fires() -> None:
    """`fired` is `peak >= threshold` — exact match counts as fire,
    consistent with WakeWordDetector's threshold semantics."""
    detector = _FakeDetector(score=0.5)
    pcm = np.zeros(wake_score.FRAME_SAMPLES, dtype=np.int16)
    _, _, _, fired = wake_score.score_clip(detector, pcm, threshold=0.5)
    assert fired is True


# ---------------------------------------------------------------------------
# score_corpus (end-to-end with injected detector)
# ---------------------------------------------------------------------------


def test_score_corpus_end_to_end(tmp_path: Path) -> None:
    # 4 clips across 2 quadrants × 2 splits.
    _make_wav(tmp_path / "aec_on_music" / "train" / "01.wav",
              wake_score.FRAME_SAMPLES * 2)
    _make_wav(tmp_path / "aec_on_music" / "eval" / "02.wav",
              wake_score.FRAME_SAMPLES * 2)
    _make_wav(tmp_path / "aec_off_nomusic" / "train" / "03.wav",
              wake_score.FRAME_SAMPLES * 2)
    _make_wav(tmp_path / "aec_off_nomusic" / "eval" / "04.wav",
              wake_score.FRAME_SAMPLES * 2)

    detector = _FakeDetector(score=0.6)
    scored = wake_score.score_corpus(
        tmp_path, "fake.onnx", threshold=0.5, detector=detector,
    )

    assert len(scored) == 4
    assert all(c.fired for c in scored)
    assert all(c.frame_count == 2 for c in scored)
    assert all(c.duration_sec == pytest.approx(
        2 * wake_score.FRAME_SAMPLES / wake_score.SAMPLE_RATE_HZ,
    ) for c in scored)

    by_name = {c.meta.path.name: c for c in scored}
    assert by_name["01.wav"].meta.leg == "on"
    assert by_name["01.wav"].meta.condition == "music"
    assert by_name["03.wav"].meta.leg == "off"


def test_score_corpus_skips_invalid_wavs_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """One bad clip shouldn't take down the whole run; the operator
    sees the skipped path via the log."""
    _make_wav(tmp_path / "aec_on_music" / "train" / "ok.wav",
              wake_score.FRAME_SAMPLES)
    _make_wav(tmp_path / "aec_on_music" / "train" / "bad.wav",
              1600, sample_rate=8000)

    detector = _FakeDetector(score=0.5)
    with caplog.at_level(logging.WARNING, logger="jasper-wake-score"):
        scored = wake_score.score_corpus(
            tmp_path, "fake.onnx", detector=detector,
        )

    assert len(scored) == 1
    assert scored[0].meta.path.name == "ok.wav"
    assert any("bad.wav" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


def test_write_csv_has_expected_columns_and_values(tmp_path: Path) -> None:
    _make_wav(tmp_path / "aec_on_music" / "train" / "01.wav",
              wake_score.FRAME_SAMPLES)
    detector = _FakeDetector(score=0.7)
    scored = wake_score.score_corpus(
        tmp_path, "fake.onnx", detector=detector,
    )

    out = tmp_path / "scores.csv"
    wake_score.write_csv(scored, out)

    rows = list(csv.DictReader(open(out)))
    assert len(rows) == 1
    assert set(rows[0].keys()) == set(wake_score.CSV_FIELDS)
    assert rows[0]["leg"] == "on"
    assert rows[0]["condition"] == "music"
    assert rows[0]["split"] == "train"
    assert rows[0]["fired"] == "1"
    assert float(rows[0]["peak_score"]) == pytest.approx(0.7)


def test_write_csv_atomic_no_tempfile_left(tmp_path: Path) -> None:
    _make_wav(tmp_path / "aec_on_music" / "train" / "01.wav",
              wake_score.FRAME_SAMPLES)
    detector = _FakeDetector(score=0.5)
    scored = wake_score.score_corpus(
        tmp_path, "fake.onnx", detector=detector,
    )

    out = tmp_path / "scores.csv"
    wake_score.write_csv(scored, out)

    assert out.exists()
    assert not out.with_suffix(out.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


def test_format_summary_contains_all_groups(tmp_path: Path) -> None:
    # Build clips across all 4 (leg, condition, split) cells we test.
    cells = [
        ("on", "music", "train"),
        ("on", "music", "eval"),
        ("off", "nomusic", "train"),
        ("off", "nomusic", "eval"),
    ]
    for leg, condition, split in cells:
        _make_wav(
            tmp_path / f"aec_{leg}_{condition}" / split / f"{split}.wav",
            wake_score.FRAME_SAMPLES * 2,
        )

    detector = _FakeDetector(score=0.6)
    scored = wake_score.score_corpus(
        tmp_path, "fake.onnx", detector=detector,
    )
    summary = wake_score.format_summary(scored, threshold=0.5)

    # Every leg + condition + split label should appear at least
    # once. We don't pin exact whitespace because the table format
    # might evolve.
    for leg in ("on", "off"):
        assert leg in summary
    for condition in ("music", "nomusic"):
        assert condition in summary
    for split in ("train", "eval"):
        assert split in summary
    assert "Threshold: 0.5" in summary
    assert "Total clips: 4" in summary


def test_format_summary_handles_empty_input() -> None:
    summary = wake_score.format_summary([], threshold=0.5)
    assert "Total clips: 0" in summary


def test_format_summary_recall_percentage_correct() -> None:
    """3 fires out of 4 clips → 75% recall in the summary."""
    meta = wake_score.ClipMeta(
        path=Path("x.wav"), leg="on", condition="music", split="train",
    )
    clips = [
        wake_score.ScoredClip(meta=meta, peak_score=0.9, mean_score=0.5,
                              frame_count=1, fired=True, duration_sec=0.08),
        wake_score.ScoredClip(meta=meta, peak_score=0.8, mean_score=0.5,
                              frame_count=1, fired=True, duration_sec=0.08),
        wake_score.ScoredClip(meta=meta, peak_score=0.6, mean_score=0.5,
                              frame_count=1, fired=True, duration_sec=0.08),
        wake_score.ScoredClip(meta=meta, peak_score=0.1, mean_score=0.5,
                              frame_count=1, fired=False, duration_sec=0.08),
    ]
    summary = wake_score.format_summary(clips, threshold=0.5)
    assert "75.0%" in summary


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_errors_on_missing_corpus_dir(tmp_path: Path) -> None:
    bogus = tmp_path / "does_not_exist"
    rc = wake_score.main([str(bogus), "fake.onnx"])
    assert rc == 2


def test_main_errors_on_empty_corpus(tmp_path: Path) -> None:
    # Directory exists but has no matching quadrant subdirs.
    rc = wake_score.main([str(tmp_path), "fake.onnx"])
    assert rc == 1


# Note: main() with a real (non-mock) detector would require
# openwakeword on the test machine. Skipped — the score_corpus tests
# via injected detector exercise the same code path otherwise.
