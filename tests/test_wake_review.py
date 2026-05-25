"""Tests for jasper.cli.wake_review.

Builds a synthetic CSV + corpus, verifies the review package output
(HTML rendering, README + verdict templates, clip placement). Pure-
Python, no audio dependencies — only stdlib + pytest.
"""
from __future__ import annotations

import csv
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from jasper.cli import wake_review
from jasper.cli.wake_review import ScoreRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav(path: Path, samples: int = 1600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(samples, dtype=np.int16).tobytes())


def _write_scores_csv(
    path: Path,
    rows: list[dict],
    fields: tuple[str, ...] | None = None,
) -> None:
    if fields is None:
        fields = (
            "path", "leg", "condition", "split",
            "peak_score", "mean_score", "frame_count",
            "fired", "duration_sec",
        )
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _sample_row(**overrides) -> dict:
    base = {
        "path": "aec_on_music/train/clip01.wav",
        "leg": "on",
        "condition": "music",
        "split": "train",
        "peak_score": "0.7500",
        "mean_score": "0.3200",
        "frame_count": "20",
        "fired": "1",
        "duration_sec": "1.600",
    }
    base.update(overrides)
    return base


def _sample_score_row(**overrides) -> ScoreRow:
    """Build a typed ScoreRow with sensible defaults."""
    defaults = dict(
        path=Path("aec_on_music/train/clip01.wav"),
        leg="on",
        condition="music",
        split="train",
        peak_score=0.75,
        mean_score=0.32,
        frame_count=20,
        fired=True,
        duration_sec=1.6,
    )
    defaults.update(overrides)
    return ScoreRow(**defaults)


# ---------------------------------------------------------------------------
# load_scores
# ---------------------------------------------------------------------------


def test_load_scores_parses_valid_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "scores.csv"
    _write_scores_csv(csv_path, [
        _sample_row(),
        _sample_row(path="aec_off_nomusic/eval/clip02.wav",
                    leg="off", condition="nomusic", split="eval",
                    fired="0", peak_score="0.0500"),
    ])

    rows = wake_review.load_scores(csv_path)
    assert len(rows) == 2
    assert rows[0].leg == "on"
    assert rows[0].fired is True
    assert rows[0].peak_score == pytest.approx(0.75)
    assert rows[1].fired is False
    assert rows[1].peak_score == pytest.approx(0.05)


