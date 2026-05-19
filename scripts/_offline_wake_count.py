#!/usr/bin/env python3
"""Run openWakeWord on a captured WAV and report per-utterance metadata.

For each peak in the wake-word score timeline (local maxima above a
loose candidate threshold), reports:
  - Time within the capture
  - openWakeWord confidence score
  - Broadband RMS of the audio in a 1.5 s window around the peak
  - Speech-band (300-3400 Hz) RMS
  - Whether the score crossed the production wake threshold

Why richer than a binary count: with 20 'Jarvis' utterances on the
track, the model might detect 14 of them and have 6 near-misses at
scores like 0.20-0.28. The near-misses tell us how close to working
the bridge / chip config is, and the per-utterance RMS tells us if
the audio level is consistent or if some utterances reached the mic
quieter than others.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np
from scipy import signal


SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280              # 80 ms @ 16 kHz, matches bridge frame
FRAME_PERIOD_SEC = CHUNK_SAMPLES / SAMPLE_RATE
WARMUP_SEC = 1.0                  # skip detections in first 1 s
RMS_WINDOW_SEC = 1.5              # window around peak for RMS
CANDIDATE_MIN_SCORE = 0.05        # any score >= this is a "candidate peak"
MAX_PEAKS_REPORTED = 40           # plenty for 20-utterance tracks


def _find_peaks(scores: np.ndarray, refractory_sec: float,
                min_score: float) -> list[int]:
    """Local maxima in `scores` above min_score, within ±refractory."""
    refr = int(refractory_sec / FRAME_PERIOD_SEC)
    peaks = []
    for i in range(len(scores)):
        if scores[i] < min_score:
            continue
        lo = max(0, i - refr)
        hi = min(len(scores), i + refr + 1)
        if scores[i] >= scores[lo:hi].max():
            peaks.append(i)
    # Dedupe: collapse adjacent equal-maxima within the refractory.
    deduped: list[int] = []
    for p in peaks:
        if deduped and p - deduped[-1] < refr:
            if scores[p] > scores[deduped[-1]]:
                deduped[-1] = p
            continue
        deduped.append(p)
    return deduped


def _rms(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav_path", type=Path)
    ap.add_argument("--model",
                    default=os.environ.get(
                        "JASPER_WAKE_MODEL",
                        "/var/lib/jasper/wake/jarvis_v2.onnx",
                    ))
    ap.add_argument("--threshold", type=float,
                    default=float(os.environ.get(
                        "JASPER_WAKE_THRESHOLD", "0.30",
                    )),
                    help="Production wake threshold (default 0.30)")
    ap.add_argument("--refractory-sec", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.wav_path.exists():
        print(f"ERROR: {args.wav_path} missing", file=sys.stderr)
        return 1
    if not Path(args.model).exists():
        print(f"ERROR: model {args.model} missing", file=sys.stderr)
        return 1

    with wave.open(str(args.wav_path)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if sr != SAMPLE_RATE or ch != 1 or sw != 2:
            print(f"ERROR: {args.wav_path} must be {SAMPLE_RATE}Hz mono S16; "
                  f"got {sr}Hz {ch}ch {sw*8}-bit", file=sys.stderr)
            return 1
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    duration_sec = len(audio) / SAMPLE_RATE

    # Speech band filter (4th-order Butter, 300-3400 Hz).
    sos_sb = signal.butter(4, [300, 3400], btype="band",
                           fs=SAMPLE_RATE, output="sos")
    audio_sb = signal.sosfilt(sos_sb, audio.astype(np.float32))

    # Run openWakeWord chunk by chunk, capturing scores + per-chunk RMS.
    from openwakeword.model import Model
    model = Model(wakeword_models=[args.model])
    model_key = Path(args.model).stem

    n_chunks = len(audio) // CHUNK_SAMPLES
    scores = np.zeros(n_chunks, dtype=np.float32)
    chunk_rms = np.zeros(n_chunks, dtype=np.float32)
    chunk_sb_rms = np.zeros(n_chunks, dtype=np.float32)
    for i in range(n_chunks):
        chunk = audio[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES]
        score = float(model.predict(chunk).get(model_key, 0.0))
        scores[i] = score
        chunk_rms[i] = _rms(chunk)
        chunk_sb_rms[i] = _rms(
            audio_sb[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES]
        )

    # Mask out warmup region from detection accounting (but keep
    # the timeline for context).
    warmup_chunks = int(WARMUP_SEC / FRAME_PERIOD_SEC)

    # Find peaks anywhere above CANDIDATE_MIN_SCORE
    peak_idxs = _find_peaks(scores, args.refractory_sec,
                            CANDIDATE_MIN_SCORE)
    # Drop warmup-region peaks
    peak_idxs = [i for i in peak_idxs if i >= warmup_chunks]
    # Cap report size
    peak_idxs = peak_idxs[:MAX_PEAKS_REPORTED]

    # Build per-peak metadata: RMS over a 1.5s window around the peak.
    half_samples = int(RMS_WINDOW_SEC / 2 * SAMPLE_RATE)
    peaks: list[dict] = []
    for i in peak_idxs:
        center = i * CHUNK_SAMPLES + CHUNK_SAMPLES // 2
        lo = max(0, center - half_samples)
        hi = min(len(audio), center + half_samples)
        seg = audio[lo:hi]
        seg_sb = audio_sb[lo:hi]
        peaks.append({
            "t_sec": round(i * FRAME_PERIOD_SEC, 2),
            "score": round(float(scores[i]), 3),
            "rms_window": round(_rms(seg), 1),
            "rms_window_speech_band": round(_rms(seg_sb), 1),
            "detected": bool(scores[i] >= args.threshold),
        })

    n_detected = sum(1 for p in peaks if p["detected"])
    n_candidates = len(peaks)
    max_score = float(scores.max()) if len(scores) > 0 else 0.0

    if args.json:
        print(json.dumps({
            "wav": str(args.wav_path),
            "duration_sec": round(duration_sec, 2),
            "threshold": args.threshold,
            "max_score": round(max_score, 3),
            "n_candidates": n_candidates,
            "n_detected": n_detected,
            "peaks": peaks,
        }, indent=2))
        return 0

    # Human-readable output
    print(f"")
    print(f"File:        {args.wav_path}")
    print(f"Duration:    {duration_sec:.2f}s")
    print(f"Threshold:   {args.threshold} (anything ≥ this counts as detection)")
    print(f"Max score:   {max_score:.3f}  (anywhere in capture)")
    print(f"")
    print(f"Peaks (local maxima above {CANDIDATE_MIN_SCORE}), top {MAX_PEAKS_REPORTED}:")
    print(f"")
    print(f"   #  |   t (s)  |  score  |  RMS  |  SB-RMS  |  detected")
    print(f"  ----+----------+---------+-------+----------+-----------")
    for n, p in enumerate(peaks, 1):
        mark = "✓ YES" if p["detected"] else "   no"
        print(f"  {n:3d} | {p['t_sec']:7.2f}  | {p['score']:.3f}   | "
              f"{p['rms_window']:5.0f} | {p['rms_window_speech_band']:7.0f}  |  {mark}")
    print(f"")
    print(f"  Candidates total:  {n_candidates}")
    print(f"  Detections:        {n_detected}")
    if n_candidates >= 1:
        # Rough wake-rate estimate (out of expected 20 if we ran the
        # standard track). When n_candidates >> 20, we may be picking
        # up music false-positives.
        print(f"  Wake rate (vs 20): {n_detected * 100 / 20:.0f}% "
              f"({n_detected}/20)")
        if n_candidates > 25:
            print(f"  ⚠  {n_candidates} candidates seen — likely some "
                  f"music transients above {CANDIDATE_MIN_SCORE}. Ignore "
                  f"them; the 'detected' column is what matters.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
