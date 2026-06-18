#!/usr/bin/env python3
"""Offline harness for the live-VAD speech-arming gate.

Replays every captured wake-event WAV through silero with the same
COLD-reset semantics production uses (``vad.reset()`` at turn start),
then computes:

  - When the *current* live-VAD gate would arm
    (3 consecutive frames at silero >= 0.15)
  - When a *candidate* peak-confidence gate would arm
    (same, but also requires max silero in the run >= --peak-min)
  - Peak silero in the wake-tail window (0–400 ms post-wake)
  - Peak silero in the user-speech window (400–2000 ms post-wake)

Use this to:

  1. Identify which captured events were silent (wake-tail false-arm
     cases — high tail peak, low user-speech peak).
  2. Sweep --peak-min to find a threshold that separates wake-tail
     residual from real speech across the corpus.
  3. Verify a proposed gate change before deploying it.

The frame-alignment offset between the live mic stream and the
captured WAV is a few tens of ms of jitter that we can't reproduce.
This script sweeps offsets 0..200ms in 40ms steps and reports the
MOST PERMISSIVE outcome (earliest arm) so you see worst-case
behavior — that's what matters for the gate-rejection question.

Usage:
  scripts/probe-wake-gate.py [--peak-min 0.40] [--threshold 0.15]
                             [--sustained-ms 200] [--csv out.csv]
                             [--event EVENT_ID] [--corpus-dir DIR]
"""
from __future__ import annotations
import argparse
import csv
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import scipy.io.wavfile as wav

# Make the jasper package importable (we use the SAME SpeechVAD
# wrapper production uses, not a re-implementation).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jasper.vad import SpeechVAD  # noqa: E402

FRAME_MS = 80
FRAME_SAMPLES = 16000 * FRAME_MS // 1000  # 1280
CAPTURE_PRE_SEC = 4.0  # match jasper.wake_events.CAPTURE_PRE_SEC
# Sweep these drain-done offsets to cover UDP/asyncio jitter in
# real-time alignment. Live VAD's first frame can land anywhere in
# this range depending on how queueing aligns with frame boundaries.
DRAIN_OFFSETS_MS = (0, 40, 80, 120, 160, 200)
# Windows for peak-silero analysis (ms post-wake).
TAIL_WINDOW = (0, 400)
SPEECH_WINDOW = (400, 2000)


