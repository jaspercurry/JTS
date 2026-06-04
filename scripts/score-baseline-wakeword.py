#!/usr/bin/env python3
"""Score a wake-word ONNX model offline against the 10-condition reference
baseline (`reference-conditions/`, see docs/HANDOFF-mic-quality-v2.md).

Runs the model across each condition's `aec-off.wav` (raw chip — pre-AEC3)
and `aec-on.wav` (post-AEC3, what production consumes today) in 1280-sample
/ 80 ms frames, the same way `jasper.wake.WakeWordDetector` does in
production. File naming matches scripts/wake-rate-test.sh's convention.
Records:

  - peak score across the file
  - peak score timestamp (where in the file it lived)
  - fire counts at three thresholds (0.5 = production default, 0.3 lenient, 0.1 floor)
  - RMS / peak amplitude for context

Output is a CSV in the reference-conditions dir (user-private). This
forms the "before" snapshot for the DTLN-aec experiment and any future
wake-word model swap — every future change A/B's against this same table.

Usage:
  python scripts/score-baseline-wakeword.py
  python scripts/score-baseline-wakeword.py --model /path/to/other.onnx
  python scripts/score-baseline-wakeword.py --model jarvis_v2.onnx --out custom.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = REPO_ROOT / "reference-conditions"

FRAME_SAMPLES = 1280  # 80 ms at 16 kHz — openWakeWord's stride
SAMPLE_RATE = 16000

# Mirrors the script's typical use against the 10-condition baseline.
DEFAULT_CONDITIONS = [
    "normal-quiet", "normal-music",
    "whisper-quiet", "whisper-music",
    "yell-quiet", "yell-music",
    "fast-quiet", "fast-music",
    "slow-quiet", "slow-music",
]
STREAM_FILES = [
    ("aec_off", "aec-off.wav"),  # raw chip 0 (pre-AEC3 = "AEC OFF" leg)
    ("aec_on", "aec-on.wav"),    # post-AEC3 output ("AEC ON" leg)
]
THRESHOLDS = [0.5, 0.3, 0.1]


def score_wav(model, score_key: str, path: Path) -> dict:
    """Stream a WAV through the openwakeword Model and return per-file metrics."""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        if sr != SAMPLE_RATE or ch != 1 or sw != 2:
            raise ValueError(
                f"{path.name}: expected 16 kHz mono int16, got sr={sr} ch={ch} sw={sw}"
            )
        raw = w.readframes(n)
    samples = np.frombuffer(raw, dtype=np.int16)
    duration_s = len(samples) / SAMPLE_RATE
    peak_amp = int(np.abs(samples).max()) if len(samples) else 0
    rms_amp = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) if len(samples) else 0.0

    # openwakeword's Model maintains a sliding 16-frame buffer of the
    # speech embedding internally, so streaming the file end-to-end is
    # equivalent to how production sees it. Reset state at the start
    # of each file so the previous file's residual doesn't bleed in.
    if hasattr(model, "reset"):
        model.reset()

    scores: list[float] = []
    n_complete_frames = len(samples) // FRAME_SAMPLES
    for i in range(n_complete_frames):
        chunk = samples[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        preds = model.predict(chunk)
        scores.append(float(preds.get(score_key, 0.0)))

    if not scores:
        return {
            "duration_s": duration_s,
            "n_frames": 0,
            "peak_score": 0.0,
            "peak_score_t": 0.0,
            "mean_score": 0.0,
            "median_score": 0.0,
            "fires_05": 0,
            "fires_03": 0,
            "fires_01": 0,
            "peak_amp": peak_amp,
            "peak_dbfs": -90.3,
            "rms_dbfs": -90.3,
        }
    arr = np.asarray(scores)
    peak_idx = int(arr.argmax())
    peak_t = peak_idx * FRAME_SAMPLES / SAMPLE_RATE
    return {
        "duration_s": duration_s,
        "n_frames": len(scores),
        "peak_score": float(arr.max()),
        "peak_score_t": peak_t,
        "mean_score": float(arr.mean()),
        "median_score": float(np.median(arr)),
        "fires_05": int((arr >= 0.5).sum()),
        "fires_03": int((arr >= 0.3).sum()),
        "fires_01": int((arr >= 0.1).sum()),
        "peak_amp": peak_amp,
        "peak_dbfs": 20.0 * math.log10(max(peak_amp, 1) / 32768.0),
        "rms_dbfs": 20.0 * math.log10(max(rms_amp, 1.0) / 32768.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--model",
        default="/tmp/jts-wake-models/jarvis_v2.onnx",
        help="Path to wake-word ONNX (or stock openwakeword name like hey_jarvis)",
    )
    ap.add_argument(
        "--baseline-dir",
        default=str(BASELINE_DIR),
        help="Reference-conditions directory (default: ./reference-conditions)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Output CSV path. Defaults to <baseline-dir>/<model-basename>-baseline-scores.csv",
    )
    args = ap.parse_args()

    baseline_dir = Path(args.baseline_dir)
    if not baseline_dir.is_dir():
        print(f"baseline dir not found: {baseline_dir}", file=sys.stderr)
        return 1

    model_path = args.model
    is_path = "/" in model_path or model_path.endswith(".onnx") or model_path.endswith(".tflite")
    if is_path and not Path(model_path).exists():
        print(f"model file not found: {model_path}", file=sys.stderr)
        return 1
    score_key = (
        Path(model_path).stem
        if is_path
        else model_path
    )

    if args.out is None:
        out_path = baseline_dir / f"{score_key}-baseline-scores.csv"
    else:
        out_path = Path(args.out)

    # Lazy import so the script can be syntax-checked without openwakeword
    from openwakeword.model import Model

    print(f"loading model: {model_path}  (score key: '{score_key}')")
    model = Model(
        wakeword_models=[model_path],
        inference_framework="onnx",
    )

    rows = []
    print(f"\nscoring across {baseline_dir}/")
    for cond in DEFAULT_CONDITIONS:
        d = baseline_dir / cond
        if not d.is_dir():
            print(f"  [skip] {cond}: dir missing")
            continue
        for stream_key, fname in STREAM_FILES:
            wav = d / fname
            if not wav.is_file():
                print(f"  [skip] {cond}/{fname}: not found")
                continue
            try:
                m = score_wav(model, score_key, wav)
            except Exception as exc:
                print(f"  [err]  {cond}/{fname}: {exc}", file=sys.stderr)
                continue
            row = {
                "condition": cond,
                "stream": stream_key,
                **m,
            }
            rows.append(row)
            print(
                f"  {cond:14s} {stream_key:10s} "
                f"peak={m['peak_score']:.3f}@{m['peak_score_t']:5.1f}s  "
                f"fires(.5/.3/.1)={m['fires_05']:3d}/{m['fires_03']:3d}/{m['fires_01']:3d}  "
                f"amp_peak={m['peak_dbfs']:+5.1f}dBFS"
            )

    if not rows:
        print("no rows produced; aborting", file=sys.stderr)
        return 1

    # Persist
    fieldnames = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {out_path} ({len(rows)} rows)")

    # Summary — paired peak-score per condition
    print("\n" + "=" * 78)
    print(f"{'condition':<14s} {'aec-off':>10s} {'aec-on':>10s} {'delta':>8s}  fires@.5")
    print("-" * 78)
    by_cond: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_cond.setdefault(row["condition"], {})[row["stream"]] = row
    for cond in DEFAULT_CONDITIONS:
        if cond not in by_cond:
            continue
        c = by_cond[cond]
        off = c.get("aec_off", {})
        on = c.get("aec_on", {})
        op = off.get("peak_score", 0.0)
        np_ = on.get("peak_score", 0.0)
        delta = np_ - op
        of = off.get("fires_05", 0)
        nf = on.get("fires_05", 0)
        marker = "  ✗" if (of > 0 and nf == 0) else ("  ⚠" if (op >= 0.5 and np_ < 0.5) else "")
        print(
            f"{cond:<14s} {op:10.3f} {np_:10.3f} {delta:+8.3f}  "
            f"off={of:3d} on={nf:3d}{marker}"
        )
    print("=" * 78)
    print("legend: ✗ = wake would fire on raw mic but silent on AEC ON output")
    print("        ⚠ = peak crossed threshold on raw but AEC ON stayed below")
    return 0


if __name__ == "__main__":
    sys.exit(main())
