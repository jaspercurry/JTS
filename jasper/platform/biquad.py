# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""RBJ biquad filters: the shared value types, the one response evaluator, and
the one headroom charge a peak costs.

Every DSP emitter, fitter and prediction speaks these types, and
:func:`biquad_coeffs` is the single model of what CamillaDSP realises from
them. Stdlib-only on purpose: socket-activated web surfaces and resident
daemons import it without NumPy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

# The rate every emitted biquad runs at. CamillaDSP's pipeline runs at it
# (``camilla_config_contract.DEFAULT_SAMPLE_RATE`` aliases this), so a response
# evaluated here is the response the speaker realises.
RESPONSE_SAMPLE_RATE_HZ = 48000
RESPONSE_NYQUIST_HZ = 0.5 * RESPONSE_SAMPLE_RATE_HZ


@dataclass(frozen=True)
class PeqFilter:
    """Import-cheap representation of a CamillaDSP peaking EQ."""

    freq: float
    q: float
    gain: float


def total_positive_boost_db(filters: Iterable[PeqFilter]) -> float:
    """The sum of positive gains across peaking filters, dB: a room layer's boost
    spend. Not the headroom it costs, which is the netted peak
    (:func:`peaking_cascade_peak_db`). Any object exposing a numeric ``.gain``
    is accepted — the designer's ``PEQ`` is structurally compatible with
    ``PeqFilter`` here.
    """
    return max(0.0, sum(f.gain for f in filters if f.gain > 0.0))


# Below the simplest |gain| a preference filter is considered "active" — a
# tiny shelf/peaking gain rounds to a no-op and is dropped before emission.
FILTER_EPSILON_DB = 0.05

# Cut/notch biquads shape the response without a user gain term. They are
# "active" by virtue of being enabled, not by a non-zero gain — see
# FilterSpec.active(). Highpass/Lowpass protect against rumble / tame top
# end; Notch is a surgical gain-less cut.
GAINLESS_BIQUAD_TYPES = frozenset({"Highpass", "Lowpass", "Notch"})
SHELF_BIQUAD_TYPES = frozenset({"Lowshelf", "Highshelf"})

# The ONE steepness every preference-EQ and linearization Lowshelf/Highshelf is
# both MODELLED at and EMITTED at: the Butterworth (non-resonant, no-overshoot)
# shelf Q.
#
# It is a single constant on purpose. No band in those domains carries a
# steepness field (FilterSpec.q is None for a shelf), so none is expressible
# there: a shelf emitted at any other Q would be a filter their evaluators
# cannot see. The evaluator :func:`biquad_coeffs` applies this Q to any shelf
# that declares no q, and honours one that does -- the rear calibration
# document (ADR-0318) is the one place that declares a shelf q, and ADR-0324's
# headroom charge must read it as CamillaDSP will.
#
# CamillaDSP's ``slope: 6.0`` is NOT Butterworth, despite reading like the
# familiar 6 dB/octave figure. CamillaDSP's advanced shelf takes S = slope/12
# and derives
#     Q = 1 / sqrt((A + 1/A) * (1/S - 1) + 2),   A = 10**(gain/40)
# (RBJ Audio EQ Cookbook; CamillaDSP src/filters/biquad.rs). Butterworth is
# S = 1, i.e. ``slope: 12`` -- pinned by CamillaDSP's own ``lowshelf_slope_vs_q``
# test, which asserts ``slope: 12.0`` and ``q: FRAC_1_SQRT_2`` produce the same
# coefficients. At ``slope: 6`` the realized Q collapses with gain (0.476 at
# -11 dB) and the realized curve missed the modelled one by up to 1.7 dB.
#
# Emitting ``q`` rather than ``slope: 12`` is deliberate: the number in the
# emitted YAML is then literally the number the evaluators use, and unlike
# ``slope`` its meaning does not depend on the band's gain.
#
# If a per-band shelf steepness is ever genuinely wanted, the MODEL must gain
# the parameter in the SAME change. A steepness the evaluators do not read is
# the bug this constant exists to prevent.
SHELF_Q: float = 1.0 / math.sqrt(2.0)

