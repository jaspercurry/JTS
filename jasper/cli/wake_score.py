# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-wake-score — Score a corpus of WAVs through a wake-word model.

Walks a directory laid out as `aec_{leg}_{condition}/{split}/*.wav`
(the output of `scripts/_extract_wake_corpus.py` or
`jasper-wake-enroll`), runs each WAV through an openWakeWord-
compatible ONNX model, and reports per-clip peak scores + aggregate
metrics by (leg, condition, split).

Use cases (per `docs/HANDOFF-wake-training-experiment.md`):
- Phase 0c: establish baseline metrics with `jarvis_v2` against the
  gold corpus
- Phase 1e: A/B trained per-leg models vs the baseline against the
  held-out corpus split

Output:
- CSV: one row per clip with peak score, frame count, did-fire flag
- stdout: tabular per-(leg, condition, split) aggregate summary

The module is hardware-free at import time — `openwakeword` is lazy-
imported inside `WakeWordDetector` (which we reuse from
`jasper.wake`). Pure-function helpers (`walk_corpus`, `score_clip`,
`format_summary`) are testable without any audio dependencies; the
test suite exercises them with synthetic WAVs + fake detectors.

Pair with `jasper-wake-review` to build a listening-review package on
top of the CSV.

Usage:
  jasper-wake-score CORPUS_DIR MODEL [--threshold 0.5] [--output scores.csv]

Examples:
  # Baseline jarvis_v2 against a fresh gold corpus
  jasper-wake-score data/real_positives \\
      /var/lib/jasper/wake/jarvis_v2.onnx \\
      --output baselines/jarvis_v2.csv

  # A/B a newly-trained model against the same corpus + threshold
  jasper-wake-score data/real_positives \\
      output/jarvis_jts_aec_v1.onnx \\
      --threshold 0.5 \\
      --output runs/jarvis_jts_aec_v1.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import wave
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

from jasper.wake_conditions import CORPUS_DIR_CONDITIONS

logger = logging.getLogger("jasper-wake-score")


# Mirrors `jasper.wake_events.SAMPLE_RATE_HZ` and the rest of the
# audio pipeline. Anything else means the WAVs in the corpus don't
# match what the wake-word model expects, and `read_pcm()` rejects
# them loudly.
SAMPLE_RATE_HZ = 16000

# openWakeWord's frame stride: 1280 samples = 80 ms at 16 kHz. Each
# `WakeWordDetector.score_frame()` call expects exactly this. Smaller
# windows produce undersized model input + misleading scores.
FRAME_SAMPLES = 1280

# Default wake threshold. Matches `jasper.config` and the `/wake/`
# wizard. Per-clip "fired" classification uses this unless overridden
# via --threshold; threshold sweeps come later via repeated runs.
DEFAULT_THRESHOLD = 0.5

# Quadrant naming. Base legs match `extract_wake_corpus.QUADRANTS` and
# `wake_enroll.all_quadrant_dirs()`; directory-condition tokens come from the
# writer/reader contract in `jasper.wake_conditions` so browser-recorder
# `ambient` clips cannot silently disappear from scoring.
LEGS = ("on", "off", "dtln")
SPLITS = ("train", "eval")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipMeta:
    """A single corpus clip with its inferred metadata.

    `leg` is "on", "off", or "dtln" (matching the `audio_<leg>_path`
    schema convention); `condition` is the on-disk corpus token
    "nomusic", "ambient", or "music"; `split` is "train", "eval", or
    "unknown" (the last covers flat-layout corpora that don't have
    train/eval subdirs).
    """

    path: Path
    leg: str
    condition: str
    split: str


@dataclass
class ScoredClip:
    """A clip plus its scoring result."""

    meta: ClipMeta
    peak_score: float
    mean_score: float
    frame_count: int
    fired: bool
    duration_sec: float


# ---------------------------------------------------------------------------
# Corpus discovery
# ---------------------------------------------------------------------------


def parse_quadrant(quadrant_name: str) -> tuple[str, str] | None:
    """Parse `aec_<leg>_<condition>` into `(leg, condition)`.

    Returns None on any name that doesn't match — lets the caller
    skip non-quadrant files at the corpus root (`manifest.csv`,
    `summary.txt`, etc.) without special-casing them.
    """
    parts = quadrant_name.split("_")
    if len(parts) != 3 or parts[0] != "aec":
        return None
    _, leg, condition = parts
    if leg not in LEGS or condition not in CORPUS_DIR_CONDITIONS:
        return None
    return leg, condition


