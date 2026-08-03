# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared WAV decoding and click-onset detection for sync spike scripts.

The public scripts keep their deliberately different numeric contracts:
``s0-sync-measure.py`` consumes normalized floats and a centered energy
window, while ``multiroom-spike-measure.py`` consumes integer PCM and the
historical trailing energy window.  Keeping those choices as parameters here
removes the duplicated mechanics without moving either script's measurements.
"""
from __future__ import annotations

import array
import sys
import wave
from collections.abc import Sequence
from os import PathLike


class UnsupportedSampleWidth(ValueError):
    """The WAV sample width is outside a caller's established policy."""

    def __init__(self, sample_width: int) -> None:
        self.sample_width = sample_width
        super().__init__(f"unsupported sample width {sample_width}")


def read_wav_mono(
    path: str | PathLike[str],
    *,
    supported_sample_widths: tuple[int, ...],
    normalize: bool,
) -> tuple[list[int] | list[float], int]:
    """Decode a PCM WAV and return mono samples plus its sample rate.

    Multi-channel integer output retains the spike script's floor-division
    downmix.  Normalized output takes the arithmetic mean before scaling,
    matching the S0 analyzer's NumPy implementation.
    """
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())

    if sample_width not in supported_sample_widths:
        raise UnsupportedSampleWidth(sample_width)

    typecode = {2: "h", 4: "i"}[sample_width]
    pcm = array.array(typecode)
    pcm.frombytes(raw)
    if sys.byteorder != "little":
        pcm.byteswap()

    if normalize:
        divisor = float(1 << (sample_width * 8 - 1))
        if channels == 1:
            return [sample / divisor for sample in pcm], sample_rate
        return [
            sum(pcm[index:index + channels]) / channels / divisor
            for index in range(0, len(pcm), channels)
        ], sample_rate

    if channels == 1:
        return list(pcm), sample_rate
    return [
        sum(pcm[index:index + channels]) // channels
        for index in range(0, len(pcm), channels)
    ], sample_rate


def find_energy_onsets(
    samples: Sequence[int] | Sequence[float],
    sample_rate: int,
    *,
    min_gap_s: float = 0.5,
    centered_window: bool = False,
) -> list[int]:
    """Find click onsets using a 5 ms energy window and 15% peak threshold.

    ``centered_window=True`` matches ``numpy.convolve(..., mode="same")``.
    The trailing form matches the original pure-Python rolling accumulator.
    Both retain the strict ``> min_gap`` refractory comparison.
    """
    window = max(1, int(0.005 * sample_rate))
    squared = [sample * sample for sample in samples]
    if centered_window:
        energy = _centered_moving_sum(squared, window)
    else:
        energy = _trailing_moving_sum(squared, window)

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


def _trailing_moving_sum(
    samples: Sequence[int] | Sequence[float],
    window: int,
) -> list[int | float]:
    energy: list[int | float] = []
    accumulator: int | float = 0
    for index, sample in enumerate(samples):
        accumulator += sample
        if index >= window:
            accumulator -= samples[index - window]
        energy.append(accumulator)
    return energy


def _centered_moving_sum(
    samples: Sequence[int] | Sequence[float],
    window: int,
) -> list[int | float]:
    """Return NumPy ``convolve(samples, ones(window), mode="same")``."""
    sample_count = len(samples)
    if sample_count == 0:
        raise ValueError("v cannot be empty")

    prefix: list[int | float] = [0]
    for sample in samples:
        prefix.append(prefix[-1] + sample)

    output_length = max(sample_count, window)
    output_start = (min(sample_count, window) - 1) // 2
    energy: list[int | float] = []
    for output_index in range(output_length):
        full_index = output_index + output_start
        sample_start = max(0, full_index - window + 1)
        sample_end = min(sample_count, full_index + 1)
        energy.append(prefix[sample_end] - prefix[sample_start])
    return energy
