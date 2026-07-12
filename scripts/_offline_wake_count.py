#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Run openWakeWord on a captured WAV and report per-utterance metadata.

Two modes:
  --template  (recommended): cross-correlates the canonical 'Jarvis'
              WAV against the recording to locate ALL 20 utterances,
              including silent misses (score=0). For each utterance,
              reports its peak wake score, broadband + speech-band
              RMS, and whether it crossed the production threshold.
              This is what tells us "we caught 14, the other 6 were
              missed because…" with quantified causes.

  default     (no template): scans the wake-score timeline for peaks
              above 0.05 (local maxima). Useful when we don't have
              the template — won't catch utterances that produced
              zero wake-model response, but also won't lie about
              false positives from music.
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

try:
    from _wake_audio_metrics import rms_amplitude as _rms
except ModuleNotFoundError as exc:
    if exc.name != "_wake_audio_metrics":
        raise
    from scripts._wake_audio_metrics import rms_amplitude as _rms


SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280               # 80 ms @ 16 kHz, matches bridge frame
FRAME_PERIOD_SEC = CHUNK_SAMPLES / SAMPLE_RATE
WARMUP_SEC = 1.0                   # skip detections in first 1 s
RMS_WINDOW_SEC = 1.5               # window around peak for RMS
SCORE_WINDOW_AHEAD_SEC = 2.0       # model needs context after utt start
CANDIDATE_MIN_SCORE = 0.05
MAX_PEAKS_REPORTED = 40


