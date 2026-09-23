# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Magnitude features and excess phase of gated impulse responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .analysis import smooth_fractional_octave
from .band_ladders import EXCESS_PHASE_NORMALISE_BAND_HZ
from .deconv import magnitude_response

#: Magnitude smoothing. 1/12 octave keeps a feature's own shape while coarse
#: enough that grid noise does not become one.
MAGNITUDE_SMOOTH_FRACTION = 12

#: Half-width of a feature's own band.
FEATURE_HALF_OCT = 1.0 / 12.0

#: Half-width of the neighbourhood a feature is read against, in octaves.
NEIGHBOURHOOD_OCT = 1.0 / 3.0

#: Complex smoothing for the phase chain, as a fraction of an octave. Finer
#: than the magnitude chain because excess phase is the quantity under test.
#: Smoothing magnitude and phase separately would break the pairing the whole
#: test depends on, which is why this is applied to the complex spectrum.
COMPLEX_SMOOTH_OCT = 1.0 / 24.0

#: Half-width of the span the group-delay slope is fitted over. The feature's
#: own half-width it is paired with is :data:`FEATURE_HALF_OCT`.
GD_SPAN_OCT = 1.0 / 24.0

#: Normalisation band, Hz. The trusted floor is derived from the gate length.
NORMALISE_BAND_HZ = EXCESS_PHASE_NORMALISE_BAND_HZ

#: Analysis grid: logarithmic, and dense enough that a 1/12-octave feature has
#: hundreds of samples across it at every frequency.
CLASSIFICATION_GRID_LO_HZ = 300.0
CLASSIFICATION_GRID_HI_HZ = 16000.0
CLASSIFICATION_GRID_POINTS = 12000

#: Zero-padded transform length for the phase chain. Interpolation, not
#: resolution: it gives the local slope fit samples to work with.
PHASE_NFFT = 1 << 16

#: Deep nulls clamp here, relative to the in-band maximum, before the cepstral
#: transform. Un-clamped, a single near-zero bin dominates the whole Hilbert
#: relation.
LOGMAG_FLOOR_DB = 60.0

#: Out-of-band log-magnitude is held at the nearest in-band value across this
#: crossfade. The cepstral transform is a Hilbert transform over the WHOLE
#: spectrum, so leaving the gate's low-frequency floor and the
#: deconvolution regulariser's roll-off raw injects a large fake
#: minimum-phase slope through the features. That holds for a capture, not
#: for magnitude a synthetic control puts down there deliberately — see
#: :func:`injection_excess_gd` and issue #3493.
EDGE_LO_HZ = 250.0
EDGE_HI_HZ = 17000.0
EDGE_BLEND_OCT = 0.5


def gate(
    ir: np.ndarray,
    sample_rate: int,
    *,
    gate_ms: float,
    lead_ms: float = 0.0,
    peak: int | None = None,
) -> np.ndarray:
    """A FIXED-length window from the IR peak, optionally with a pre-peak lead.

    Deliberately not
    :func:`~jasper.audio_measurement.gating.gate_impulse_response`, whose
    window is sized from a DETECTED reflection: the length here is the
    caller's, so a control's known answer and the capture it is injected into
    are read through the identical window. Same shape family — flat to the
    peak, decaying half-Hann after it, raised-cosine fade into any lead.

    This is the PRIMARY and phase window only. The window LADDER has its own
    shape (:func:`~jasper.active_speaker.crossover_v2.gate_sweep.gated_segment`); the two are not
    interchangeable and their numbers are not comparable (P1 sec 6, rows D
    and F).
    """
    if peak is None:
        peak = int(np.argmax(np.abs(ir)))
    span = int(round(gate_ms * 1e-3 * sample_rate))
    lead = int(round(lead_ms * 1e-3 * sample_rate))
    start = max(0, peak - lead)
    lead = peak - start
    segment = np.asarray(ir[start:start + lead + span], dtype=np.float64)
    if segment.size < lead + span:
        segment = np.pad(segment, (0, lead + span - segment.size))
    window = np.ones(segment.size)
    window[lead:] = np.hanning(span * 2)[span:]
    if lead:
        window[:lead] = np.hanning(lead * 2)[:lead]
    return segment * window


def classification_grid() -> np.ndarray:
    lo, hi = CLASSIFICATION_GRID_LO_HZ, CLASSIFICATION_GRID_HI_HZ
    return np.geomspace(lo, hi, CLASSIFICATION_GRID_POINTS)


