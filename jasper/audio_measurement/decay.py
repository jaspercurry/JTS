# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How a measured impulse decays, octave by octave: the Schroeder backward
integral and the decay times ISO 3382-1 reads off it. See ADR-0357.

Pure functions of an impulse, its sample rate and the sample its sound
starts at. Each band's integral stops where its decay meets the noise, and
the energy the decay would have carried past that point is added back, so
a noise floor neither lengthens nor cuts a decay time (Lundeby, 1995).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal as scipy_signal

from .band_ladders import OCTAVE_BAND_CENTERS_HZ, OCTAVE_BANDS_HZ

#: ISO 3382-1's lowest octave.
LOWEST_CENTRE_HZ = 63.0
#: Each figure's evaluation range on the Schroeder curve, in dB (ISO 3382-1).
FIGURE_RANGES_DB = {"edt_s": (0.0, -10.0), "t20_s": (-5.0, -25.0), "t30_s": (-5.0, -35.0)}
#: A figure is read only when the band's noise sits at least this far below
#: its range's lower level (ISO 3382-1: T30 needs 45 dB of decay range).
NOISE_MARGIN_DB = 10.0
#: The band energy envelope's averaging span, used to find where decay meets noise.
ENVELOPE_MS = 10.0
#: The last fraction of the impulse whose mean energy is the noise floor.
NOISE_TAIL_FRACTION = 0.1
#: Order of each octave band's Butterworth low and high edges.
FILTER_ORDER = 3
#: The share of a band filter's energy its lead before the onset must hold.
FILTER_ENERGY_SHARE = 0.99


@dataclass(frozen=True)
class BandDecay:
    """One octave band's decay: its figures in seconds, ``None`` where the
    band's range above its noise cannot carry them."""

    centre_hz: float
    #: The band's envelope peak over its noise floor, in dB.
    decay_range_db: float | None
    #: Where the decay meets the noise, in ms after the sound starts.
    noise_crossing_ms: float | None
    edt_s: float | None
    t20_s: float | None
    t30_s: float | None
    #: The Schroeder curve, dB re its start, one value per sample up to the crossing.
    schroeder_db: np.ndarray


def octave_band(samples: np.ndarray, sample_rate: int, edges_hz: tuple[float, float]) -> tuple[np.ndarray, int]:
    """``samples`` through one band, and the band's lead in samples.

    The filter runs time-reversed, so its own ringing lands before a sound
    rather than in its decay; the lead is how far before a sound that
    ringing reaches, so an integral started that early holds the whole of it.
    """
    sos = scipy_signal.butter(FILTER_ORDER, edges_hz, btype="bandpass", fs=sample_rate, output="sos")
    probe = np.zeros(sample_rate // 2)
    probe[0] = 1.0
    spread = np.cumsum(scipy_signal.sosfilt(sos, probe) ** 2)
    lead = int(np.argmax(spread >= FILTER_ENERGY_SHARE * spread[-1]))
    return scipy_signal.sosfilt(sos, np.asarray(samples, dtype=np.float64)[::-1])[::-1], lead


def _slope_db_per_s(level_db: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """Least-squares line through ``level_db``: ``(slope dB/s, intercept dB)``."""
    t = np.arange(level_db.size) / sample_rate
    slope, intercept = np.polyfit(t, level_db, 1)
    return float(slope), float(intercept)


def band_decay(
    band: np.ndarray, sample_rate: int, *, start_index: int, centre_hz: float, lead: int,
) -> BandDecay:
    """One band's decay from ``start_index``, where the sound starts, integrated
    from ``lead`` samples before it."""
    first = max(0, start_index - lead)
    energy = np.asarray(band, dtype=np.float64)[first:] ** 2
    empty = BandDecay(centre_hz, None, None, None, None, None, np.empty(0))
    tail = max(1, int(round(NOISE_TAIL_FRACTION * energy.size)))
    noise = float(np.mean(energy[-tail:]))
    span = max(1, int(round(ENVELOPE_MS * sample_rate / 1000)))
    envelope = np.convolve(energy, np.ones(span) / span, mode="same")
    peak = int(np.argmax(envelope))
    if noise <= 0 or envelope[peak] <= noise:
        return empty
    range_db = float(10 * np.log10(envelope[peak] / noise))
    envelope_db = 10 * np.log10(np.maximum(envelope, 1e-30) / noise)
    # The late decay, the 20 dB above a point 10 dB over the noise, is the
    # line whose meeting with the noise ends the measured decay (Lundeby).
    decaying = envelope_db[peak:]
    knee = peak + int(np.argmax(decaying <= 10.0)) if np.any(decaying <= 10.0) else energy.size
    upper = peak + int(np.argmax(decaying <= 30.0)) if range_db > 30.0 else peak
    if knee - upper < 2:
        return BandDecay(centre_hz, range_db, None, None, None, None, np.empty(0))
    slope, intercept = _slope_db_per_s(envelope_db[upper:knee], sample_rate)
    if slope >= 0:
        return BandDecay(centre_hz, range_db, None, None, None, None, np.empty(0))
    crossing = min(energy.size - 1, upper + int(round(-intercept / slope * sample_rate)))
    tail_energy = noise * 10.0 / (-slope * np.log(10.0)) * sample_rate
    integral = np.cumsum(energy[:crossing + 1][::-1])[::-1] + tail_energy
    schroeder_db = 10 * np.log10(integral / integral[0])

    def figure(upper_db: float, lower_db: float) -> float | None:
        if range_db < -lower_db + NOISE_MARGIN_DB or schroeder_db[-1] > lower_db:
            return None
        first = int(np.argmax(schroeder_db <= upper_db))
        last = int(np.argmax(schroeder_db <= lower_db))
        if last - first < 2:
            return None
        fit, _ = _slope_db_per_s(schroeder_db[first:last + 1], sample_rate)
        return -60.0 / fit if fit < 0 else None

    return BandDecay(
        centre_hz, range_db, 1000.0 * (crossing - (start_index - first)) / sample_rate,
        **{name: figure(*levels) for name, levels in FIGURE_RANGES_DB.items()},
        schroeder_db=schroeder_db,
    )


def octave_decays(
    samples: np.ndarray, sample_rate: int, *, start_index: int, band_hz: tuple[float, float],
) -> tuple[BandDecay, ...]:
    """Every octave band from :data:`LOWEST_CENTRE_HZ` whose centre lies inside
    ``band_hz`` and whose top edge lies below Nyquist, read from ``start_index``."""
    decays = []
    for centre, edges in zip(OCTAVE_BAND_CENTERS_HZ, OCTAVE_BANDS_HZ):
        if LOWEST_CENTRE_HZ <= centre and band_hz[0] <= centre <= band_hz[1] and edges[1] < sample_rate / 2:
            band, lead = octave_band(samples, sample_rate, edges)
            decays.append(band_decay(band, sample_rate, start_index=start_index, centre_hz=centre, lead=lead))
    return tuple(decays)