def test_load_scores_missing_columns_raises(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    _write_scores_csv(
        csv_path, [{"path": "x.wav", "leg": "on", "condition": "music",
                    "split": "train", "peak_score": "0.5", "fired": "1"}],
        fields=("path", "leg", "condition", "split", "peak_score", "fired"),
    )
    with pytest.raises(ValueError, match="missing required columns"):
        wake_review.load_scores(csv_path)


def test_load_scores_empty_file_raises(tmp_path: Path) -> None:
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("")
    with pytest.raises(ValueError, match="empty or unreadable"):
        wake_review.load_scores(csv_path)


def test_load_scores_no_data_rows_returns_empty(tmp_path: Path) -> None:
    """Header-only CSV returns an empty list, not a crash."""
    csv_path = tmp_path / "header_only.csv"
    _write_scores_csv(csv_path, [])
    assert wake_review.load_scores(csv_path) == []


def test_load_scores_handles_zero_fired_string(tmp_path: Path) -> None:
    """CSV stores fired as 0/1 strings; loader must convert correctly."""
    csv_path = tmp_path / "scores.csv"
    _write_scores_csv(csv_path, [
        _sample_row(fired="0"),
        _sample_row(fired="1"),
    ])
    rows = wake_review.load_scores(csv_path)
    assert rows[0].fired is False
    assert rows[1].fired is True


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def test_build_index_html_includes_every_row() -> None:
    rows = [
        _sample_score_row(path=Path("aec_on_music/train/clip01.wav"),
                          peak_score=0.9),
        _sample_score_row(path=Path("aec_off_nomusic/eval/clip02.wav"),
                          leg="off", condition="nomusic", split="eval",
                          peak_score=0.3, fired=False),
    ]
    html_text = wake_review.build_index_html(rows, title="Test review")
    assert "clip01.wav" in html_text
    assert "clip02.wav" in html_text
    assert "YES" in html_text  # fired=True
    assert ">no<" in html_text  # fired=False (lowercase per the renderer)
    assert "Test review" in html_text


def test_build_index_html_sorts_descending_by_peak_score() -> None:
    rows = [
        _sample_score_row(path=Path("low.wav"), peak_score=0.1),
        _sample_score_row(path=Path("high.wav"), peak_score=0.9),
        _sample_score_row(path=Path("mid.wav"), peak_score=0.5),
    ]
    html_text = wake_review.build_index_html(rows, title="Sort test")
    high_idx = html_text.index("high.wav")
    mid_idx = html_text.index("mid.wav")
    low_idx = html_text.index("low.wav")
    assert high_idx < mid_idx < low_idx, (
        "rows should be sorted by peak score descending"
    )


def test_build_index_html_top_bottom_for_large_corpus() -> None:
    """When >2N clips, render top-N + bottom-N sections separately
    (the middle is uninteresting for review)."""
    rows = [
        _sample_score_row(path=Path(f"clip{i:03d}.wav"),
                          peak_score=i / 100.0)
        for i in range(100)
    ]
    html_text = wake_review.build_index_html(rows, title="Big", top_n=10)
    # The HTML must explicitly label both sections
    assert "Top 10" in html_text
    assert "Bottom 10" in html_text
    # clip099 (peak 0.99) should appear in the top; clip000 in the bottom.
    assert "clip099.wav" in html_text
    assert "clip000.wav" in html_text
    # A middle clip shouldn't appear (would be in neither top-10 nor bottom-10)
    assert "clip050.wav" not in html_text


def test_build_index_html_single_table_for_small_corpus() -> None:
    """≤2N clips → single "All clips" section, not top/bottom split."""
    rows = [
        _sample_score_row(path=Path(f"clip{i:03d}.wav"), peak_score=i / 10.0)
        for i in range(10)
    ]
    html_text = wake_review.build_index_html(rows, title="Small", top_n=10)
    assert "All clips" in html_text
    assert "Top 10" not in html_text
    assert "Bottom 10" not in html_text


def test_build_index_html_escapes_path_chars() -> None:
    """Filenames containing HTML-special characters must be escaped."""
    rows = [
        _sample_score_row(
            path=Path("aec_on_music/train/danger<script>.wav"),
        ),
    ]
    html_text = wake_review.build_index_html(rows, title="Escape test")
    assert "<script>" not in html_text  # would mean unescaped
    assert "&lt;script&gt;" in html_text


def test_build_index_html_handles_empty_rows() -> None:
    """Zero clips → still renders a valid HTML doc; just no table body."""
    html_text = wake_review.build_index_html([], title="Empty")
    assert "<html>" in html_text
    assert "Empty" in html_text


# ---------------------------------------------------------------------------
# README + verdict template
# ---------------------------------------------------------------------------


def test_render_readme_includes_title_and_timestamp() -> None:
    now = datetime(2026, 5, 25, 14, 30, 0, tzinfo=timezone.utc)
    md = wake_review.render_readme("My Run", now=now)
    assert "My Run" in md
    assert "2026-05-25 14:30 UTC" in md
    # Must point to the right next-action files
    assert "index.html" in md
    assert "YOUR_VERDICT.md" in md


def test_render_verdict_template_has_decision_section() -> None:
    md = wake_review.render_verdict_template("Run 1")
    assert "Run 1" in md
    # The four decision options must all be present so the operator
    # can check the right box.
    assert "Approve as-is" in md
    assert "Approve with caveats" in md
    assert "Reject" in md
    assert "investigate further" in md


# ---------------------------------------------------------------------------
# write_review_package — full end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus_with_clips(tmp_path: Path) -> tuple[Path, list[Path]]:
    """Corpus dir + a few WAV files inside it."""
    corpus = tmp_path / "corpus"
    paths = [
        corpus / "aec_on_music/train/clip01.wav",
        corpus / "aec_off_nomusic/eval/clip02.wav",
    ]
    for p in paths:
        _make_wav(p)
    return corpus, paths


def test_write_review_package_creates_all_files(
    tmp_path: Path, corpus_with_clips: tuple[Path, list[Path]],
) -> None:
    corpus, clip_paths = corpus_with_clips
    rows = [
        _sample_score_row(path=clip_paths[0], peak_score=0.8),
        _sample_score_row(path=clip_paths[1], leg="off",
                          condition="nomusic", split="eval",
                          peak_score=0.2, fired=False),
    ]
    output = tmp_path / "review"
    wake_review.write_review_package(rows, corpus, output, title="Run")

    assert (output / "index.html").is_file()
    assert (output / "README.md").is_file()
    assert (output / "YOUR_VERDICT.md").is_file()
    assert (output / "clips").is_dir()
    assert (output / "clips" / "clip01.wav").is_file()
    assert (output / "clips" / "clip02.wav").is_file()


def test_write_review_package_copies_scores_csv_when_provided(
    tmp_path: Path, corpus_with_clips: tuple[Path, list[Path]],
) -> None:
    corpus, clip_paths = corpus_with_clips
    rows = [_sample_score_row(path=clip_paths[0])]
    scores_csv = tmp_path / "scores.csv"
    _write_scores_csv(scores_csv, [_sample_row()])

    output = tmp_path / "review"
    wake_review.write_review_package(
        rows, corpus, output, scores_csv=scores_csv,
    )
    assert (output / "scores.csv").is_file()


def test_write_review_package_skips_missing_clip_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    corpus_with_clips: tuple[Path, list[Path]],
) -> None:
    corpus, clip_paths = corpus_with_clips
    rows = [
        _sample_score_row(path=clip_paths[0]),
        _sample_score_row(path=Path("aec_on_music/train/ghost.wav")),
    ]
    output = tmp_path / "review"
    import logging
    with caplog.at_level(logging.WARNING, logger="jasper-wake-review"):
        wake_review.write_review_package(rows, corpus, output)

    # The real clip was placed; the ghost was skipped + warned
    assert (output / "clips" / "clip01.wav").exists()
    assert not (output / "clips" / "ghost.wav").exists()
    assert any("ghost.wav" in rec.message for rec in caplog.records)


