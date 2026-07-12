#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Generate the deterministic broadband click used by multi-room benches."""

from __future__ import annotations

import argparse
from pathlib import Path
import random
import struct
import wave


SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH_BYTES = 2
CLICK_SECONDS = 0.002
GAP_SECONDS = 0.998
REPETITIONS = 60
AMPLITUDE_DBFS = -20.0
SEED = 1234


def _click_frames() -> bytes:
    """Return one fixed-seed, lightly band-limited stereo noise burst."""
    amplitude = 32767 * (10 ** (AMPLITUDE_DBFS / 20))
    rng = random.Random(SEED)
    raw = [rng.uniform(-1, 1) for _ in range(int(CLICK_SECONDS * SAMPLE_RATE))]
    smoothed: list[float] = []
    previous = 0.0
    for sample in raw:
        previous = 0.5 * previous + 0.5 * sample
        smoothed.append(previous)
    peak = max(abs(value) for value in smoothed) or 1.0
    mono = [int(amplitude * value / peak) for value in smoothed]
    return b"".join(struct.pack("<hh", sample, sample) for sample in mono)


def render_click_track(path: Path, *, container: str) -> None:
    """Write identical PCM as raw S16LE stereo or as a WAV container."""
    if container not in {"raw", "wav"}:
        raise ValueError(f"unsupported container {container!r}")
    click = _click_frames()
    gap = b"\0" * (int(GAP_SECONDS * SAMPLE_RATE) * CHANNELS * SAMPLE_WIDTH_BYTES)
    if container == "raw":
        with path.open("wb") as output:
            for _ in range(REPETITIONS):
                output.write(click)
                output.write(gap)
        return
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH_BYTES)
        output.setframerate(SAMPLE_RATE)
        for _ in range(REPETITIONS):
            output.writeframesraw(click)
            output.writeframesraw(gap)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate JTS's deterministic 1 Hz broadband click track.",
    )
    parser.add_argument("--format", choices=("raw", "wav"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render_click_track(args.output, container=args.format)
    duration_seconds = REPETITIONS * (CLICK_SECONDS + GAP_SECONDS)
    print(
        f"click.{args.format}: {args.output} "
        f"({args.output.stat().st_size} bytes, {duration_seconds:.1f}s "
        f"@{SAMPLE_RATE // 1000}k S16 stereo)",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