def walk_corpus(corpus_dir: Path) -> Iterator[ClipMeta]:
    """Yield every WAV in the corpus, tagged with leg/condition/split.

    Expected layout:
      <corpus_dir>/aec_<leg>_<condition>/<split>/*.wav

    Where `split` is "train" or "eval". A flat layout (no split
    subdir) is also supported for backward compat with corpora that
    don't have a held-out split — those clips get `split="unknown"`.

    Order: sorted by directory, then by filename, so output ordering
    is deterministic across machines + filesystems.
    """
    if not corpus_dir.is_dir():
        raise ValueError(f"{corpus_dir} is not a directory")

    for quadrant_dir in sorted(corpus_dir.iterdir()):
        if not quadrant_dir.is_dir():
            continue
        parsed = parse_quadrant(quadrant_dir.name)
        if parsed is None:
            if quadrant_dir.name.startswith("aec_"):
                logger.warning(
                    "skipping unrecognized corpus quadrant directory: %s",
                    quadrant_dir,
                )
            continue
        leg, condition = parsed

        split_dirs = [
            d for d in quadrant_dir.iterdir()
            if d.is_dir() and d.name in SPLITS
        ]
        if split_dirs:
            for split_dir in sorted(split_dirs):
                for wav_path in sorted(split_dir.glob("*.wav")):
                    yield ClipMeta(
                        path=wav_path,
                        leg=leg,
                        condition=condition,
                        split=split_dir.name,
                    )
        else:
            for wav_path in sorted(quadrant_dir.glob("*.wav")):
                yield ClipMeta(
                    path=wav_path,
                    leg=leg,
                    condition=condition,
                    split="unknown",
                )


# ---------------------------------------------------------------------------
# WAV reading + scoring (pure helpers, fully testable)
# ---------------------------------------------------------------------------


