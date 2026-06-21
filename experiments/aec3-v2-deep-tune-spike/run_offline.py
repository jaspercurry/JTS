#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Process all 10 reference-conditions cells through AEC3 v2.1 with
the research-report-recommended starting config, and score the
output with jarvis_v2. Compare to AEC3-stock, DTLN-128, DTLN-256.
"""
import math
import sys
import wave
from pathlib import Path

import numpy as np

# Make our locally-compiled extension importable
sys.path.insert(0, "/tmp/aec3v2-spike")
from _aec3_v2_spike import Aec3V2  # noqa: E402

REPO = Path("/Users/jaspercurry/Code/JTS/.claude/worktrees/hardcore-herschel-c614a3")
BASELINE = REPO / "reference-conditions"
CONDS = [
    "normal-quiet", "normal-music",
    "whisper-quiet", "whisper-music",
    "yell-quiet", "yell-music",
    "fast-quiet", "fast-music",
    "slow-quiet", "slow-music",
]


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        if sr != 16000 or ch != 1 or sw != 2:
            raise ValueError(f"{path}: expected 16k mono int16")
        return w.readframes(n)


def write_pcm(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)


def main() -> int:
    for cond in CONDS:
        mic_path = BASELINE / cond / "aec-off.wav"
        ref_path = BASELINE / cond / "reference.wav"
        out_path = BASELINE / cond / "aec-v2tuned.wav"

        mic = load_pcm(mic_path)
        ref = load_pcm(ref_path)
        # Pad to equal length (ref may be silent shorter)
        n = min(len(mic), len(ref))
        mic = mic[:n]
        ref = ref[:n]
        # AEC requires 10ms (320-byte) aligned. Truncate to a clean boundary.
        n_aligned = (n // 320) * 320
        mic = mic[:n_aligned]
        ref = ref[:n_aligned]

        aec = Aec3V2()  # all defaults = research-recommended starting config
        out = aec.process(mic, ref)
        write_pcm(out_path, out)

        # Stats
        s = np.frombuffer(out, dtype=np.int16)
        peak = int(np.abs(s).max())
        rms = float(np.sqrt(np.mean(s.astype(np.float32) ** 2)))
        print(
            f"  {cond:<14s} → aec-v2tuned.wav  "
            f"peak={peak:6d} ({20*math.log10(max(peak,1)/32768):+5.1f} dBFS)  "
            f"RMS={rms:5.0f} ({20*math.log10(max(rms,1)/32768):+5.1f} dBFS)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
