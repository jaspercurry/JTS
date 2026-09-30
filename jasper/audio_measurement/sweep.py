# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Synchronized swept-sine (ESS) generation per Novak et al. 2015.

Synchronized rather than vanilla Farina ESS: harmonic-distortion impulses fall
at integer-fraction offsets of the linear IR, so deconvolution can discard them
(JAES 61(7), Novak, Lotton, Simon). Generated at the playback rate (48 kHz, to
match CamillaDSP). The sweep only, no inverse filter: :mod:`.deconv` inverts at
IR-extract time.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

from jasper.platform.json_fields import finite_float
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

    peak_dbfs = finite_float(amplitude_dbfs)
    if peak_dbfs is None or peak_dbfs > 0.0:
        raise ValueError("amplitude_dbfs must be a finite non-positive number")
    amplitude_dbfs = peak_dbfs
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
