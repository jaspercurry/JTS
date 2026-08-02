#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Print consistent PCM16 WAV sanity statistics for capture scripts."""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WavStats:
    path: Path
    frames: int
    rate: int
    channels: int
    mean: float
    rms: float
    peak: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.rate


def read_wav_stats(path: Path) -> WavStats:
    """Read first-channel, mean-centred statistics from a PCM16 WAV."""
    with wave.open(str(path), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        pcm = wav.readframes(frames)

    if sample_width != 2:
        raise ValueError(f"expected PCM16, got {sample_width * 8}-bit samples")
    if rate <= 0 or channels <= 0:
        raise ValueError("invalid WAV rate or channel count")

    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    channel = samples[::channels]
    if not channel:
        raise ValueError("WAV contains no audio frames")

    mean = sum(channel) / len(channel)
    rms = math.sqrt(sum((sample - mean) ** 2 for sample in channel) / len(channel))
    peak = max(abs(sample) for sample in channel)
    return WavStats(path, frames, rate, channels, mean, rms, peak)


def _dbfs(value: float) -> float:
    return 20 * math.log10(max(value, 1e-9) / 32768)


def format_wav_stats(stats: WavStats) -> str:
    return (
        f"  {stats.path.name:32s} {stats.duration_seconds:5.1f}s "
        f"{stats.rate:5d} Hz {stats.channels}ch  mean={stats.mean:+7.1f}  "
        f"RMS={stats.rms:6.0f} ({_dbfs(stats.rms):+6.1f} dBFS)  "
        f"peak={stats.peak:5d} ({_dbfs(stats.peak):+6.1f} dBFS)"
    )


def _wav_paths(inputs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for candidate in inputs:
        if candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.wav")))
        else:
            paths.append(candidate)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any input cannot be summarized",
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    failed = False
    for path in _wav_paths(args.paths):
        try:
            print(format_wav_stats(read_wav_stats(path)))
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            failed = True
            print(f"  {path.name}: ERROR {exc}", file=sys.stderr)
    return int(failed and args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
