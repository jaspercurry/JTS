#!/usr/bin/env python3
"""The time window every score in this folder is read through, and the score.

The window is ``coh_check_windowed.py``'s, unchanged: 4 ms before the marker to
``T`` ms after it, a 2 ms raised-cosine rise, and a falling half-Hann over the
last third. It exists because the ROOM, not the loudspeaker, caps what an
ungated level behind the cabinet can show: the same front/rear pair that bounds
a flat gain+delay null at -32..-38 dB inside 6 ms bounds it at only -3..-4 dB
ungated.

The marker is NOT the take's alignment anchor. On the side microphone that
anchor reads 0.01-0.3 confidence and jumps 13 ms between takes. It is instead
the peak of the 1-4 kHz band-limited envelope of the take's own impulse
response: the rear chain is low-passed at 300 Hz, so that band carries only the
front of the speaker and does not move when the rear candidate changes.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import hilbert

from jasper.audio_measurement.rear_evidence import band_limited_impulse

SAMPLE_RATE_HZ = 48000
#: Window lengths past the marker, ms. 6 ms is the direct sound, 25 ms is
#: already most of the way to what ungated shows.
WINDOW_MS = (6.0, 12.0, 25.0)
PRE_MS = 4.0
RISE_MS = 2.0
#: Where the marker is read. Above the rear branch's 300 Hz low-pass and below
#: the woofer's beaming, so every candidate of one take marks the same instant.
MARKER_BAND_HZ = (1000.0, 4000.0)
#: The band scored, and the 1/f weight that makes a linear frequency axis
#: count octaves evenly (``coh_check_windowed.bound``'s ``w``).
SCORE_BAND_HZ = (100.0, 350.0)


#: How far under the envelope maximum still counts as an arrival for the
#: ``first`` rule. 6 dB is half the amplitude.
FIRST_ARRIVAL_FLOOR_DB = 6.0
#: Two peaks closer than this are one arrival.
ARRIVAL_SEPARATION_MS = 1.0


def marker_sample(freqs_hz: np.ndarray, transfer: np.ndarray, *, rule: str = "peak") -> int:
    """This response's direct arrival, in :data:`MARKER_BAND_HZ`.

    ``peak`` is the loudest point of the band's envelope. IN FRONT of the
    speaker that is the direct arrival and the two rules agree to 0.01 dB.
    BEHIND the cabinet there is no direct 1-4 kHz path -- the band arrives by
    diffraction and by the room -- so the loudest point can be a room feature
    8 ms late, and ``peak`` then windows the room instead of the speaker.

    ``first`` is the EARLIEST envelope peak within
    :data:`FIRST_ARRIVAL_FLOOR_DB` of the maximum, which is the usual way to
    find an arrival that is not the loudest thing in the response.

    Both rules are candidate-invariant, which is the property the window needs:
    the rear chain is low-passed at 300 Hz, so no rear candidate moves this band.
    """
    envelope = np.abs(hilbert(band_limited_impulse(freqs_hz, transfer, MARKER_BAND_HZ)))
    if rule == "peak":
        return int(np.argmax(envelope))
    if rule != "first":
        raise ValueError(f"unknown marker rule {rule!r}")
    floor = float(np.max(envelope)) * 10.0 ** (-FIRST_ARRIVAL_FLOOR_DB / 20.0)
    loud = np.flatnonzero(envelope >= floor)
    if not loud.size:
        return int(np.argmax(envelope))
    span = int(ARRIVAL_SEPARATION_MS * 1e-3 * SAMPLE_RATE_HZ)
    start = int(loud[0])
    return start + int(np.argmax(envelope[start:start + span]))


def marker_usable(marker: int) -> bool:
    """Is there room for the window's 4 ms pre-roll before this marker?

    A marker inside the first :data:`PRE_MS` of the array makes the window
    start negative, and ``% length`` then wraps it onto the array's TAIL --
    a different part of the response entirely. That is a refusal, not a
    number to report: on the measured s1-gain round one side take marks at
    0.08 ms and would otherwise have been scored against the room.
    """
    return marker >= int(PRE_MS * 1e-3 * SAMPLE_RATE_HZ)


def window(marker: int, length: int, span_ms: float) -> np.ndarray:
    """``coh_check_windowed.py``'s window, verbatim, as an array of ``length``."""
    out = np.zeros(length)
    start = marker - int(PRE_MS * 1e-3 * SAMPLE_RATE_HZ)
    stop = marker + int(span_ms * 1e-3 * SAMPLE_RATE_HZ)
    index = np.arange(start, min(stop, start + length - 1)) % length
    taper = np.ones(index.size)
    rise = int(RISE_MS * 1e-3 * SAMPLE_RATE_HZ)
    taper[:rise] = 0.5 - 0.5 * np.cos(np.pi * np.arange(rise) / rise)
    fall = max(1, index.size // 3)
    taper[-fall:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(fall) / fall)
    out[index] = taper
    return out


def band_weight(n_fft: int) -> tuple[np.ndarray, np.ndarray]:
    """``(in-band bin mask, 1/f weight)`` on an ``n_fft`` rfft grid."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SAMPLE_RATE_HZ)
    inside = (freqs >= SCORE_BAND_HZ[0]) & (freqs <= SCORE_BAND_HZ[1])
    return inside, 1.0 / freqs[inside]


def windowed_energy(impulse: np.ndarray, marker: int, span_ms: float) -> float:
    """Weighted in-band energy of ``impulse`` seen through the window.

    One number per (response, window). A SCORE is the ratio of two of these,
    so whatever is common to both -- the microphone calibration, which is
    magnitude-only, and the absolute level -- divides out exactly.
    """
    shaped = np.asarray(impulse, dtype=float) * window(marker, impulse.size, span_ms)
    spectrum = np.fft.rfft(shaped)
    inside, weight = band_weight(shaped.size)
    return float(np.sum(weight * np.abs(spectrum[inside]) ** 2))


def score_db(candidate: float, reference: float) -> float:
    return 10.0 * np.log10(max(candidate, 1e-300) / max(reference, 1e-300))


def impulse_of(freqs_hz: np.ndarray, transfer: np.ndarray) -> np.ndarray:
    """The impulse response of a transfer sampled on a full rfft grid."""
    return np.fft.irfft(np.asarray(transfer), n=2 * (np.asarray(freqs_hz).size - 1))


def windowed_energies(freqs_hz: np.ndarray, transfer: np.ndarray,
                      marker: int | None = None, *,
                      rule: str = "peak") -> tuple[int, dict[str, float]]:
    """``(marker, {span: weighted energy})`` for one response.

    ``marker`` given re-uses another response's marker, which is what keeps a
    candidate and its rear-muted reference inside the SAME window.
    """
    impulse = impulse_of(freqs_hz, transfer)
    if marker is None:
        marker = marker_sample(freqs_hz, transfer, rule=rule)
    if not marker_usable(marker):
        return marker, {f"{span:g}": None for span in WINDOW_MS}
    return marker, {f"{span:g}": windowed_energy(impulse, marker, span)
                    for span in WINDOW_MS}
