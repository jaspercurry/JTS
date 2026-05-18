#!/usr/bin/env python3
"""Run openWakeWord on a captured WAV and count detections.

Used by `scripts/wake-rate-test.sh` to count wakes WITHOUT running
jasper-voice live (so we don't open LLM sessions / play TTS / pay
$0.05/turn × 60 wakes × 3 conditions during testing). Uses the same
model + threshold + frame size as production, so the count should
match what jasper-voice would have produced live.

Frame size is 1280 samples (80 ms @ 16 kHz), which matches the
bridge's UDP packet size and jasper-voice's WakeLoop frame
processing. openWakeWord internally chunks to its model's preferred
size (typically 1280 samples for v0.6.x).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np


SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280
WARMUP_SEC = 1.0       # skip detections in first 1 s (model state warmup)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav_path", type=Path)
    ap.add_argument("--model",
                    default=os.environ.get(
                        "JASPER_WAKE_MODEL",
                        "/var/lib/jasper/wake/jarvis_v2.onnx",
                    ),
                    help="Path to .onnx wake-word model")
    ap.add_argument("--threshold", type=float,
                    default=float(os.environ.get(
                        "JASPER_WAKE_THRESHOLD", "0.30",
                    )),
                    help="Match production threshold (default 0.30)")
    ap.add_argument("--refractory-sec", type=float, default=1.0,
                    help="Min seconds between counted detections "
                         "(prevents one utterance counting as many)")
    ap.add_argument("--json", action="store_true",
                    help="Output JSON instead of human-readable")
    args = ap.parse_args()

    if not args.wav_path.exists():
        print(f"ERROR: {args.wav_path} missing", file=sys.stderr)
        return 1
    if not Path(args.model).exists():
        print(f"ERROR: model {args.model} missing", file=sys.stderr)
        return 1

    with wave.open(str(args.wav_path)) as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        if sr != SAMPLE_RATE or ch != 1 or sw != 2:
            print(f"ERROR: {args.wav_path} must be {SAMPLE_RATE}Hz mono S16; "
                  f"got {sr}Hz {ch}ch {sw*8}-bit", file=sys.stderr)
            return 1
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    duration_sec = len(audio) / SAMPLE_RATE

    # Lazy-import openwakeword (slow startup, ~1 s on Pi 5).
    from openwakeword.model import Model
    model = Model(wakeword_models=[args.model])
    # Model dict key = the model filename without extension.
    model_key = Path(args.model).stem

    detections: list[dict] = []
    scores_max = 0.0
    last_det_t = -1e9
    warmup_samples = int(WARMUP_SEC * SAMPLE_RATE)

    for i in range(0, len(audio) - CHUNK_SAMPLES + 1, CHUNK_SAMPLES):
        chunk = audio[i:i + CHUNK_SAMPLES]
        scores = model.predict(chunk)
        score = float(scores.get(model_key, 0.0))
        scores_max = max(scores_max, score)
        t = i / SAMPLE_RATE
        if i < warmup_samples:
            continue
        if score >= args.threshold and (t - last_det_t) >= args.refractory_sec:
            detections.append({"t_sec": round(t, 2), "score": round(score, 3)})
            last_det_t = t

    if args.json:
        print(json.dumps({
            "wav": str(args.wav_path),
            "duration_sec": round(duration_sec, 2),
            "model": args.model,
            "threshold": args.threshold,
            "refractory_sec": args.refractory_sec,
            "max_score_seen": round(scores_max, 3),
            "detections": detections,
            "count": len(detections),
        }, indent=2))
    else:
        print(f"Wave:        {args.wav_path}")
        print(f"Duration:    {duration_sec:.2f}s")
        print(f"Model:       {args.model}")
        print(f"Threshold:   {args.threshold}")
        print(f"Refractory:  {args.refractory_sec}s")
        print(f"Max score:   {scores_max:.3f}")
        print(f"")
        print(f"Detections:  {len(detections)}")
        for d in detections:
            print(f"  t={d['t_sec']:6.2f}s  score={d['score']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