def test_write_review_package_symlink_mode(
    tmp_path: Path, corpus_with_clips: tuple[Path, list[Path]],
) -> None:
    corpus, clip_paths = corpus_with_clips
    rows = [_sample_score_row(path=clip_paths[0])]

    output = tmp_path / "review"
    wake_review.write_review_package(
        rows, corpus, output, symlink=True,
    )
    linked = output / "clips" / "clip01.wav"
    assert linked.is_symlink()
    # The symlink must resolve to the source clip (absolute path so it
    # survives cwd changes when the HTML is opened).
    assert linked.resolve() == clip_paths[0].resolve()


def test_write_review_package_rejects_path_escaping_corpus(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Defense-in-depth: a CSV with a hand-edited path like
    `../../etc/passwd` (or any other escape) must be skipped, not
    silently bundled into the review package."""
    import logging

    # Set up: a corpus dir + a "secret" file OUTSIDE it that an
    # attacker might try to exfiltrate via a crafted CSV.
    corpus = tmp_path / "corpus"
    (corpus / "aec_on_music" / "train").mkdir(parents=True)
    _make_wav(corpus / "aec_on_music" / "train" / "ok.wav")
    secret = tmp_path / "secret.wav"
    _make_wav(secret)

    # A row referencing a relative path that resolves OUTSIDE corpus.
    bad_row = _sample_score_row(path=Path("../secret.wav"))
    good_row = _sample_score_row(
        path=corpus / "aec_on_music/train/ok.wav",
    )

    output = tmp_path / "review"
    with caplog.at_level(logging.WARNING, logger="jasper-wake-review"):
        wake_review.write_review_package(
            [bad_row, good_row], corpus, output,
        )

    # The escaping path got skipped + warned.
    assert any("escapes corpus_dir" in rec.message
               for rec in caplog.records)
    # The secret was NOT bundled into clips/.
    assert not (output / "clips" / "secret.wav").exists()
    # But the legit clip WAS bundled.
    assert (output / "clips" / "ok.wav").exists()


def test_write_review_package_refuses_non_empty_dir(
    tmp_path: Path, corpus_with_clips: tuple[Path, list[Path]],
) -> None:
    corpus, clip_paths = corpus_with_clips
    rows = [_sample_score_row(path=clip_paths[0])]

    output = tmp_path / "review"
    output.mkdir()
    (output / "old_verdict.md").write_text("previous notes — don't lose me")

    with pytest.raises(ValueError, match="non-empty"):
        wake_review.write_review_package(rows, corpus, output)
    # Old file untouched
    assert (output / "old_verdict.md").read_text().startswith("previous")


# ---------------------------------------------------------------------------
# main() — CLI smoke tests
# ---------------------------------------------------------------------------


def test_main_errors_on_missing_csv(tmp_path: Path) -> None:
    rc = wake_review.main([
        str(tmp_path / "nope.csv"),
        str(tmp_path),
        "--output", str(tmp_path / "out"),
    ])
    assert rc == 2


def test_main_errors_on_missing_corpus(tmp_path: Path) -> None:
    csv_path = tmp_path / "scores.csv"
    _write_scores_csv(csv_path, [_sample_row()])
    rc = wake_review.main([
        str(csv_path),
        str(tmp_path / "no_corpus"),
        "--output", str(tmp_path / "out"),
    ])
    assert rc == 2


def test_main_errors_on_empty_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "empty.csv"
    _write_scores_csv(csv_path, [])  # header only

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    rc = wake_review.main([
        str(csv_path), str(corpus),
        "--output", str(tmp_path / "out"),
    ])
    assert rc == 1


def test_main_end_to_end(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    clip = corpus / "aec_on_music/train/clip01.wav"
    _make_wav(clip)

    csv_path = tmp_path / "scores.csv"
    _write_scores_csv(csv_path, [_sample_row(path=str(clip))])

    output = tmp_path / "out"
    rc = wake_review.main([
        str(csv_path), str(corpus),
        "--output", str(output),
        "--title", "Smoke test",
    ])
    assert rc == 0
    assert (output / "index.html").is_file()
    assert "Smoke test" in (output / "index.html").read_text()