# Decimals used when spelling SHELF_Q into CamillaDSP YAML. The shared 4-decimal
# ``camilla_emit.fmt`` is right for Hz / dB / ms but leaves 0.7071 -- a 1e-5
# relative Q error, worth ~5e-5 dB of realized-vs-modelled mismatch. Seven
# decimals put the emitted filter within ~1.3e-7 dB of the model, i.e. inside
# the PEQ parity suite's 1e-6 dB tolerance, so "emitted == modelled" can be
# asserted as an equality rather than an approximation.
SHELF_Q_EMIT_DECIMALS = 7


@dataclass(frozen=True)
class FilterSpec:
    """A bounded CamillaDSP-friendly filter definition (preference EQ band).

    ``q`` carries the Q-parameterised types only (Peaking / Highpass / Lowpass /
    Notch). Shelves carry NO steepness field: every shelf is emitted and
    modelled at :data:`SHELF_Q` -- see that constant for why a per-band shelf
    steepness is deliberately not expressible here.
    """

    name: str
    biquad_type: str
    freq: float
    gain: float
    q: float | None = None

    def active(self) -> bool:
        if self.biquad_type in GAINLESS_BIQUAD_TYPES:
            return True
        return abs(self.gain) >= FILTER_EPSILON_DB


# The floor biquad_coeffs clamps q to, and the smallest Q
# jasper.audio_routes.camilla_emit.fmt's "%.4f" spells faithfully into CamillaDSP's YAML
# (below it the emitter writes "q: 0.0000", a document that fails at apply
# time). Below this floor an evaluated chain is not the filter that was
# asked for: the evaluator silently widens it and the emitter silently
# truncates it.
EVALUABLE_Q_MIN = 1e-4

# Above this Q, alpha = sin(w0)/(2Q) falls within ~8 orders of f64 epsilon of
# 1 in the Peaking numerator/denominator's "1 +/- alpha/amp", and the two
# stop cancelling symmetrically: measured +6.99 dB REALIZED from a requested
# Q 8e14 CUT (an admitted -3.0 dB), exact unity pole radius by Q 1e16. The
# ceiling keeps alpha/amp >= ~1e-8 across the audio band, so a cut's |H| <= 1
# stays true in the arithmetic this module actually does, not only in the
# algebra that assumes infinite precision.
EVALUABLE_Q_MAX = 1e6

# The frequency span every chain peak is taken over, and the one every
# per-driver prescribed filter must sit in, Hz: essentially DC to Nyquist. At its edges a
# Peaking cut at EVALUABLE_Q_MAX lifts f64 round-off to about -146 dBFS, under
# the 24-bit floor; at the extremes the emitter spells (1e-4 Hz,
# 23,999.9999 Hz), to about -28 dBFS (ADR-0367).
EVALUABLE_HZ_MIN = 1.0
EVALUABLE_HZ_MAX = 0.4999 * RESPONSE_SAMPLE_RATE_HZ