def _load_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Load a WAV file as int16 mono. Returns (samples, sample_rate)."""
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        if sw != 2:
            raise SystemExit(f"{path}: expected 16-bit, got {sw*8}")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch == 2:
        data = data[::2]  # take left channel only
    return data, sr


def _resample_to_16k(audio: np.ndarray, in_rate: int) -> np.ndarray:
    """Resample to 16 kHz. resample_poly handles common ratios cleanly."""
    if in_rate == SAMPLE_RATE:
        return audio
    # Find rational approximation
    from math import gcd
    g = gcd(in_rate, SAMPLE_RATE)
    up = SAMPLE_RATE // g
    down = in_rate // g
    return signal.resample_poly(audio.astype(np.float32), up=up, down=down)


def find_utterances_xcorr(audio: np.ndarray, template: np.ndarray,
                          expected_count: int, min_gap_sec: float
                          ) -> list[tuple[int, float]]:
    """Find utterance start times via normalized cross-correlation.

    Returns up to `expected_count` peaks (sorted by time), each as
    (sample_index, normalized_xcorr_strength). Robust to loud music
    passages because we normalize by sliding audio RMS.
    """
    template = template.astype(np.float32)
    audio_f = audio.astype(np.float32)

    template_norm = template / (np.linalg.norm(template) + 1e-9)
    xc = signal.correlate(audio_f, template_norm, mode="valid", method="fft")

    # Normalize by sliding audio RMS so loud music doesn't dominate.
    tlen = len(template)
    audio_sq = audio_f ** 2
    cumsum = np.concatenate(([0.0], np.cumsum(audio_sq)))
    window_energy = cumsum[tlen:] - cumsum[:-tlen]
    window_rms = np.sqrt(window_energy / tlen)
    window_rms = np.where(window_rms > 1e-9, window_rms, 1e-9)
    xc_norm = xc / window_rms

    min_gap = int(min_gap_sec * SAMPLE_RATE)
    peaks: list[tuple[int, float]] = []
    xc_search = xc_norm.copy()
    for _ in range(expected_count):
        idx = int(np.argmax(xc_search))
        if not np.isfinite(xc_search[idx]) or xc_search[idx] <= 0:
            break
        peaks.append((idx, float(xc_norm[idx])))
        lo, hi = max(0, idx - min_gap), min(len(xc_search), idx + min_gap)
        xc_search[lo:hi] = -np.inf

    peaks.sort(key=lambda p: p[0])
    return peaks


def find_peaks_in_score(scores: np.ndarray, refractory_sec: float,
                        min_score: float) -> list[int]:
    refr = int(refractory_sec / FRAME_PERIOD_SEC)
    peaks: list[int] = []
    for i in range(len(scores)):
        if scores[i] < min_score:
            continue
        lo, hi = max(0, i - refr), min(len(scores), i + refr + 1)
        if scores[i] >= scores[lo:hi].max():
            peaks.append(i)
    deduped: list[int] = []
    for p in peaks:
        if deduped and p - deduped[-1] < refr:
            if scores[p] > scores[deduped[-1]]:
                deduped[-1] = p
            continue
        deduped.append(p)
    return deduped


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("wav_path", type=Path)
    ap.add_argument("--model",
                    default=os.environ.get(
                        "JASPER_WAKE_MODEL",
                        "/var/lib/jasper/wake/jarvis_v2.onnx",
                    ))
    ap.add_argument("--threshold", type=float,
                    default=float(os.environ.get(
                        "JASPER_WAKE_THRESHOLD", "0.30",
                    )))
    ap.add_argument("--refractory-sec", type=float, default=1.0)
    ap.add_argument("--template", type=Path,
                    help="Canonical 'Jarvis' WAV — enables silent-miss "
                         "tracking via cross-correlation")
    ap.add_argument("--reps", type=int, default=20,
                    help="Expected utterance count (default 20)")
    ap.add_argument("--min-utt-gap-sec", type=float, default=3.5,
                    help="Min seconds between utterances (default 3.5)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.wav_path.exists():
        print(f"ERROR: {args.wav_path} missing", file=sys.stderr)
        return 1
    if not Path(args.model).exists():
        print(f"ERROR: model {args.model} missing", file=sys.stderr)
        return 1

    audio, sr = _load_wav_mono(args.wav_path)
    if sr != SAMPLE_RATE:
        print(f"ERROR: capture must be {SAMPLE_RATE}Hz, got {sr}", file=sys.stderr)
        return 1
    duration_sec = len(audio) / SAMPLE_RATE

    # Speech-band filter (300-3400 Hz, 4th-order Butter SOS).
    sos_sb = signal.butter(4, [300, 3400], btype="band",
                           fs=SAMPLE_RATE, output="sos")
    audio_sb = signal.sosfilt(sos_sb, audio.astype(np.float32))

    # Run openWakeWord on the whole file.
    from openwakeword.model import Model
    model = Model(wakeword_models=[args.model])
    model_key = Path(args.model).stem

    n_chunks = len(audio) // CHUNK_SAMPLES
    scores = np.zeros(n_chunks, dtype=np.float32)
    for i in range(n_chunks):
        chunk = audio[i * CHUNK_SAMPLES:(i + 1) * CHUNK_SAMPLES]
        scores[i] = float(model.predict(chunk).get(model_key, 0.0))
    max_score = float(scores.max()) if n_chunks else 0.0

    score_window_chunks = int(SCORE_WINDOW_AHEAD_SEC / FRAME_PERIOD_SEC)
    half_rms_samples = int(RMS_WINDOW_SEC / 2 * SAMPLE_RATE)

    def utterance_metadata(sample_idx: int) -> dict:
        """Per-utterance row: where the utterance starts in samples."""
        center_sample = sample_idx + int(0.7 * SAMPLE_RATE)  # ~mid Jarvis
        lo_rms = max(0, center_sample - half_rms_samples)
        hi_rms = min(len(audio), center_sample + half_rms_samples)
        first_chunk = sample_idx // CHUNK_SAMPLES
        last_chunk = min(n_chunks, first_chunk + score_window_chunks)
        if last_chunk > first_chunk:
            sub = scores[first_chunk:last_chunk]
            peak_local = int(np.argmax(sub))
            peak_score = float(sub[peak_local])
            peak_t = (first_chunk + peak_local) * FRAME_PERIOD_SEC
        else:
            peak_score = 0.0
            peak_t = sample_idx / SAMPLE_RATE
        return {
            "t_sec": round(sample_idx / SAMPLE_RATE, 2),
            "peak_score": round(peak_score, 3),
            "peak_at_t_sec": round(peak_t, 2),
            "rms": round(_rms(audio[lo_rms:hi_rms]), 1),
            "rms_speech_band": round(_rms(audio_sb[lo_rms:hi_rms]), 1),
            "detected": bool(peak_score >= args.threshold),
        }

    output: dict = {
        "wav": str(args.wav_path),
        "duration_sec": round(duration_sec, 2),
        "threshold": args.threshold,
        "max_score": round(max_score, 3),
    }

    if args.template and args.template.exists():
        # Template mode: find ALL `--reps` utterances via xcorr,
        # then report per-utterance metadata regardless of whether
        # the wake model heard them.
        template_raw, t_sr = _load_wav_mono(args.template)
        template_16k = _resample_to_16k(template_raw.astype(np.float32), t_sr)
        peaks_xc = find_utterances_xcorr(
            audio, template_16k.astype(np.float32),
            expected_count=args.reps,
            min_gap_sec=args.min_utt_gap_sec,
        )
        utts = []
        for n, (sample_idx, xc_strength) in enumerate(peaks_xc, 1):
            row = utterance_metadata(sample_idx)
            row["n"] = n
            row["xcorr"] = round(xc_strength, 1)
            utts.append(row)
        n_detected = sum(1 for u in utts if u["detected"])
        # Classify each utterance.
        for u in utts:
            if u["detected"]:
                u["category"] = "detected"
            elif u["peak_score"] >= 0.10:
                u["category"] = "near_miss"      # 0.10 ≤ score < threshold
            elif u["peak_score"] >= 0.02:
                u["category"] = "weak_signal"    # model barely saw it
            else:
                u["category"] = "silent_miss"    # model saw nothing
        output["utterances_found_via_template"] = utts
        output["template_path"] = str(args.template)
        output["n_utterances_found"] = len(utts)
        output["n_detected"] = n_detected
        output["wake_rate_pct"] = round(n_detected * 100 / max(1, args.reps), 1)

        if not args.json:
            print("")
            print(f"File:        {args.wav_path}")
            print(f"Duration:    {duration_sec:.2f}s")
            print(f"Template:    {args.template}")
            print(f"Threshold:   {args.threshold}  (anything ≥ this counts as detection)")
            print(f"Max score:   {max_score:.3f}")
            print("")
            print(f"Located {len(utts)}/{args.reps} 'Jarvis' utterances via cross-correlation.")
            print("")
            print("   #  |   t (s)  | peak score | wake-RMS |  SB-RMS  |  xcorr | status")
            print("  ----+----------+------------+----------+----------+--------+----------------")
            for u in utts:
                if u["category"] == "detected":
                    status = "✓ DETECTED"
                elif u["category"] == "near_miss":
                    status = "  near miss   (just below threshold)"
                elif u["category"] == "weak_signal":
                    status = "  weak signal (model barely saw it)"
                else:
                    status = "  silent      (model saw nothing)"
                print(f"  {u['n']:3d} | {u['t_sec']:7.2f}  |   {u['peak_score']:.3f}    | "
                      f"{u['rms']:7.0f}  | {u['rms_speech_band']:7.0f}  | "
                      f"{u['xcorr']:5.0f}  |  {status}")
            print("")
            print(f"  Total detected:       {n_detected:2d} / {args.reps}  ({n_detected*100/args.reps:.0f}%)")
            n_near = sum(1 for u in utts if u['category'] == 'near_miss')
            n_weak = sum(1 for u in utts if u['category'] == 'weak_signal')
            n_silent = sum(1 for u in utts if u['category'] == 'silent_miss')
            print(f"  Near misses (≥0.10): {n_near:2d}    score below threshold but model saw it")
            print(f"  Weak signal (≥0.02): {n_weak:2d}    barely registered")
            print(f"  Silent miss:         {n_silent:2d}    model saw nothing")

    # Peak-based output (works without template, finds music false positives).
    peak_idxs = find_peaks_in_score(scores, args.refractory_sec,
                                    CANDIDATE_MIN_SCORE)
    warmup_chunks = int(WARMUP_SEC / FRAME_PERIOD_SEC)
    peak_idxs = [i for i in peak_idxs if i >= warmup_chunks][:MAX_PEAKS_REPORTED]
    peaks_above = [i for i in peak_idxs if scores[i] >= args.threshold]
    output["n_peaks_in_score"] = len(peak_idxs)
    output["n_peaks_above_threshold"] = len(peaks_above)
    output["peaks_above_threshold"] = [
        {"t_sec": round(i * FRAME_PERIOD_SEC, 2),
         "score": round(float(scores[i]), 3)}
        for i in peaks_above
    ]

    if args.json:
        print(json.dumps(output, indent=2))
        return 0

    # If we had a template, the above already printed everything.
    if not args.template:
        print("")
        print(f"File:        {args.wav_path}")
        print(f"Duration:    {duration_sec:.2f}s")
        print(f"Threshold:   {args.threshold}")
        print(f"Max score:   {max_score:.3f}")
        print("")
        print(f"Score peaks (no template; ≥{CANDIDATE_MIN_SCORE}, top {MAX_PEAKS_REPORTED}):")
        for i in peak_idxs:
            mark = "✓ YES" if scores[i] >= args.threshold else "   no"
            print(f"  t={i*FRAME_PERIOD_SEC:6.2f}s  score={scores[i]:.3f}  {mark}")
        print("")
        print(f"  Peaks total:            {len(peak_idxs)}")
        print(f"  Peaks above threshold:  {len(peaks_above)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