def smoothed_curve(
    segment: np.ndarray, sample_rate: int, grid: np.ndarray
) -> np.ndarray:
    """Gated segment to a normalised fractional-octave dB curve on ``grid``.

    Smoothing is the product's own power-mean
    (:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`), run
    on the linear rfft grid it expects and then interpolated onto the log
    grid. The lab harness used an arithmetic dB mean of its own, so these
    numbers are this instrument's, not a reproduction of the night's.
    """
    freqs, db = magnitude_response(segment.astype(np.float32), sample_rate)
    lo, hi = CLASSIFICATION_GRID_LO_HZ * 0.8, CLASSIFICATION_GRID_HI_HZ * 1.2
    keep = np.isfinite(db) & (freqs >= lo) & (freqs <= hi)
    smoothed = smooth_fractional_octave(
        freqs[keep], db[keep], MAGNITUDE_SMOOTH_FRACTION
    )
    curve = np.interp(grid, freqs[keep], smoothed)
    band = (grid >= NORMALISE_BAND_HZ[0]) & (grid <= NORMALISE_BAND_HZ[1])
    return curve - float(np.median(curve[band]))


def _hold_band_edges(
    freqs: np.ndarray, logmag: np.ndarray,
    edge_band_hz: tuple[float, float] = (EDGE_LO_HZ, EDGE_HI_HZ),
) -> np.ndarray:
    """Blend out-of-band log-magnitude to the nearest in-band value."""
    edge_lo, edge_hi = edge_band_hz
    out = logmag.copy()
    lo_ref = float(np.median(logmag[(freqs >= edge_lo) & (freqs <= edge_lo * 1.3)]))
    hi_ref = float(
        np.median(logmag[(freqs >= edge_hi / 1.3) & (freqs <= edge_hi)])
    )

    lo_start = edge_lo * 2 ** -EDGE_BLEND_OCT
    out[freqs <= lo_start] = lo_ref
    ramp = (freqs > lo_start) & (freqs < edge_lo)
    if ramp.any():
        t = np.log2(freqs[ramp] / lo_start) / EDGE_BLEND_OCT
        w = 0.5 - 0.5 * np.cos(np.pi * t)
        out[ramp] = (1 - w) * lo_ref + w * logmag[ramp]

    hi_end = edge_hi * 2 ** EDGE_BLEND_OCT
    out[freqs >= hi_end] = hi_ref
    ramp = (freqs > edge_hi) & (freqs < hi_end)
    if ramp.any():
        t = np.log2(freqs[ramp] / edge_hi) / EDGE_BLEND_OCT
        w = 0.5 - 0.5 * np.cos(np.pi * t)
        out[ramp] = (1 - w) * logmag[ramp] + w * hi_ref
    return out