def biquad_coeffs(
    biquad_type: str, freq: float, gain_db: float, q: float | None,
    *, sample_rate_hz: float = RESPONSE_SAMPLE_RATE_HZ,
) -> tuple[float, float, float, float, float, float]:
    """RBJ Audio EQ Cookbook biquad coefficients (un-normalised).

    https://www.w3.org/TR/audio-eq-cookbook/ — the same digital biquad
    family CamillaDSP realises, so the magnitude we draw matches the
    speaker's actual output for every Q-parameterised type.

    ``q`` is the width the SPEC declares. ``None`` means it declares none, and
    the shape then falls back to the width the emitter writes for it: the fixed
    Butterworth ``SHELF_Q`` for a shelf, 1.0 elsewhere. A shelf that DOES declare a q is
    evaluated at it, because CamillaDSP honours the ``q`` field the graph
    carries — ``active_speaker.rear_calibration`` admits shelves up to q 1.0 and
    emits them verbatim, and reading one of those at ``SHELF_Q`` under-reports
    its corner peak by up to 0.78 dB per shelf.

    This MUST stay byte-for-byte equivalent to biquadCoeffs() in
    deploy/assets/sound-profile/js/eq-math.js for every input the /sound/ UI can
    produce. That UI has no shelf-steepness control (``FilterSpec.q`` is None
    for a shelf), so the explicit-shelf-q branch is unreachable from it and the
    JS twin does not carry it. Both are checked against
    tests/fixtures/peq_response_fixture.json.

    ``sample_rate_hz`` is the emitted graph's rate unless a caller filters a
    signal recorded at another.
    """
    w0 = 2.0 * math.pi * max(freq, 1e-6) / sample_rate_hz
    cw = math.cos(w0)
    sw = math.sin(w0)
    if q is None:
        q = SHELF_Q if biquad_type in SHELF_BIQUAD_TYPES else 1.0
    alpha = sw / (2.0 * max(q, EVALUABLE_Q_MIN))
    if biquad_type == "Lowpass":
        return ((1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    if biquad_type == "Highpass":
        return ((1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    if biquad_type == "Notch":
        return (1.0, -2 * cw, 1.0, 1 + alpha, -2 * cw, 1 - alpha)
    amp = 10.0 ** (gain_db / 40.0)
    if biquad_type == "Lowshelf":
        beta = 2.0 * math.sqrt(amp) * alpha
        return (
            amp * ((amp + 1) - (amp - 1) * cw + beta),
            2 * amp * ((amp - 1) - (amp + 1) * cw),
            amp * ((amp + 1) - (amp - 1) * cw - beta),
            (amp + 1) + (amp - 1) * cw + beta,
            -2 * ((amp - 1) + (amp + 1) * cw),
            (amp + 1) + (amp - 1) * cw - beta,
        )
    if biquad_type == "Highshelf":
        beta = 2.0 * math.sqrt(amp) * alpha
        return (
            amp * ((amp + 1) + (amp - 1) * cw + beta),
            -2 * amp * ((amp - 1) + (amp + 1) * cw),
            amp * ((amp + 1) + (amp - 1) * cw - beta),
            (amp + 1) - (amp - 1) * cw + beta,
            2 * ((amp - 1) - (amp + 1) * cw),
            (amp + 1) - (amp - 1) * cw - beta,
        )
    # Peaking (default).
    return (
        1 + alpha * amp,
        -2 * cw,
        1 - alpha * amp,
        1 + alpha / amp,
        -2 * cw,
        1 - alpha / amp,
    )


def freq_trig(freqs: Iterable[float]) -> list[tuple[float, float, float, float]]:
    """Per-frequency (cos ω, sin ω, cos 2ω, sin 2ω) at the response rate.

    Depends only on the frequency grid, not on any filter, so a summed
    response computes it once and reuses it across every band — the trig is
    the bulk of the per-point cost. Pass the result to filter_response_db.
    """
    table: list[tuple[float, float, float, float]] = []
    for freq in freqs:
        w = 2.0 * math.pi * max(float(freq), 1e-6) / RESPONSE_SAMPLE_RATE_HZ
        table.append((math.cos(w), math.sin(w), math.cos(2.0 * w), math.sin(2.0 * w)))
    return table


def filter_response_db(
    spec: FilterSpec,
    freqs: Iterable[float],
    trig: list[tuple[float, float, float, float]] | None = None,
) -> list[float]:
    """Magnitude response in dB of one biquad across ``freqs``.

    Evaluates |H(e^{jω})| of the RBJ biquad. Cascading is exact in dB
    (|H1·H2| = |H1|·|H2| ⇒ dB adds), so callers sum per-band results. Pass
    a shared ``trig`` table (from freq_trig) to avoid recomputing the
    per-frequency trig once per band in a multi-band sum.
    """
    b0, b1, b2, a0, a1, a2 = biquad_coeffs(
        spec.biquad_type, spec.freq, spec.gain, spec.q
    )
    if trig is None:
        trig = freq_trig(freqs)
    out: list[float] = []
    for c1, s1, c2, s2 in trig:
        num_re = b0 + b1 * c1 + b2 * c2
        num_im = -(b1 * s1 + b2 * s2)
        den_re = a0 + a1 * c1 + a2 * c2
        den_im = -(a1 * s1 + a2 * s2)
        num = num_re * num_re + num_im * num_im
        den = den_re * den_re + den_im * den_im
        out.append(10.0 * math.log10(max(num / den, 1e-12)) if den > 0.0 else 0.0)
    return out


def filter_response_complex(
    spec: FilterSpec,
    freqs: Iterable[float],
    trig: list[tuple[float, float, float, float]] | None = None,
) -> list[complex]:
    """Complex response H(e^{jω}) of one biquad across ``freqs``: the
    minimum-phase twin of :func:`filter_response_db`, whose magnitude it
    equals bin-for-bin.

    Keep the phase wherever a correction is applied to a branch that is then
    summed with another: a crossover sum is phase-dominated, so a zero-phase
    ``10**(db/20)`` scale mispredicts it. Apply in the linear domain,
    ``H = H * filter_response_complex(spec, freqs)``. Not floored like the
    magnitude twin, because flooring a complex value would break its phase.
    """
    coeffs = biquad_coeffs(spec.biquad_type, spec.freq, spec.gain, spec.q)
    return biquad_response_complex(coeffs, freq_trig(freqs) if trig is None else trig)


def biquad_response_complex(
    coeffs: tuple[float, float, float, float, float, float],
    trig: list[tuple[float, float, float, float]],
) -> list[complex]:
    """Complex response of raw ``(b0, b1, b2, a0, a1, a2)`` over a :func:`freq_trig` grid."""
    b0, b1, b2, a0, a1, a2 = coeffs
    out: list[complex] = []
    for c1, s1, c2, s2 in trig:
        num = complex(b0 + b1 * c1 + b2 * c2, -(b1 * s1 + b2 * s2))
        den = complex(a0 + a1 * c1 + a2 * c2, -(a1 * s1 + a2 * s2))
        out.append(num / den if den != 0 else complex(1.0, 0.0))
    return out


# A peak at or under this is unity, dB: it is left uncharged, and it is the
# verifier's slack on a charged peak. The emitter spells every gain, frequency
# and q to 4 decimals, so a graph charged exactly can read a hair above unity
# after the YAML round-trip; an analytic 0 dB reads about 1e-4.
PEAK_EPS_DB: float = 1e-3

# Added to a netted peak over PEAK_EPS_DB when charging headroom, dB (#1808): the
# evaluation grid's between-sample residue plus the emitter's 4-decimal YAML
# rounding. It equals the active graph's -1 dBFS limiter threshold
# (``camilla_yaml.BASELINE_LIMITER_CLIP_LIMIT_DB``).
HEADROOM_MARGIN_DB: float = 1.0

# Points per octave of the log grid a response is read on, before any filter's
# own frequencies are added to it.
RESPONSE_GRID_POINTS_PER_OCTAVE: int = 48


def headroom_charge_db(peak_db: float) -> float:
    """The attenuation a program peaking at ``peak_db`` needs, dB: 0.0 at or
    under :data:`PEAK_EPS_DB`, else the peak plus :data:`HEADROOM_MARGIN_DB`
    (ADR-0385)."""
    if peak_db <= PEAK_EPS_DB:
        return 0.0
    return float(peak_db) + HEADROOM_MARGIN_DB


def peaking_cascade_response_db(
    filters: Sequence[PeqFilter], low_hz: float, high_hz: float,
) -> tuple[list[float], list[float]]:
    """A series cascade of one or more Peaking ``filters`` from ``low_hz`` to
    ``high_hz``: its grid, and its summed response on that grid, dB.

    The grid is :data:`RESPONSE_GRID_POINTS_PER_OCTAVE` log-spaced points with
    both ends, plus each filter's own centre inside the span, where a lone
    filter reads its gain.
    """
    steps = max(1, math.ceil(RESPONSE_GRID_POINTS_PER_OCTAVE * math.log2(high_hz / low_hz)))
    grid = sorted({*(low_hz * (high_hz / low_hz) ** (step / steps) for step in range(steps + 1)),
                   *(f.freq for f in filters if low_hz <= f.freq <= high_hz)})
    trig = freq_trig(grid)
    return grid, [sum(values) for values in zip(*(
        filter_response_db(FilterSpec("room", "Peaking", f.freq, f.gain, f.q), grid, trig)
        for f in filters))]


def peaking_cascade_peak_db(filters: Sequence[PeqFilter]) -> float:
    """The netted peak of a series cascade of Peaking ``filters``, dB re unity:
    cuts net against boosts (ADR-0399). It is read across the evaluable span,
    widened to hold every filter's centre below Nyquist, so every centre is
    sampled and a lone boost reads its gain. A cascade with no boost never
    leaves unity, so it returns 0.0 and evaluates nothing.
    """
    if not any(f.gain > 0.0 for f in filters):
        return 0.0
    centres = [f.freq for f in filters if 0.0 < f.freq < RESPONSE_NYQUIST_HZ]
    return max(peaking_cascade_response_db(
        filters, min([EVALUABLE_HZ_MIN, *centres]), max([EVALUABLE_HZ_MAX, *centres]),
    )[1])
