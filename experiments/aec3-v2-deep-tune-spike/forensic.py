#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Forensic audio analysis comparing raw / AEC3 / V2tune across cells.

Quantifies: clipping, peak/RMS, AGC pumping (variance of moving-window
RMS), HF tearing (per-frame magnitude jumpiness in 3-7 kHz), spectral
flatness, frame-boundary discontinuities, "blown out" indicators.

Run after process_baseline.py has produced aec-v2tuned.wav etc.
"""

import math
import wave
from pathlib import Path

import numpy as np

BASELINE = Path("/Users/jaspercurry/Code/JTS/.claude/worktrees/hardcore-herschel-c614a3/reference-conditions")
SR = 16000

# Cells to scrutinize — failure cells first
CELLS = ["whisper-music", "fast-music", "yell-music", "normal-music"]
LEGS = [
    ("raw",    "aec-off.wav"),
    ("AEC3",   "aec-on.wav"),
    ("V2tune", "aec-v2tuned.wav"),
]


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def metrics(s_i16: np.ndarray) -> dict:
    """All metrics in float space."""
    s = s_i16.astype(np.float32)
    n = len(s)
    peak = float(np.abs(s).max())
    rms = float(np.sqrt(np.mean(s ** 2)))
    # Clipping: count of samples at or beyond int16 max
    clip_count = int(((np.abs(s_i16) >= 32767)).sum())
    clip_pct = 100.0 * clip_count / max(n, 1)
    # Crest factor (peak/RMS, in dB)
    crest_db = 20 * math.log10(max(peak, 1) / max(rms, 1))

    # AGC pumping: variance of moving-window RMS at ~50 ms windows
    win = SR // 20  # 50 ms
    if n >= win * 4:
        # Compute RMS in non-overlapping windows
        n_win = n // win
        windows = s[: n_win * win].reshape(n_win, win)
        win_rms = np.sqrt(np.mean(windows ** 2, axis=1) + 1e-9)
        # Normalize to mean and report coefficient of variation
        pump = float(np.std(win_rms) / max(np.mean(win_rms), 1e-9))
    else:
        pump = 0.0

    # HF tearing proxy: STFT-based, look at 3-7 kHz bin energy variance
    # frame-by-frame. If RS is gating bins, we'll see jumpy HF energy.
    # 512-point FFT @ 16 kHz = 31.25 Hz per bin → 3 kHz = bin 96, 7 kHz = bin 224
    frame = 512
    hop = 128
    if n >= frame:
        n_frames = (n - frame) // hop + 1
        hf_energies = np.zeros(n_frames)
        for i in range(n_frames):
            f = s[i * hop : i * hop + frame] * np.hanning(frame)
            spec = np.abs(np.fft.rfft(f))
            hf_energies[i] = float(np.sum(spec[96:224] ** 2))
        # Coefficient of variation of HF energy — if RS is gating bins
        # frame-by-frame, this'll be high.
        mean_hf = float(np.mean(hf_energies))
        if mean_hf > 0:
            hf_cv = float(np.std(hf_energies) / mean_hf)
        else:
            hf_cv = 0.0
        # Spectral flatness — high = noise-like, low = tonal-like
        # Use just the HF band
        gmean = np.exp(np.mean(np.log(hf_energies + 1e-9)))
        amean = float(np.mean(hf_energies + 1e-9))
        flatness_hf = float(gmean / amean) if amean > 0 else 0.0
    else:
        hf_cv = flatness_hf = 0.0

    # Frame-boundary discontinuity: look for clicks at AEC3's 10ms (160-sample)
    # boundaries — sudden sample-to-sample jumps.
    if n > 160 * 4:
        # Compute sample-to-sample differences, look at distribution at boundary frames
        diffs = np.diff(s.astype(np.float32))
        # Find indices where a frame boundary lands
        n_boundaries = (n - 1) // 160
        boundary_idxs = np.arange(n_boundaries) * 160 + 159
        boundary_idxs = boundary_idxs[boundary_idxs < len(diffs)]
        boundary_diffs = np.abs(diffs[boundary_idxs])
        non_boundary_diffs = np.abs(np.delete(diffs, boundary_idxs))
        if len(non_boundary_diffs) > 0 and np.median(non_boundary_diffs) > 0:
            boundary_jump_ratio = float(
                np.median(boundary_diffs) / max(np.median(non_boundary_diffs), 1.0)
            )
        else:
            boundary_jump_ratio = 1.0
    else:
        boundary_jump_ratio = 1.0

    return {
        "peak_dbfs": 20 * math.log10(max(peak, 1) / 32768),
        "rms_dbfs": 20 * math.log10(max(rms, 1) / 32768),
        "clip_pct": clip_pct,
        "crest_db": crest_db,
        "pump_cv": pump,
        "hf_CV": hf_cv,
        "hf_flatness": flatness_hf,
        "boundary_ratio": boundary_jump_ratio,
    }


def main() -> None:
    print(f"{'cell':<14s} {'leg':<7s} {'peak':>7s} {'RMS':>7s} {'clip%':>6s} {'crest':>6s} {'pump':>6s} {'hf_CV':>6s} {'hf_flat':>7s} {'fbnd_x':>7s}")
    print("-" * 88)
    for cell in CELLS:
        for leg_name, fname in LEGS:
            path = BASELINE / cell / fname
            if not path.is_file():
                continue
            s = load(path)
            m = metrics(s)
            print(
                f"{cell:<14s} {leg_name:<7s} "
                f"{m['peak_dbfs']:+6.1f} {m['rms_dbfs']:+6.1f}  "
                f"{m['clip_pct']:5.2f} {m['crest_db']:5.1f} "
                f"{m['pump_cv']:6.3f} {m['hf_CV']:6.3f} "
                f"{m['hf_flatness']:7.4f} {m['boundary_ratio']:6.2f}"
            )
        print()


if __name__ == "__main__":
    main()