def read_pcm(wav_path: Path) -> np.ndarray:
    """Read a WAV file and return int16 PCM samples.

    Validates the format matches what wake-word models expect (16
    kHz mono int16). Raises `ValueError` on mismatch — better to
    fail loudly than to score garbage and call the result "low
    recall."
    """
    with wave.open(str(wav_path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE_HZ:
            raise ValueError(
                f"{wav_path}: expected {SAMPLE_RATE_HZ} Hz, "
                f"got {w.getframerate()} Hz"
            )
        if w.getnchannels() != 1:
            raise ValueError(
                f"{wav_path}: expected mono, "
                f"got {w.getnchannels()} channels"
            )
        if w.getsampwidth() != 2:
            raise ValueError(
                f"{wav_path}: expected 16-bit (sampwidth=2), "
                f"got sampwidth={w.getsampwidth()}"
            )
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def score_clip(
    detector, pcm: np.ndarray, threshold: float,
) -> tuple[float, float, int, bool]:
    """Score a single clip frame-by-frame.

    Returns `(peak_score, mean_score, frame_count, fired)`. `detector`
    must expose `score_frame(np.ndarray) -> float` — matches
    `WakeWordDetector` from `jasper.wake` and the fake detectors used
    in tests.

    Partial trailing frames (less than FRAME_SAMPLES) are skipped —
    feeding the detector an undersized window produces a misleading
    score that would inflate or deflate the peak.
    """
    n_full_frames = len(pcm) // FRAME_SAMPLES
    if n_full_frames == 0:
        return 0.0, 0.0, 0, False
    scores: list[float] = []
    for i in range(n_full_frames):
        frame = pcm[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        scores.append(detector.score_frame(frame))
    peak = max(scores)
    mean = sum(scores) / len(scores)
    return peak, mean, n_full_frames, peak >= threshold


def score_corpus(
    corpus_dir: Path,
    model_path: str,
    threshold: float = DEFAULT_THRESHOLD,
    *,
    detector=None,
) -> list[ScoredClip]:
    """Score every clip in the corpus.

    `detector` is keyword-only and overridable for testing; production
    callers pass `None` and get a real `WakeWordDetector` loaded from
    `model_path`. This is the only place this module imports
    `openwakeword` (indirectly via `jasper.wake`), so test code never
    touches it.

    Clips that fail to load (bad format, malformed WAV) are skipped
    with a `WARNING` and not included in the output — one bad clip
    doesn't take down the whole run, but the operator sees the
    skipped count via the log.
    """
    # Walk first; if no clips to score, return empty without paying
    # the cost of loading the wake-word model (and on dev machines,
    # without even triggering the openwakeword import). This makes
    # "I pointed at the wrong dir" fail-fast.
    clips = list(walk_corpus(corpus_dir))
    if not clips:
        return []

    if detector is None:
        from jasper.wake import WakeWordDetector
        detector = WakeWordDetector(model_path, threshold=threshold)

    scored: list[ScoredClip] = []
    for meta in clips:
        try:
            pcm = read_pcm(meta.path)
        except (ValueError, wave.Error) as e:
            logger.warning("skipping %s: %s", meta.path, e)
            continue
        peak, mean, n_frames, fired = score_clip(detector, pcm, threshold)
        scored.append(ScoredClip(
            meta=meta,
            peak_score=peak,
            mean_score=mean,
            frame_count=n_frames,
            fired=fired,
            duration_sec=n_frames * FRAME_SAMPLES / SAMPLE_RATE_HZ,
        ))
    return scored


# ---------------------------------------------------------------------------
# Output: CSV + summary
# ---------------------------------------------------------------------------


# CSV column order — chosen for spreadsheet readability (identifiers
# first, then scoring metrics, then derived booleans last).
CSV_FIELDS = (
    "path",
    "leg",
    "condition",
    "split",
    "peak_score",
    "mean_score",
    "frame_count",
    "fired",
    "duration_sec",
)


def write_csv(scored: Iterable[ScoredClip], output: Path) -> None:
    """Write per-clip scores to CSV. Atomic via tempfile + rename
    so a partial write (out-of-disk, Ctrl-C) doesn't leave a
    corrupted CSV that downstream tools try to parse."""
    tmp = output.with_suffix(output.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for clip in scored:
            w.writerow({
                "path": str(clip.meta.path),
                "leg": clip.meta.leg,
                "condition": clip.meta.condition,
                "split": clip.meta.split,
                "peak_score": f"{clip.peak_score:.4f}",
                "mean_score": f"{clip.mean_score:.4f}",
                "frame_count": clip.frame_count,
                "fired": int(clip.fired),
                "duration_sec": f"{clip.duration_sec:.3f}",
            })
    tmp.replace(output)


def format_summary(scored: list[ScoredClip], threshold: float) -> str:
    """Tabular per-(leg, condition, split) aggregate metrics.

    Recall here is the fraction of clips that scored above
    `threshold`. For the gold-corpus positives, recall is the
    headline metric. For hard negatives (which are inverted —
    you WANT them not to fire), the same column means "false-positive
    rate" — interpret per corpus context, not from the column header.
    """
    groups: dict[tuple[str, str, str], list[ScoredClip]] = defaultdict(list)
    for clip in scored:
        groups[(clip.meta.leg, clip.meta.condition, clip.meta.split)].append(clip)

    lines: list[str] = [
        "Wake-word scoring summary",
        "=" * 72,
        f"  Threshold: {threshold}",
        f"  Total clips: {len(scored)}",
        "",
        f"  {'leg':<6} {'condition':<10} {'split':<8} "
        f"{'count':>5} {'fired':>5} {'recall':>7} "
        f"{'mean_peak':>10} {'median_peak':>12}",
        f"  {'-' * 6} {'-' * 10} {'-' * 8} "
        f"{'-' * 5} {'-' * 5} {'-' * 7} "
        f"{'-' * 10} {'-' * 12}",
    ]
    for key in sorted(groups.keys()):
        leg, condition, split = key
        clips = groups[key]
        count = len(clips)
        fired = sum(1 for c in clips if c.fired)
        recall = fired / count if count else 0.0
        peaks = sorted(c.peak_score for c in clips)
        mean_peak = sum(peaks) / count if count else 0.0
        median_peak = peaks[count // 2] if count else 0.0
        lines.append(
            f"  {leg:<6} {condition:<10} {split:<8} "
            f"{count:>5} {fired:>5} {recall:>6.1%} "
            f"{mean_peak:>10.3f} {median_peak:>12.3f}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-score",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "corpus_dir",
        type=Path,
        help="Corpus root directory. Expected layout: "
             "<dir>/aec_{on,off,dtln}_{nomusic,ambient,music}/"
             "{train,eval}/*.wav "
             "(the output of scripts/_extract_wake_corpus.py or "
             "jasper-wake-enroll). Flat layout (no split subdirs) is "
             "also supported for backward compat.",
    )
    parser.add_argument(
        "model",
        type=str,
        help="openWakeWord-compatible ONNX model path or stock name "
             "(e.g. /var/lib/jasper/wake/jarvis_v2.onnx, or 'hey_jarvis').",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Wake threshold for the 'fired' classification "
             f"(default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("scores.csv"),
        help="Per-clip scores CSV path (default ./scores.csv).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging (DEBUG level).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.corpus_dir.is_dir():
        print(f"ERROR: {args.corpus_dir} is not a directory", file=sys.stderr)
        return 2

    print(f"Scoring {args.corpus_dir} with model={args.model} "
          f"threshold={args.threshold}...")
    scored = score_corpus(args.corpus_dir, args.model, args.threshold)

    if not scored:
        print(
            f"ERROR: no clips found in {args.corpus_dir} matching the "
            "expected aec_<leg>_<condition>/ layout. Check the corpus "
            "was built correctly + that the WAVs are in quadrant "
            "subdirs (not the root).",
            file=sys.stderr,
        )
        return 1

    write_csv(scored, args.output)
    print(format_summary(scored, args.threshold))
    print(f"\nPer-clip scores: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
