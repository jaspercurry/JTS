#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compute ERLE numbers from two WAV files produced by aec-erle-record.sh.

Asks three questions:

  1. Apparent broadband attenuation (mic vs AEC output RMS, in dB).
     This is what the operator hears when they listen back to the
     captures, and what the bridge logs every 5 s as "attenuation".

  2. Speech-band attenuation (300-3400 Hz bandpassed). This is what
     matters for wake-word detection — phonemes live in this band.
     A high broadband number with low speech-band number means the
     bridge is cancelling bass + hiss, not actual echo.

  3. Per-second distribution (1-s windows). A high mean with high
     variance means the bridge cancels well on easy frames and
     poorly on hard ones — the user experiences the bad frames.

The recording is assumed to be music with no speech (so the mic
content is pure echo + ambient). Caller should drop the first 5 s
to skip startup transients (default).

Usage:
    python3 scripts/aec_erle_analyze.py MIC.wav AEC.wav
    python3 scripts/aec_erle_analyze.py MIC.wav AEC.wav --mic-gain-db 6 --skip 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy import signal

try:
    from _wake_audio_metrics import rms_amplitude as _rms
except ModuleNotFoundError as exc:
    if exc.name != "_wake_audio_metrics":
        raise
    from scripts._wake_audio_metrics import rms_amplitude as _rms


SAMPLE_RATE_EXPECTED = 16000


def _load_mono(path: Path) -> np.ndarray:
    sr, data = wavfile.read(path)
    if sr != SAMPLE_RATE_EXPECTED:
        raise SystemExit(
            f"{path}: sample rate {sr} != expected {SAMPLE_RATE_EXPECTED}"
        )
    if data.ndim == 2:
        # If somehow stereo, take first channel.
        data = data[:, 0]
    return data.astype(np.float32)


