# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WAV decoding and click-onset detection for ``multiroom-spike-measure.py``."""
from __future__ import annotations

import array
import sys
import wave
from collections.abc import Sequence
from os import PathLike


class UnsupportedSampleWidth(ValueError):
    """The WAV sample width is not the 16 bits the analyzer requires."""

    def __init__(self, sample_width: int) -> None:
        self.sample_width = sample_width
        super().__init__(f"unsupported sample width {sample_width}")


def read_wav_mono(path: str | PathLike[str]) -> tuple[list[int], int]:
    """Decode a 16-bit PCM WAV and return mono samples plus its sample rate.

    Multi-channel input is downmixed by floor division.
    """
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())

    if sample_width != 2:
        raise UnsupportedSampleWidth(sample_width)

    pcm = array.array("h")
    pcm.frombytes(raw)
    if sys.byteorder != "little":
        pcm.byteswap()

    if channels == 1:
        return list(pcm), sample_rate
    return [
        sum(pcm[index:index + channels]) // channels
        for index in range(0, len(pcm), channels)
    ], sample_rate


def find_energy_onsets(
    samples: Sequence[int],
    sample_rate: int,
    *,
    min_gap_s: float = 0.5,
) -> list[int]:
    """Find click onsets using a 5 ms energy window and 15% peak threshold."""
    window = max(1, int(0.005 * sample_rate))
    energy = _trailing_moving_sum([sample * sample for sample in samples], window)

    peak = max(energy, default=0)
    if peak <= 0:
        return []

    threshold = peak * 0.15
    refractory = int(min_gap_s * sample_rate)
    onsets: list[int] = []
    last = -refractory
    for index, value in enumerate(energy):
        if value > threshold and index - last > refractory:
            onsets.append(index)
            last = index
    return onsets


def _trailing_moving_sum(samples: Sequence[int], window: int) -> list[int]:
    energy: list[int] = []
    accumulator = 0
    for index, sample in enumerate(samples):
        accumulator += sample
        if index >= window:
            accumulator -= samples[index - window]
        energy.append(accumulator)
    return energy