def _replay(
    data: np.ndarray, start_sample: int,
    *, threshold: float, sustained_ms: int, peak_min: float,
    max_window_ms: int = 2000,
) -> dict:
    """Feed frames from start_sample to a fresh silero VAD with the
    production gate logic. Returns a dict describing the arming
    behavior: when (if ever) the current gate fires, when (if ever)
    the peak-min-augmented gate fires, and the max silero in the run."""
    vad = SpeechVAD()  # cold — matches production reset
    wake_sample = int(CAPTURE_PRE_SEC * 16000)

    consec = 0
    run_start_ms = None
    run_max = 0.0
    current_arm_at = None      # current gate (duration only)
    peakgate_arm_at = None     # current + peak-min requirement

    for i in range(start_sample, len(data) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        rel_ms = (i - wake_sample) / 16
        if rel_ms > max_window_ms:
            break
        frame = data[i : i + FRAME_SAMPLES]
        score = float(vad.predict(frame))
        is_speech = score >= threshold

        if is_speech:
            if consec == 0:
                run_start_ms = rel_ms
                run_max = score
            else:
                run_max = max(run_max, score)
            consec += 1
            sustained = (rel_ms - run_start_ms) + FRAME_MS
            if (current_arm_at is None
                    and consec >= 3 and sustained >= sustained_ms):
                current_arm_at = rel_ms + FRAME_MS
            if (peakgate_arm_at is None
                    and consec >= 3 and sustained >= sustained_ms
                    and run_max >= peak_min):
                peakgate_arm_at = rel_ms + FRAME_MS
        else:
            consec = 0
            run_start_ms = None
            run_max = 0.0

    return {
        "current_arm_at_ms": current_arm_at,
        "peakgate_arm_at_ms": peakgate_arm_at,
    }


def _peak_in_window(
    data: np.ndarray, t_start_ms: int, t_end_ms: int,
) -> float:
    """Peak silero score in [t_start_ms, t_end_ms] post-wake, with
    cold-reset silero. Independent VAD instance so this analysis
    isn't contaminated by the replay's run-arming sweep."""
    vad = SpeechVAD()
    wake_sample = int(CAPTURE_PRE_SEC * 16000)
    start = wake_sample + int(t_start_ms * 16)
    end = wake_sample + int(t_end_ms * 16)
    peak = 0.0
    for i in range(start, min(end, len(data)) - FRAME_SAMPLES + 1,
                   FRAME_SAMPLES):
        frame = data[i : i + FRAME_SAMPLES]
        peak = max(peak, float(vad.predict(frame)))
    return peak


def analyze_event(
    wav_path: Path, *,
    threshold: float, sustained_ms: int, peak_min: float,
) -> dict:
    """Returns a row of metrics for one event."""
    rate, data = wav.read(wav_path)
    if data.dtype != np.int16:
        data = data.astype(np.int16)
    wake_sample = int(CAPTURE_PRE_SEC * rate)

    # Sweep drain-done offsets; report the WORST-case (earliest arm)
    # for the current gate, and the BEST-case (earliest non-arm or
    # latest arm) for the peakgate.
    current_arms = []
    peakgate_arms = []
    for off_ms in DRAIN_OFFSETS_MS:
        start = wake_sample + int(off_ms * 16)
        r = _replay(data, start, threshold=threshold,
                    sustained_ms=sustained_ms, peak_min=peak_min)
        current_arms.append(r["current_arm_at_ms"])
        peakgate_arms.append(r["peakgate_arm_at_ms"])
    # Worst case = earliest arm time (most permissive). None = never armed.
    cur_min = min((t for t in current_arms if t is not None), default=None)
    peak_min_t = min((t for t in peakgate_arms if t is not None), default=None)

    return {
        "event_id": wav_path.stem.replace(".aec-on", ""),
        "tail_peak_silero": round(_peak_in_window(data, *TAIL_WINDOW), 3),
        "speech_peak_silero": round(_peak_in_window(data, *SPEECH_WINDOW), 3),
        "current_arm_at_ms": cur_min,
        "peakgate_arm_at_ms": peak_min_t,
        "current_armed_in_tail": (
            cur_min is not None and cur_min <= TAIL_WINDOW[1] + FRAME_MS
        ),
        "peakgate_armed_in_tail": (
            peak_min_t is not None and peak_min_t <= TAIL_WINDOW[1] + FRAME_MS
        ),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--threshold", type=float, default=0.15,
                   help="silero speech threshold (current: 0.15)")
    p.add_argument("--sustained-ms", type=int, default=200,
                   help="min sustained-speech duration to arm (current: 200)")
    p.add_argument("--peak-min", type=float, default=0.40,
                   help="candidate: min PEAK silero in arming run")
    p.add_argument("--corpus-dir", type=str,
                   default=str(ROOT / "wake-events" / "latest"),
                   help="directory of captured wake events")
    p.add_argument("--event", type=str, default=None,
                   help="analyze just this event_id (substring match ok)")
    p.add_argument("--csv", type=str, default=None,
                   help="write full per-event CSV to this path")
    args = p.parse_args()

    corpus = Path(args.corpus_dir)
    wavs = sorted(corpus.glob("*.aec-on.wav"))
    if args.event:
        wavs = [w for w in wavs if args.event in w.name]
    if not wavs:
        print(f"no WAVs found in {corpus}", file=sys.stderr)
        return 1

    # Load SQLite to enrich each row with the live log's actual arming
    # behavior, for cross-validation.
    db_path = corpus / "wake-events.sqlite3"
    live_log = {}
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT event_id, ts_utc, ts_speech_detected, outcome FROM wake_events"
        ):
            d = dict(row)
            def _p(s):
                if not s: return None
                try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
                except Exception: return None  # noqa: BLE001
            t_wake = _p(d["ts_utc"])
            t_speech = _p(d["ts_speech_detected"])
            live_log[d["event_id"]] = {
                "live_arm_at_ms": (
                    (t_speech - t_wake).total_seconds() * 1000
                    if (t_speech and t_wake) else None
                ),
                "outcome": d["outcome"],
            }

    rows = []
    print(f"Analyzing {len(wavs)} events. Params: "
          f"threshold={args.threshold}, sustained_ms={args.sustained_ms}, "
          f"peak_min={args.peak_min}")
    print(f"\n{'event_id':<28} {'tail':>6} {'speech':>7} "
          f"{'cur_arm':>9} {'peak_arm':>9} {'live_arm':>9} "
          f"{'cur?tail':>10} {'peak?tail':>10}")
    print("-" * 105)

    for w in wavs:
        r = analyze_event(w, threshold=args.threshold,
                          sustained_ms=args.sustained_ms,
                          peak_min=args.peak_min)
        lg = live_log.get(r["event_id"], {})
        r["live_arm_at_ms"] = lg.get("live_arm_at_ms")
        r["outcome"] = lg.get("outcome")
        rows.append(r)
        ca = f"{r['current_arm_at_ms']:.0f}ms" if r['current_arm_at_ms'] else "—"
        pa = f"{r['peakgate_arm_at_ms']:.0f}ms" if r['peakgate_arm_at_ms'] else "—"
        la = f"{r['live_arm_at_ms']:.0f}ms" if r.get('live_arm_at_ms') else "—"
        cur_tail = "YES" if r["current_armed_in_tail"] else "."
        peak_tail = "YES" if r["peakgate_armed_in_tail"] else "."
        print(f"{r['event_id']:<28} {r['tail_peak_silero']:>6.3f} "
              f"{r['speech_peak_silero']:>7.3f} {ca:>9} {pa:>9} {la:>9} "
              f"{cur_tail:>10} {peak_tail:>10}")

    # Aggregate summary
    n = len(rows)
    n_cur_tail = sum(1 for r in rows if r["current_armed_in_tail"])
    n_peak_tail = sum(1 for r in rows if r["peakgate_armed_in_tail"])
    n_cur_any = sum(1 for r in rows if r["current_arm_at_ms"] is not None)
    n_peak_any = sum(1 for r in rows if r["peakgate_arm_at_ms"] is not None)
    print(f"\nSummary across {n} events:")
    print(f"  Current gate arms in wake-tail window:  {n_cur_tail}/{n} "
          f"({100*n_cur_tail/n:.0f}%) — these are likely false arms")
    print(f"  Peak-min gate arms in wake-tail window: {n_peak_tail}/{n} "
          f"({100*n_peak_tail/n:.0f}%)")
    print(f"  Current gate arms at all (within 2s):   {n_cur_any}/{n}")
    print(f"  Peak-min gate arms at all (within 2s):  {n_peak_any}/{n}")
    delta = n_cur_any - n_peak_any
    print(f"  Events the peak-min gate would REJECT: {delta} "
          f"(current arms but peak-min doesn't)")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows: w.writerow(r)
        print(f"\nFull per-event CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