def _db(numer: float, denom: float, eps: float = 1.0) -> float:
    return 20.0 * np.log10(max(numer, eps) / max(denom, eps))


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("mic", type=Path, help="bridge input (chip mic ch1)")
    p.add_argument("aec", type=Path, help="bridge output (post-AEC UDP)")
    p.add_argument(
        "--skip", type=float, default=5.0,
        help="seconds to skip at start (startup transients). default 5",
    )
    p.add_argument(
        "--mic-gain-db", type=float, default=6.0,
        help="post-AEC MIC_GAIN_DB (env JASPER_AEC_MIC_GAIN_DB). "
             "default 6 — used to back out the makeup gain when "
             "estimating the AEC engine's pre-gain cancellation",
    )
    p.add_argument(
        "--window-sec", type=float, default=1.0,
        help="per-window length for the distribution stats. default 1.0",
    )
    args = p.parse_args()

    mic = _load_mono(args.mic)
    aec = _load_mono(args.aec)

    # Trim to common length so RMS comparisons see the same content.
    # The two captures start within ~1 s of each other; for whole-file
    # RMS this drift is irrelevant. (No xcorr alignment — we're not
    # computing per-sample error; per-window RMS is robust to ±1s.)
    n = min(len(mic), len(aec))
    skip = int(args.skip * SAMPLE_RATE_EXPECTED)
    if n <= skip + SAMPLE_RATE_EXPECTED:
        raise SystemExit(
            f"capture too short after --skip={args.skip}s — got {n} samples"
        )
    mic = mic[skip:n]
    aec = aec[skip:n]
    analysed_sec = (n - skip) / SAMPLE_RATE_EXPECTED

    # Broadband (matches bridge log convention).
    mic_rms = _rms(mic)
    aec_rms = _rms(aec)
    broadband_db = _db(aec_rms, mic_rms)

    # Speech-band: 300-3400 Hz, 4th-order Butter SOS.
    sos = signal.butter(4, [300, 3400], btype="band",
                        fs=SAMPLE_RATE_EXPECTED, output="sos")
    mic_sb = signal.sosfilt(sos, mic)
    aec_sb = signal.sosfilt(sos, aec)
    mic_sb_rms = _rms(mic_sb)
    aec_sb_rms = _rms(aec_sb)
    speech_db = _db(aec_sb_rms, mic_sb_rms)

    # Per-window distribution (broadband).
    win = int(args.window_sec * SAMPLE_RATE_EXPECTED)
    per_win = []
    for i in range(0, len(mic) - win, win):
        m = _rms(mic[i:i + win])
        a = _rms(aec[i:i + win])
        if m > 1.0:  # skip silent windows
            per_win.append(_db(a, m))
    per_win_arr = np.array(per_win) if per_win else np.array([0.0])

    # Pre-MIC_GAIN estimate: the bridge applies +MIC_GAIN_DB after the
    # AEC engine, so the engine itself attenuated by (apparent + GAIN).
    # tanh soft-clip below ±26000 is ≈linear, so this is a good
    # first-order back-out as long as the output isn't clipping.
    aec_clip_pct = float(np.mean(np.abs(aec) > 32000.0) * 100.0)
    pre_gain_db = broadband_db - args.mic_gain_db

    print()
    print("=== AEC3 ERLE analysis ===")
    print(f"Mic input:    {args.mic}")
    print(f"AEC output:   {args.aec}")
    print(f"Analysed:     {analysed_sec:.1f}s (after {args.skip:.0f}s skip)")
    print()
    print("Broadband (full spectrum, matches bridge log):")
    print(f"  Mic RMS:        {mic_rms:7.1f}")
    print(f"  AEC RMS:        {aec_rms:7.1f}")
    print(f"  Attenuation:    {broadband_db:+6.1f} dB")
    print()
    print("Speech band (300-3400 Hz, where wake-word phonemes live):")
    print(f"  Mic RMS:        {mic_sb_rms:7.1f}")
    print(f"  AEC RMS:        {aec_sb_rms:7.1f}")
    print(f"  Attenuation:    {speech_db:+6.1f} dB")
    print()
    print(f"Per-{args.window_sec:.1f}s windows (broadband, N={len(per_win)}):")
    print(f"  Mean:           {np.mean(per_win_arr):+6.1f} dB")
    print(f"  Std:            {np.std(per_win_arr):6.1f} dB")
    print(f"  Best frame:     {np.min(per_win_arr):+6.1f} dB")
    print(f"  Worst frame:    {np.max(per_win_arr):+6.1f} dB")
    print()
    print("Estimated pre-MIC_GAIN cancellation (engine-only):")
    print(f"  Approx:         {pre_gain_db:+6.1f} dB "
          f"(broadband {broadband_db:+.1f} − {args.mic_gain_db:+.0f} dB makeup)")
    print(f"  AEC output clip:{aec_clip_pct:6.2f}%  "
          f"(>0.5% means MIC_GAIN+tanh is destroying peaks)")
    print()
    print("=== Interpretation hints ===")
    if speech_db > -3:
        print("  ⚠  Speech-band attenuation is < 3 dB — AEC is doing")
        print("     essentially nothing where wake words live. This")
        print("     matches the 'I don't hear improvement' perception.")
    elif speech_db > -8:
        print("  ⚠  Speech-band attenuation is 3-8 dB — AEC is helping,")
        print("     but not enough to mask music residual at wake-word")
        print("     speech frequencies.")
    else:
        print(f"  ✓  Speech-band attenuation {speech_db:+.1f} dB — AEC is")
        print("     doing real work in the band that matters.")
    if broadband_db - speech_db < -5:
        print(f"  ⚠  Broadband ({broadband_db:+.1f}) is much stronger than")
        print(f"     speech-band ({speech_db:+.1f}) — most cancellation is")
        print("     outside speech (bass, hiss). HPF + NS doing the lifting.")
    if np.std(per_win_arr) > 5:
        print(f"  ⚠  Per-window std is {np.std(per_win_arr):.1f} dB — AEC is")
        print("     unstable across frames. User experiences the worst, not")
        print("     the mean.")
    if aec_clip_pct > 0.5:
        print(f"  ⚠  AEC output is clipping ({aec_clip_pct:.1f}%) — the")
        print("     MIC_GAIN+tanh stage is destroying information. Lower")
        print("     JASPER_AEC_MIC_GAIN_DB or check input levels.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
