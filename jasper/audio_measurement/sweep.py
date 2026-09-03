# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synchronized swept-sine (ESS) generation per Novak et al. 2015.

Synchronized rather than vanilla Farina ESS: harmonic-distortion impulses fall
at integer-fraction offsets of the linear IR, so deconvolution can discard them
(JAES 61(7), Novak, Lotton, Simon). Generated at the playback rate (48 kHz, to
match CamillaDSP) and saved as 16-bit S16_LE WAV so ``aplay`` can consume it.
The sweep only, no inverse filter: :mod:`.deconv` inverts at IR-extract time.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping

import numpy as np

from .excitation import AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepMeta:
    """What deconvolution needs to recover the IR, persisted beside the sweep WAV."""
    f1: float
    f2: float
    L: float
    duration_s: float
    n_samples: int
    sample_rate: int
    amplitude_dbfs: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "f1": self.f1, "f2": self.f2, "L": self.L,
            "duration_s": self.duration_s,
            "n_samples": self.n_samples,
            "sample_rate": self.sample_rate,
            "amplitude_dbfs": self.amplitude_dbfs,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SweepMeta":
        """Strictly reconstruct persisted synchronized-sweep metadata."""

        required = {
            "f1",
            "f2",
            "L",
            "duration_s",
            "n_samples",
            "sample_rate",
            "amplitude_dbfs",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("sweep metadata schema is invalid")

        def number(name: str) -> float:
            raw = value[name]
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(float(raw))
            ):
                raise ValueError(f"sweep metadata {name} must be finite numeric")
            return float(raw)

        if (
            type(value["n_samples"]) is not int
            or value["n_samples"] <= 0
            or type(value["sample_rate"]) is not int
            or value["sample_rate"] <= 0
        ):
            raise ValueError("sweep sample count and rate must be positive integers")
        f1 = number("f1")
        f2 = number("f2")
        rate = value["sample_rate"]
        length = number("L")
        duration = number("duration_s")
        amplitude = number("amplitude_dbfs")
        if not (
            0.0 < f1 < f2 < rate / 2.0
            and length > 0.0
            and duration > 0.0
            and amplitude <= 0.0
        ):
            raise ValueError("sweep metadata values are outside the valid domain")
        expected_duration = length * math.log(f2 / f1)
        expected_samples = int(round(expected_duration * rate))
        if (
            not math.isclose(duration, expected_duration, rel_tol=0.0, abs_tol=1e-9)
            or value["n_samples"] != expected_samples
            or not math.isclose(length * f1, round(length * f1), abs_tol=1e-9)
        ):
            raise ValueError("sweep synchronization metadata is inconsistent")
        return cls(
            f1=f1,
            f2=f2,
            L=length,
            duration_s=duration,
            n_samples=value["n_samples"],
            sample_rate=rate,
            amplitude_dbfs=amplitude,
        )


def synchronized_swept_sine(
    f1: float = 20.0,
    f2: float = 20000.0,
    duration_approx_s: float = 10.0,
    sample_rate: int = 48000,
    amplitude_dbfs: float = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
) -> tuple[np.ndarray, SweepMeta]:
    """Generate a synchronized exponential swept-sine.

    ``f2`` must be < ``sample_rate`` / 2; pin ``sample_rate`` to 48 kHz to match
    CamillaDSP. ``duration_approx_s`` is rounded so the sweep holds an integer
    number of cycles at ``f1`` (Novak's synchronization condition). Returns
    float32 in [-amp, amp], amp = 10**(amplitude_dbfs/20), plus the metadata
    deconvolution needs.
    """
    meta = synchronized_sweep_metadata(
        f1=f1,
        f2=f2,
        duration_approx_s=duration_approx_s,
        sample_rate=sample_rate,
        amplitude_dbfs=amplitude_dbfs,
    )

    t = np.arange(meta.n_samples, dtype=np.float64) / meta.sample_rate
    amp = 10 ** (meta.amplitude_dbfs / 20.0)
    phase = 2 * np.pi * meta.f1 * meta.L * (np.exp(t / meta.L) - 1)
    sweep = amp * np.sin(phase)

    # 5 ms fade at 48 kHz: removes the click from a sweep that does not end at a
    # zero crossing in float32, and masks DC offset on the playback chain.
    fade_samples = max(8, int(0.005 * meta.sample_rate))
    if fade_samples * 2 < meta.n_samples:
        fade_in = np.linspace(0.0, 1.0, fade_samples) ** 2
        fade_out = np.linspace(1.0, 0.0, fade_samples) ** 2
        sweep[:fade_samples] *= fade_in
        sweep[-fade_samples:] *= fade_out

    return sweep.astype(np.float32), meta