def minimum_phase(logmag_half: np.ndarray, n_fft: int = PHASE_NFFT) -> np.ndarray:
    """Minimum phase, radians, from a half-spectrum natural-log magnitude.

    Real-cepstrum folding: ln|H| to cepstrum, zero the anticausal quefrencies
    and double the causal ones, back to frequency; the imaginary part is the
    minimum phase. Exact for a minimum-phase system.
    """
    full = np.concatenate([logmag_half, logmag_half[-2:0:-1]])
    if full.size != n_fft:
        raise ValueError(f"log-magnitude length {full.size} != n_fft {n_fft}")
    cepstrum = np.fft.ifft(full).real
    fold = np.zeros(n_fft)
    fold[0] = 1.0
    fold[n_fft // 2] = 1.0
    fold[1:n_fft // 2] = 2.0
    return np.fft.fft(cepstrum * fold).imag[: n_fft // 2 + 1]


def _complex_smooth(freqs: np.ndarray, spectrum: np.ndarray, frac: float) -> np.ndarray:
    """Running mean of a complex spectrum over a fractional-octave span.

    Only meaningful once bulk delay is removed — a rotating phasor averages
    towards zero — which :func:`excess_group_delay` guarantees before calling.
    """
    lo, hi = 2 ** (-frac / 2), 2 ** (frac / 2)
    csum = np.concatenate([[0.0 + 0j], np.cumsum(spectrum)])
    a = np.searchsorted(freqs, freqs * lo)
    b = np.maximum(np.searchsorted(freqs, freqs * hi), a + 1)
    return (csum[b] - csum[a]) / (b - a)


def _window_slopes(
    y: np.ndarray, lo_idx: np.ndarray, hi_idx: np.ndarray
) -> np.ndarray:
    """Least-squares slope of ``y`` against sample INDEX in each window.

    The transform grid is uniform, so the regressor is the index and the
    denominator ``n(n^2-1)/12`` is exact. Doing this by prefix sums rather
    than a Python loop is what keeps a run bounded: the in-band span is
    ~21 000 bins and the instrument fits a slope at every one of them,
    dozens of times per round.
    """
    n = y.size
    idx = np.arange(n, dtype=np.float64)
    cum_y = np.concatenate([[0.0], np.cumsum(y)])
    cum_iy = np.concatenate([[0.0], np.cumsum(idx * y)])
    count = (hi_idx - lo_idx).astype(np.float64)
    sum_y = cum_y[hi_idx] - cum_y[lo_idx]
    sum_iy = cum_iy[hi_idx] - cum_iy[lo_idx]
    mean_idx = (lo_idx + hi_idx - 1) / 2.0
    numerator = sum_iy - mean_idx * sum_y
    denominator = count * (count * count - 1.0) / 12.0
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = numerator / denominator
    return np.where(count >= _MIN_SLOPE_SAMPLES, slope, np.nan)


#: Fewest samples a local slope may be fitted through. Below this the fit is
#: reading two points and a rounding error.
_MIN_SLOPE_SAMPLES = 4


def local_group_delay_s(freqs: np.ndarray, phase: np.ndarray, sample_rate: int) -> np.ndarray:
    """Group delay, seconds, of an unwrapped phase on the :data:`PHASE_NFFT`
    transform grid: minus its local slope over +/- :data:`GD_SPAN_OCT` at every bin."""
    lo_idx = np.searchsorted(freqs, freqs * 2 ** -GD_SPAN_OCT)
    hi_idx = np.minimum(np.searchsorted(freqs, freqs * 2 ** GD_SPAN_OCT) + 1, freqs.size)
    return -_window_slopes(phase, lo_idx, hi_idx) / (2 * np.pi * sample_rate / PHASE_NFFT)


@dataclass(frozen=True)
class ExcessPhase:
    """One capture's excess phase, bulk time-of-flight removed."""

    freqs: np.ndarray
    excess_phase: np.ndarray
    excess_gd_us: np.ndarray
    bulk_delay_us: float


def excess_group_delay(
    segment: np.ndarray,
    sample_rate: int,
    *,
    trusted_band_hz: tuple[float, float],
    fit_band_hz: tuple[float, float] = (400.0, 12000.0),
    edge_band_hz: tuple[float, float] = (EDGE_LO_HZ, EDGE_HI_HZ),
) -> ExcessPhase:
    """Excess group delay of a gated response, in microseconds.

    In the order that matters: transform with heavy zero-padding; estimate
    and remove bulk time-of-flight as a linear phase fit, without which the
    complex smoothing that follows would average rotating phasors to
    nothing; complex-smooth; take minimum phase from the smoothed
    log-magnitude with the band edges held; subtract it and remove any
    residual linear trend; differentiate by a local linear fit.

    **What it cannot see, permanently.** Bulk delay removal cannot tell a
    smooth band-wide all-pass from metres of air, so a crossover's own global
    phase rotation is removed along with the time of flight. Only LOCALISED
    excess phase is detectable, so "minimum-phase" from this instrument means
    "no local excess phase at this feature", never "the whole response is
    minimum-phase".
    """
    spectrum = np.fft.rfft(np.asarray(segment, dtype=np.float64), n=PHASE_NFFT)
    freqs = np.fft.rfftfreq(PHASE_NFFT, d=1.0 / sample_rate)
    omega = 2 * np.pi * freqs

    fit = (freqs >= fit_band_hz[0]) & (freqs <= fit_band_hz[1])
    phase = np.unwrap(np.angle(spectrum))
    tau0 = -float(np.polyfit(omega[fit], phase[fit], 1)[0])
    rotated = spectrum * np.exp(1j * omega * tau0)

    first = max(1, int(np.searchsorted(freqs, edge_band_hz[0] * 2 ** -EDGE_BLEND_OCT / 2)))
    smoothed = rotated.copy()
    smoothed[first:] = _complex_smooth(
        freqs[first:], rotated[first:], COMPLEX_SMOOTH_OCT
    )

    in_band = (freqs >= trusted_band_hz[0]) & (freqs <= trusted_band_hz[1])
    magnitude = np.abs(smoothed)
    reference = float(np.max(magnitude[in_band])) if in_band.any() else 0.0
    magnitude = np.maximum(magnitude, reference * 10 ** (-LOGMAG_FLOOR_DB / 20))
    logmag = _hold_band_edges(freqs, np.log(magnitude), edge_band_hz)

    excess = np.unwrap(np.angle(smoothed)) - minimum_phase(logmag)
    residual = np.polyfit(omega[fit], excess[fit], 1)
    excess = excess - np.polyval(residual, omega)

    group_delay = local_group_delay_s(freqs, excess, sample_rate)
    group_delay[~in_band] = np.nan

    return ExcessPhase(
        freqs=freqs[in_band],
        excess_phase=excess[in_band],
        excess_gd_us=group_delay[in_band] * 1e6,
        bulk_delay_us=float((tau0 - residual[0]) * 1e6),
    )


def egd_excursion(
    ep: ExcessPhase, fc: float, trusted_band_hz: tuple[float, float]
) -> dict[str, Any]:
    """A feature's excess-GD excursion against its own neighbourhood.

    Two metrics, because one would miss half the physics. ``excursion_us`` is
    the SIGNED peak departure of the feature band from the neighbourhood
    median — a local detector, and a cancellation throws exactly that shape.
    ``p2p_us`` is peak-to-peak across the whole neighbourhood — a broad
    detector, because a gentle all-pass lifts the neighbourhood together and
    the local metric would read it as flat. ``clean`` is False when the
    neighbourhood runs off the trusted band.
    """
    freqs, gd = ep.freqs, ep.excess_gd_us
    finite = np.isfinite(gd)
    feature = (
        (freqs >= fc * 2 ** -FEATURE_HALF_OCT)
        & (freqs <= fc * 2 ** FEATURE_HALF_OCT)
        & finite
    )
    wide = (
        (freqs >= fc * 2 ** -NEIGHBOURHOOD_OCT)
        & (freqs <= fc * 2 ** NEIGHBOURHOOD_OCT)
        & finite
    )
    neighbourhood = wide & ~feature
    if not (feature.any() and neighbourhood.any()):
        return {
            "excursion_us": float("nan"),
            "p2p_us": float("nan"),
            "nbhd_median_us": float("nan"),
            "nbhd_sd_us": float("nan"),
            "at_hz": float("nan"),
            "clean": False,
        }
    base = float(np.median(gd[neighbourhood]))
    departure = gd[feature] - base
    peak = int(np.argmax(np.abs(departure)))
    return {
        "excursion_us": float(departure[peak]),
        "p2p_us": float(np.max(gd[wide]) - np.min(gd[wide])),
        "at_hz": float(freqs[feature][peak]),
        "nbhd_median_us": base,
        "nbhd_sd_us": float(np.std(gd[neighbourhood], ddof=1)),
        "clean": bool(
            feature.sum() > 8
            and neighbourhood.sum() > 16
            and fc * 2 ** -NEIGHBOURHOOD_OCT >= trusted_band_hz[0]
            and fc * 2 ** NEIGHBOURHOOD_OCT <= trusted_band_hz[1]
        ),
    }


def biquad_allpass(
    f0: float, q: float, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """RBJ 2nd-order all-pass: flat magnitude, pure excess phase.

    The cleanest possible non-minimum-phase control — it changes nothing a
    magnitude measurement can see, so anything the pipeline reports here is
    excess phase and nothing else.
    """
    w0 = 2 * np.pi * f0 / sample_rate
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 - alpha, -2 * np.cos(w0), 1 + alpha])
    a = np.array([1 + alpha, -2 * np.cos(w0), 1 - alpha])
    return b / a[0], a / a[0]


def add_delayed_copy(
    ir: np.ndarray, gain: float, delay_ms: float, sample_rate: int
) -> np.ndarray:
    """``h(t) + gain*h(t - delay)``.

    ``|gain| < 1`` keeps every zero INSIDE the unit circle, so the result is
    MINIMUM phase; only ``|gain| > 1`` is genuinely non-minimum-phase. That is
    not a quibble — it is the physics the whole classification turns on, and it
    is why the research spec's own ``-0.5`` interference case is a NEGATIVE
    control here.
    """
    shift = int(round(delay_ms * 1e-3 * sample_rate))
    out = ir.copy()
    out[shift:] += gain * ir[: ir.size - shift]
    return out


def tau_for_first_null_ms(fc: float) -> float:
    """Delay whose first two-path null lands on ``fc``.

    ``h(t) + g*h(t-tau)`` with ``g > 0`` nulls at ``(n + 1/2)/tau``, so the
    first rung sits at ``1/(2*tau)`` — the same ladder law the shipped
    interference-null instrument fits.
    """
    return 1e3 / (2.0 * fc)


def injection_excess_gd(
    gain: float,
    delay_ms: float,
    sample_rate: int,
    *,
    trusted_band_hz: tuple[float, float],
) -> ExcessPhase:
    """What this chain reports for a delayed-copy injection ON ITS OWN.

    ``|gain| < 1`` makes ``1 + g*z^-N`` minimum phase, so a correct reading
    is zero everywhere and whatever comes back is this chain's own artifact —
    which is what makes subtracting it sound rather than a rubber stamp.

    The artifact is :func:`_hold_band_edges`: it replaces the log-magnitude
    below :data:`EDGE_LO_HZ` with a constant while :func:`excess_group_delay`
    keeps the measured phase there. A NEGATIVE gain puts the comb's DC null
    inside that discarded band (-6.0 dB true against -3.1 dB held at
    ``g = -0.5``), and because the Hilbert relation is global the mismatch
    lands in the analysed band as excess GD rising roughly as 1/f^2 towards
    the low edge: 12.7 us at 465 Hz on a bare impulse, against C3's 10.0 us
    bar. C1 is unity at DC and C4's positive-gain combs sit on a broad DC
    maximum, so only C3 is touched. See issue #3493.

    Read through the identical chain, so the correction is made the way the
    reading is and goes to zero exactly when the hold does.
    """
    if abs(gain) >= 1.0:
        raise ValueError(
            f"injection gain {gain} is not minimum phase; its reading is "
            "signal, not artifact, and removing it would blind the control"
        )
    kernel = np.zeros(PHASE_NFFT)
    kernel[0] = 1.0
    return excess_group_delay(
        add_delayed_copy(kernel, gain, delay_ms, sample_rate),
        sample_rate,
        trusted_band_hz=trusted_band_hz,
    )


def _apply(ir: np.ndarray, coeffs: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    from scipy.signal import lfilter

    return np.asarray(lfilter(coeffs[0], coeffs[1], ir), dtype=np.float64)


def local_features(
    grid: np.ndarray, pooled: np.ndarray, *, lo_hz: float, hi_hz: float, feature_db: float
) -> list[tuple[int, int, int]]:
    """``(center_idx, lo_idx, hi_idx)`` for every local extremum of
    ``|pooled| >= feature_db`` inside ``[lo_hz, hi_hz]``, half-depth edges."""
    band = (grid >= lo_hz) & (grid <= hi_hz)
    idx = np.where(band)[0]
    found: list[tuple[int, int, int]] = []
    for k in range(1, len(idx) - 1):
        i = idx[k]
        v = pooled[i]
        if abs(v) < feature_db:
            continue
        if v > 0 and not (pooled[i] >= pooled[i - 1] and pooled[i] >= pooled[i + 1]):
            continue
        if v < 0 and not (pooled[i] <= pooled[i - 1] and pooled[i] <= pooled[i + 1]):
            continue
        half = abs(v) / 2.0
        a = i
        while a > idx[0] and abs(pooled[a]) > half and np.sign(pooled[a]) == np.sign(v):
            a -= 1
        b = i
        while b < idx[-1] and abs(pooled[b]) > half and np.sign(pooled[b]) == np.sign(v):
            b += 1
        found.append((i, a, b))
    merged: list[tuple[int, int, int]] = []
    for i, a, b in found:
        if merged and a <= merged[-1][2]:
            prev_i, prev_a, prev_b = merged[-1]
            if abs(pooled[i]) > abs(pooled[prev_i]):
                merged[-1] = (i, min(a, prev_a), max(b, prev_b))
            else:
                merged[-1] = (prev_i, min(a, prev_a), max(b, prev_b))
        else:
            merged.append((i, a, b))
    return merged
