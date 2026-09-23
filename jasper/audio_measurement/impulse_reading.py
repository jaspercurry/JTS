# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What one measured impulse says: its onset against the noise before it, how
its energy decays, and its timing by frequency.

Pure functions of an impulse and its sample rate. Each takes the window it
reads through as an argument, since a phase or a group delay means nothing
without one (ADR-0355).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .analysis import smooth_fractional_octave
from .excess_phase import PHASE_NFFT, GD_SPAN_OCT, _window_slopes, excess_group_delay, gate
from .gating import analytic_envelope, f_trusted_floor_hz

#: ISO 3382-1's start of an impulse: the first sample within this of its peak.
ONSET_BELOW_PEAK_DB = 20.0
#: The noise a peak is read against, in ms before the onset: late enough to
#: clear the deconvolution's pre-ringing, early enough to stay clear of a short
#: sweep's harmonic impulses, which land further ahead of the peak.
NOISE_BEFORE_ONSET_MS = (50.0, 10.0)
#: Where the energy-time curve is read, in ms after the direct peak. Each
#: reading averages the energy over +/- :data:`ETC_SPAN_FRACTION` of its time.
ETC_READ_MS = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)
ETC_SPAN_FRACTION = 0.1


@dataclass(frozen=True)
class ImpulseShape:
    """An impulse's peak, onset and decay, in samples and dB."""

    peak_index: int
    onset_index: int
    polarity: int
    peak_to_noise_db: float | None
    #: ``(ms after the peak, energy dB re the peak)``; ``None`` past the samples kept.
    etc_db: tuple[tuple[float, float | None], ...]


def energy_time_db(samples: np.ndarray, *, peak_index: int) -> np.ndarray:
    """The energy-time curve: the squared analytic envelope, dB re its value at the peak."""
    energy = analytic_envelope(np.asarray(samples, dtype=np.float64)) ** 2
    reference = float(energy[peak_index]) or float(np.max(energy)) or 1.0
    return 10 * np.log10(np.maximum(energy, 1e-30) / reference)


def impulse_shape(samples: np.ndarray, sample_rate: int, *, peak_index: int | None = None) -> ImpulseShape:
    x = np.asarray(samples, dtype=np.float64)
    peak = int(np.argmax(np.abs(x))) if peak_index is None else int(peak_index)
    level = float(abs(x[peak]))
    above = np.flatnonzero(np.abs(x[:peak + 1]) >= level * 10 ** (-ONSET_BELOW_PEAK_DB / 20))
    onset = int(above[0]) if above.size else peak
    far, near = (round(ms * sample_rate / 1000) for ms in NOISE_BEFORE_ONSET_MS)
    noise = x[max(0, onset - far):max(0, onset - near)]
    noise_rms = float(np.sqrt(np.mean(noise ** 2))) if noise.size else 0.0
    energy = 10 ** (energy_time_db(x, peak_index=peak) / 10)

    def etc(ms: float) -> float | None:
        centre = peak + ms * sample_rate / 1000
        half = max(1.0, ETC_SPAN_FRACTION * ms * sample_rate / 1000)
        lo, hi = int(round(centre - half)), int(round(centre + half)) + 1
        if hi > energy.size:
            return None
        return float(10 * np.log10(max(float(np.mean(energy[lo:hi])), 1e-30)))

    return ImpulseShape(
        peak_index=peak, onset_index=onset, polarity=1 if x[peak] >= 0 else -1,
        peak_to_noise_db=float(20 * np.log10(level / noise_rms)) if noise_rms > 0 and level > 0 else None,
        etc_db=tuple((ms, etc(ms)) for ms in ETC_READ_MS),
    )


def step_response(samples: np.ndarray) -> np.ndarray:
    """The running sum of an impulse, scaled so its largest excursion is 1."""
    step = np.cumsum(np.asarray(samples, dtype=np.float64))
    extreme = float(np.max(np.abs(step))) if step.size else 0.0
    return step / extreme if extreme else step


@dataclass(frozen=True)
class TimingByFrequency:
    """A windowed response's magnitude, phase and delays on a log grid.

    Time zero is the direct peak: phase and group delay are relative to the
    arrival, so a flat group delay at zero is a response with no dispersion.
    ``excess_group_delay_ms`` has the bulk delay its own fit removed, so only its
    shape is read; ``None`` where the band is too narrow for that fit.
    """

    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    phase_deg: np.ndarray
    group_delay_ms: np.ndarray
    excess_group_delay_ms: np.ndarray | None
    band_hz: tuple[float, float]