def synchronized_sweep_metadata(
    f1: float = 20.0,
    f2: float = 20000.0,
    duration_approx_s: float = 10.0,
    sample_rate: int = 48000,
    amplitude_dbfs: float = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
) -> SweepMeta:
    """Return exact synchronized-sweep metadata without allocating PCM.

    Excitation admission must bind the realized, phase-rounded duration before a
    signal may be generated.
    """

    if (
        isinstance(amplitude_dbfs, bool)
        or not isinstance(amplitude_dbfs, (int, float))
        or not math.isfinite(float(amplitude_dbfs))
        or float(amplitude_dbfs) > 0.0
    ):
        raise ValueError("amplitude_dbfs must be a finite non-positive number")
    amplitude_dbfs = float(amplitude_dbfs)
    if f1 <= 0:
        raise ValueError(f"f1 must be positive, got {f1}")
    if f2 <= f1:
        raise ValueError(f"f2 ({f2}) must be > f1 ({f1})")
    if f2 >= sample_rate / 2:
        raise ValueError(
            f"f2 ({f2}) must be < Nyquist ({sample_rate / 2}); "
            f"increase sample_rate or lower f2"
        )

    # Novak's synchronization condition: choose L (rate constant) so the cycle
    # count at f1 (T*f1, with T = L*ln(f2/f1)) is an integer, which makes the
    # harmonic-impulse offsets predictable.
    L_initial = duration_approx_s / math.log(f2 / f1)
    n_cycles_at_f1 = round(L_initial * f1)
    if n_cycles_at_f1 < 1:
        raise ValueError(
            f"duration_approx_s={duration_approx_s} too short for "
            f"f1={f1} (need at least one cycle at start)"
        )
    L = n_cycles_at_f1 / f1
    duration_s = L * math.log(f2 / f1)
    n_samples = int(round(duration_s * sample_rate))

    return SweepMeta(
        f1=float(f1), f2=float(f2), L=float(L),
        duration_s=float(duration_s),
        n_samples=int(n_samples),
        sample_rate=int(sample_rate),
        amplitude_dbfs=float(amplitude_dbfs),
    )


def phase_closing_duration_s(
    f1: float,
    f2: float,
    *,
    at_or_below_s: float,
    sample_rate: int = 48000,
) -> float:
    """The longest phase-closing sweep over ``[f1, f2]`` within ``at_or_below_s``.

    :func:`synchronized_sweep_metadata` rounds to the NEAREST phase-closing
    length, so a request equal to a ceiling can realize just above it (150-4000
    Hz asked for 4.0 s realizes 4.00577 s). The realized duration is quantized
    to whole cycles at ``f1``, so the step down from a length that overshoots is
    exactly one cycle, and ``round`` can only overshoot by half a cycle. Raises
    :class:`ValueError` when no phase-closing sweep of this band fits.
    """
    if not math.isfinite(at_or_below_s) or at_or_below_s <= 0.0:
        raise ValueError(
            f"at_or_below_s must be a positive finite number, got {at_or_below_s}"
        )
    meta = synchronized_sweep_metadata(
        f1=f1, f2=f2, duration_approx_s=at_or_below_s, sample_rate=sample_rate,
    )
    while meta.duration_s > at_or_below_s:
        n_cycles = round(meta.L * f1)
        if n_cycles <= 1:
            raise ValueError(
                f"no synchronized sweep of [{f1:g},{f2:g}] Hz closes its phase "
                f"within {at_or_below_s:g} s: one cycle at f1 already spans "
                f"{meta.duration_s:g} s"
            )
        meta = synchronized_sweep_metadata(
            f1=f1, f2=f2,
            duration_approx_s=meta.duration_s * (n_cycles - 1) / n_cycles,
            sample_rate=sample_rate,
        )
    return meta.duration_s


def write_sweep_wav(
    path: str | Path | BinaryIO,
    sweep: np.ndarray,
    sample_rate: int,
) -> None:
    """Write a mono float32 sweep as 16-bit PCM WAV (S16_LE)."""
    from scipy.io import wavfile

    if sweep.ndim != 1:
        raise ValueError(
            f"sweep must be mono (1-D), got shape {sweep.shape}"
        )
    clipped = np.clip(sweep, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    wavfile.write(path if hasattr(path, "write") else str(path), sample_rate, int16)


def read_wav_mono(
    path: str | Path,
) -> tuple[np.ndarray, int]:
    """Read a WAV file as mono float32 in [-1, 1]; stereo downmixed by average.

    Accepts 16-bit and 32-bit-float WAVs.
    """
    from scipy.io import wavfile

    sr, data = wavfile.read(str(path))
    # Capture the source dtype BEFORE downmixing: np.mean promotes an integer
    # array to float, so keying normalization off it afterwards would leave the
    # signal at ±32767 instead of ±1.0.
    source_dtype = data.dtype
    if data.ndim == 2:
        data = data.mean(axis=1)
    if np.issubdtype(source_dtype, np.integer):
        max_val = float(np.iinfo(source_dtype).max)
        signal = data.astype(np.float32) / max_val
    else:
        signal = data.astype(np.float32)
    return signal, int(sr)
