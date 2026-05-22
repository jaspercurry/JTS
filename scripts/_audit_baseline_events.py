#!/usr/bin/env python3
"""Event-level audit of the 10-condition reference baseline across the
four legs (raw mic / AEC3 / DTLN-128 / DTLN-256).

Reports DISTINCT WAKE EVENTS per file (peak detection in the score
timeline with a 0.7 s refractory matching production WakeLoop), not
raw frame counts. Each event lists its timestamp + peak score so the
user can play the WAV and verify "yes, I said 'Jarvis' at that
time." Files with no events report the highest sub-threshold peak
so we know whether the model came close.

Usage:
  python scripts/_audit_baseline_events.py                # all 10 conditions
  python scripts/_audit_baseline_events.py whisper-music  # one condition

Threshold = 0.5 (production default).
Refractory = 0.7 s (production wake-loop dedup window).
Frame = 80 ms (1280 samples @ 16 kHz, matches production).
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
MODEL_PATH = "/tmp/jts-wake-models/jarvis_v2.onnx"

FRAME_SAMPLES = 1280
FRAME_SEC = FRAME_SAMPLES / 16000  # 80 ms
THRESHOLD = 0.5
REFRACTORY_SEC = 0.7  # production WakeLoop dedup window

CONDITIONS_DEFAULT = [
    "normal-quiet", "normal-music",
    "whisper-quiet", "whisper-music",
    "yell-quiet", "yell-music",
    "fast-quiet", "fast-music",
    "slow-quiet", "slow-music",
]

LEGS = [
    ("raw_mic",  "aec-off.wav",       "raw chip mic (no AEC)"),
    ("aec3",     "aec-on.wav",        "AEC3 (production today)"),
    ("dtln_128", "aec-dtln-128.wav",  "DTLN-aec, 128-unit"),
    ("dtln_256", "aec-dtln-256.wav",  "DTLN-aec, 256-unit"),
]


def find_events(scores: np.ndarray, threshold: float, refractory_sec: float) -> list[tuple[int, float]]:
    """Locate distinct events where score crossed `threshold`. Adjacent
    above-threshold frames are collapsed into one event (the peak),
    and a `refractory_sec` window after each event suppresses further
    detections — matches production WakeLoop's dedup behaviour.

    Returns: list of (frame_idx_of_peak, peak_score)."""
    refractory_frames = int(refractory_sec / FRAME_SEC)
    events: list[tuple[int, float]] = []
    i = 0
    while i < len(scores):
        if scores[i] >= threshold:
            end = min(i + refractory_frames, len(scores))
            peak_offset = int(np.argmax(scores[i:end]))
            peak_idx = i + peak_offset
            peak_score = float(scores[peak_idx])
            events.append((peak_idx, peak_score))
            i = peak_idx + refractory_frames
        else:
            i += 1
    return events


def score_file(model, score_key: str, path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16 kHz mono int16")
        n = w.getnframes()
        data = w.readframes(n)
    s = np.frombuffer(data, dtype=np.int16)
    if hasattr(model, "reset"):
        model.reset()
    scores = []
    n_frames = len(s) // FRAME_SAMPLES
    for i in range(n_frames):
        chunk = s[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        scores.append(float(model.predict(chunk).get(score_key, 0.0)))
    return np.asarray(scores)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("conditions", nargs="*", default=CONDITIONS_DEFAULT,
                    help="condition names (default: all 10)")
    ap.add_argument("--csv", type=Path,
                    default=BASELINE_DIR / "jarvis_v2-event-audit.csv",
                    help="output CSV path")
    args = ap.parse_args()

    from openwakeword.model import Model
    print(f"loading model: {MODEL_PATH}")
    model = Model(wakeword_models=[MODEL_PATH], inference_framework="onnx")
    score_key = "jarvis_v2"

    csv_rows = []

    for cond in args.conditions:
        cond_dir = BASELINE_DIR / cond
        if not cond_dir.is_dir():
            print(f"  [skip] {cond}: no dir")
            continue

        print()
        print(f"=" * 78)
        print(f"  {cond}")
        print(f"=" * 78)
        # First pass: collect per-leg results to display them paired up.
        leg_rows = []
        for leg_key, leg_file, leg_desc in LEGS:
            path = cond_dir / leg_file
            if not path.is_file():
                leg_rows.append((leg_key, leg_desc, None, None, None))
                continue
            scores = score_file(model, score_key, path)
            events = find_events(scores, THRESHOLD, REFRACTORY_SEC)
            if len(scores):
                peak_overall = float(scores.max())
                peak_at_t = float(scores.argmax()) * FRAME_SEC
            else:
                peak_overall = 0.0
                peak_at_t = 0.0
            leg_rows.append((leg_key, leg_desc, events, peak_overall, peak_at_t))

            csv_rows.append({
                "condition": cond,
                "leg": leg_key,
                "file": leg_file,
                "n_events_at_0.5": len(events),
                "peak_score_overall": round(peak_overall, 4),
                "peak_at_t_sec": round(peak_at_t, 2),
                "events": ";".join(f"{e[0]*FRAME_SEC:.2f}s@{e[1]:.3f}" for e in events),
            })

        # Display: aligned by leg
        for leg_key, leg_desc, events, peak_overall, peak_at_t in leg_rows:
            label = f"  {leg_key:<9s} ({leg_desc})"
            if events is None:
                print(f"{label:<55s} (file missing)")
                continue
            n = len(events)
            if n > 0:
                tstamps = ", ".join(f"{e[0]*FRAME_SEC:5.1f}s" for e in events)
                avg_score = sum(s for _, s in events) / n
                print(f"{label:<55s} N={n:<2d}  avg_score={avg_score:.3f}")
                print(f"{'  events at:':<55s} {tstamps}")
            else:
                # No fires — but how close did we get?
                if peak_overall >= 0.3:
                    miss_label = "near-miss"
                elif peak_overall >= 0.1:
                    miss_label = "weak signal"
                else:
                    miss_label = "silent miss"
                print(f"{label:<55s} N=0   peak={peak_overall:.3f} @ {peak_at_t:.1f}s ({miss_label})")

    # Write CSV
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    if csv_rows:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)
        print(f"\nWrote {args.csv} ({len(csv_rows)} rows)")

    # Cross-condition summary at the end
    print()
    print("=" * 78)
    print("  Summary — events per condition × leg")
    print("=" * 78)
    print(f"  {'condition':<14s} | {'raw':>5s} {'AEC3':>5s} {'D128':>5s} {'D256':>5s}  notes")
    print(f"  {'-'*14} | {'-'*5} {'-'*5} {'-'*5} {'-'*5}  -----")
    cond_events = {}
    for row in csv_rows:
        cond_events.setdefault(row["condition"], {})[row["leg"]] = row["n_events_at_0.5"]
    for cond in args.conditions:
        if cond not in cond_events:
            continue
        ce = cond_events[cond]
        raw = ce.get("raw_mic", "-")
        aec3 = ce.get("aec3", "-")
        d128 = ce.get("dtln_128", "-")
        d256 = ce.get("dtln_256", "-")
        # Quick verdict
        notes = []
        if isinstance(aec3, int) and isinstance(raw, int) and aec3 == 0 and raw > 0:
            notes.append("AEC3 silent miss")
        if isinstance(d128, int) and isinstance(aec3, int) and aec3 == 0 and d128 > 0:
            notes.append("D128 rescues")
        if isinstance(d256, int) and isinstance(aec3, int) and aec3 == 0 and d256 > 0:
            notes.append("D256 rescues")
        print(f"  {cond:<14s} | {str(raw):>5s} {str(aec3):>5s} {str(d128):>5s} {str(d256):>5s}  {', '.join(notes)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