def trusted_band_hz(window_ms: float, radiated_band_hz: tuple[float, float],
                    sample_rate: int) -> tuple[float, float] | None:
    """Where a ``window_ms`` read of a sweep over ``radiated_band_hz`` can be trusted."""
    lo = max(float(radiated_band_hz[0]), f_trusted_floor_hz(window_ms / 1000))
    hi = min(float(radiated_band_hz[1]), sample_rate / 2)
    return (lo, hi) if hi > lo * 1.5 else None


def timing_by_frequency(
    samples: np.ndarray,
    sample_rate: int,
    *,
    peak_index: int,
    window_ms: float,
    lead_ms: float,
    band_hz: tuple[float, float],
    points_per_octave: int = 24,
) -> TimingByFrequency:
    """Read ``samples`` through a fixed window from its peak over ``band_hz``."""
    segment = gate(samples, sample_rate, gate_ms=window_ms, lead_ms=lead_ms, peak=peak_index)
    lead = peak_index - max(0, peak_index - int(round(lead_ms * 1e-3 * sample_rate)))
    freqs = np.fft.rfftfreq(PHASE_NFFT, d=1.0 / sample_rate)
    omega = 2 * np.pi * freqs
    spectrum = np.fft.rfft(segment, n=PHASE_NFFT) * np.exp(1j * omega * lead / sample_rate)
    phase = np.unwrap(np.angle(spectrum))
    lo_idx = np.searchsorted(freqs, freqs * 2 ** -GD_SPAN_OCT)
    hi_idx = np.minimum(np.searchsorted(freqs, freqs * 2 ** GD_SPAN_OCT) + 1, freqs.size)
    group_delay_s = -_window_slopes(phase, lo_idx, hi_idx) / (2 * np.pi * sample_rate / PHASE_NFFT)

    lo, hi = band_hz
    grid = np.geomspace(lo, hi, max(2, int(round(np.log2(hi / lo) * points_per_octave)) + 1))
    magnitude = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-12))
    in_band = (freqs >= lo) & (freqs <= hi)
    magnitude_db = np.interp(grid, freqs, magnitude) - float(np.max(magnitude[in_band]))
    # Referenced at the band's low edge: an unwrapped phase's absolute turn count
    # below it is the zero-padded transform's, not the speaker's.
    phase_deg = np.degrees(np.interp(grid, freqs, phase) - 2 * np.pi * round(
        float(np.interp(lo, freqs, phase)) / (2 * np.pi)))
    excess = None
    if hi > lo * 2:
        read = excess_group_delay(segment, sample_rate, trusted_band_hz=(lo, hi),
                                  fit_band_hz=(lo, hi), edge_band_hz=(lo, hi))
        excess = np.interp(grid, read.freqs, read.excess_gd_us / 1000, left=np.nan, right=np.nan)
    return TimingByFrequency(
        freqs_hz=grid, magnitude_db=magnitude_db, phase_deg=phase_deg,
        group_delay_ms=np.interp(grid, freqs, group_delay_s * 1000),
        excess_group_delay_ms=excess, band_hz=(float(lo), float(hi)),
    )


def magnitude_db(
    samples: np.ndarray,
    sample_rate: int,
    *,
    peak_index: int,
    window_ms: float,
    lead_ms: float,
    grid_hz: np.ndarray,
    smoothing_fraction: int | None = None,
) -> np.ndarray:
    """The magnitude of ``samples`` read through a fixed window from its peak,
    power-smoothed to 1/``smoothing_fraction`` octave when given, on ``grid_hz``.
    Uncalibrated dB."""
    segment = gate(samples, sample_rate, gate_ms=window_ms, lead_ms=lead_ms, peak=peak_index)
    spectrum = np.fft.rfft(segment, n=max(PHASE_NFFT, 1 << (segment.size - 1).bit_length()))
    freqs = np.fft.rfftfreq(2 * (spectrum.size - 1), d=1.0 / sample_rate)[1:]
    db = 20 * np.log10(np.maximum(np.abs(spectrum[1:]), 1e-12))
    if smoothing_fraction:
        db = smooth_fractional_octave(freqs, db, smoothing_fraction)
    return np.interp(grid_hz, freqs, db)
