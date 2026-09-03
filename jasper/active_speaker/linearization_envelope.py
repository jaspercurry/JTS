# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction envelope: how many dB of correction depth each frequency
bin is allowed.

Pure computation only: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:class:`jasper.audio_measurement.program_analysis.DriverResponse`. No I/O, no
product policy, no CamillaDSP/emission imports.

It has live callers: ``crossover_v2.intervention``'s ``plan_linearization``
composes an envelope per driver role and hands it to
:func:`~jasper.active_speaker.linearization_fit.fit_driver_linearization`,
whose filters are persisted and applied on hardware. Read a change to any
term as a change to a live correction profile
(docs/active-speaker-tuning-layers-design.md "Layer 1a concretely").

Not to be confused with :mod:`jasper.correction.envelope` or
:mod:`jasper.active_speaker.crossover_envelope_v2`: those are *screen*
envelopes (wizard UI state), and none of them computes a correction depth.

See docs/active-speaker-tuning-layers-design.md "The correction envelope"
for the adopted design this module implements:

    allowed_depth(f) = min(
        mic_trust_limit(f, tier),
        repeatability_limit(f, sigma(f)),
        class_prior_limit(f, class),
    )

and the sigma(f) reference implementation this mirrors:
``captures/xover-e0-2026-07-21/sigma-seeding-20260723/compute_sigma.py``
(session-artifact; see the ``REPORT.md`` beside it for the corpus findings
that seeded the tier/class tables below).

Two further terms are optional and cloud-derived. When a caller has a
*spatial cloud* — several mic positions combined by
:func:`~jasper.audio_measurement.spatial_combine.combine_positions` — it may
hand :func:`compose_envelope` that cloud's honesty evidence, and the min
above gains:

    spatial_exclusion_limit(f, merged honesty intervals)
    position_stability_limit(f, cloud BandSpread, N)

Both are **narrowing only** — they enter the same ``np.min`` as everything
else, so an envelope with them can never allow more depth than the same
envelope without them. Both default to absent, and an absent term composes
to byte-identical output. They sit on opposite sides of the final smoothing
pass; :func:`compose_envelope` states why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.audio_measurement.spatial_combine import BandSpread

from ._common import DRIVER_CLASSES


class ReasonCode(StrEnum):
    """Per-bin honesty-guard vocabulary — why a bin's allowed depth is what
    it is. Snake_case, domain-prefixed values, so a logged or persisted code
    is self-identifying without a lookup table.

    The closed vocabulary is wider than what this module produces.
    ``LIMITED_BY_VERIFY_DIVERGENCE`` is reserved for the design doc's
    closed-loop verification feedback and nothing emits it yet.
    ``BEYOND_MEASUREMENT_CONFIDENCE`` is emitted by the FIT layer
    (:func:`jasper.active_speaker.linearization_fit.fit_driver_linearization`)
    for octave centers above the mic-trust confidence ceiling when the CD-horn
    continuation stage fired: that lift is a declared-driver-type-informed
    continuation, not a measured claim, so it is disclosed as
    beyond-confidence rather than as any measured limit. Both live here so
    every persisted reason code, wherever produced, reads against one enum.

    ``LIMITED_BY_SPATIAL_EXCLUSION`` and ``LIMITED_BY_POSITION_STABILITY``
    are produced by :func:`compose_envelope` only when a caller supplied the
    corresponding cloud evidence; without it the matching terms are absent
    from :attr:`EnvelopeCurve.terms` entirely, not present-but-inert.
    """

    FITTED = "envelope_fitted"
    LIMITED_BY_MIC_TIER = "envelope_limited_by_mic_tier"
    LIMITED_BY_REPEATABILITY = "envelope_limited_by_repeatability"
    LIMITED_BY_CLASS_PRIOR = "envelope_limited_by_class_prior"
    LIMITED_BY_SPATIAL_EXCLUSION = "envelope_limited_by_spatial_exclusion"
    LIMITED_BY_POSITION_STABILITY = "envelope_limited_by_position_stability"
    LIMITED_BY_VERIFY_DIVERGENCE = "envelope_limited_by_verify_divergence"
    BEYOND_MEASUREMENT_CONFIDENCE = "envelope_beyond_measurement_confidence"
    OUT_OF_BAND = "envelope_out_of_band"


# Closed vocabulary (design doc "Microphone doctrine"). `compose_envelope`
# and every term function taking a tier rejects anything outside this tuple.
MIC_TIERS: tuple[str, ...] = ("reference", "consumer", "phone")

# Shared working grid: log spacing from 150 Hz — the design doc's stated
# gated-measurement validity floor, "~143-200 Hz in the JTS3 room" — to
# 20 kHz. 176 points is >=4x finer than the smoothing ladder's finest step
# (1/6 oct), so it loses no ladder-relevant detail.
DEFAULT_ENVELOPE_GRID_HZ: np.ndarray = np.geomspace(150.0, 20_000.0, 176)
# Frozen dataclasses (EnvelopeCurve) only stop reassigning FIELDS -- a
# `freqs_hz` field that defaults to this exact array object still holds a
# live reference to it, so an in-place mutation by one caller would
# silently corrupt the grid for every other caller. Read-only closes that.
DEFAULT_ENVELOPE_GRID_HZ.flags.writeable = False

# dB. Every term function below is capped at this value. NOT a policy
# number: the real ceiling on what fitting may do is the wiring layer's
# cut/boost caps (-12 dB cut / +6 dB boost, design doc "Correction is clamped
# to the envelope..."). Any sentinel strictly above those caps is
# behaviorally equivalent — its only job is to lose the min() to whichever
# real term constrains a bin, never to constrain anything itself.
ENVELOPE_CEILING_SENTINEL_DB: float = 24.0

# sigma_tolerable(tier), dB — design doc "Cold-start priors" / the
# sigma-seeding REPORT.md finding 5. The corpus measured 1-2 orders of
# magnitude BELOW these, so they are validated as generous floors.
_SIGMA_TOLERABLE_DB: Mapping[str, float] = {
    "reference": 0.5,
    "consumer": 1.0,
    "phone": 1.5,
}

# mic_trust_limit's (full_to_hz, taper_zero_hz) by tier — design doc
# "Cold-start priors: artifact 01's per-tier table". THIS is the
# design-doc-canonical table, NOT artifact 01's separate fit/verify ceiling
# pair; the research artifacts hold both, and only this one is implemented.
_MIC_TRUST_TABLE_HZ: Mapping[str, tuple[float, float]] = {
    "reference": (12_000.0, 20_000.0),
    "consumer": (6_000.0, 12_000.0),
    "phone": (3_000.0, 8_000.0),
}

# class_prior_limit's full_to_hz by driver class, Hz — artifact 02 §5's
# driver-class table. "unknown" is one class more conservative than
# consumer-tier mic trust, i.e. below even the phone tier's own full_to.
# taper_zero is DERIVED as full_to * 2; see `class_prior_limit` for why that
# multiplier is a heuristic rather than a researched value.
_CLASS_PRIOR_FULL_TO_HZ: Mapping[str, float] = {
    "compression_horn": 10_000.0,
    "soft_dome": 14_000.0,
    "metal_dome": 16_000.0,
    "beryllium_diamond_dome": 17_000.0,
    "ribbon_amt": 17_000.0,
    "unknown": 6_000.0,
}


def _validate_tier(tier: str) -> None:
    if tier not in MIC_TIERS:
        raise ValueError(f"unknown mic tier {tier!r}; expected one of {MIC_TIERS}")


def _validate_driver_class(driver_class: str) -> None:
    if driver_class not in DRIVER_CLASSES:
        raise ValueError(
            f"unknown driver class {driver_class!r}; expected one of {DRIVER_CLASSES}"
        )


def _ladder_smooth(grid_hz: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    """The design doc's smoothing ladder: 1/6 oct below 4 kHz, 1/3 oct
    4-10 kHz, 1/2 oct at/above 10 kHz, hard-stitched via ``np.where`` —
    mirrors compute_sigma.py's ``ladder_smooth_loggrid``. Shared by
    :func:`compute_sigma_curve` and :func:`compose_envelope`'s final
    cliff-free smoothing pass.
    """
    fine = smooth_fractional_octave(grid_hz, magnitude_db, fraction=6)
    mid = smooth_fractional_octave(grid_hz, magnitude_db, fraction=3)
    coarse = smooth_fractional_octave(grid_hz, magnitude_db, fraction=2)
    return np.where(grid_hz < 4_000.0, fine, np.where(grid_hz < 10_000.0, mid, coarse))


def _flat_then_taper(
    freqs_hz: np.ndarray,
    full_to_hz: float,
    taper_zero_hz: float,
    *,
    sentinel_db: float = ENVELOPE_CEILING_SENTINEL_DB,
) -> np.ndarray:
    """Shared taper shape for :func:`mic_trust_limit` and
    :func:`class_prior_limit`: flat at ``sentinel_db`` up to
    ``full_to_hz``, octave-linear (linear in log2 f) taper down to 0 at
    ``taper_zero_hz``, 0 above. Same geometry, different per-tier /
    per-class breakpoints.
    """
    log2_f = np.log2(freqs_hz)
    log2_full_to = math.log2(full_to_hz)
    log2_taper_zero = math.log2(taper_zero_hz)
    span = log2_taper_zero - log2_full_to
    fraction = np.clip((log2_taper_zero - log2_f) / span, 0.0, 1.0)
    return sentinel_db * fraction


def compute_sigma_curve(
    primary: DriverResponse,
    *,
    valid_band_hz: tuple[float, float],
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray | None:
    """Repeatability spread sigma(f) across a driver's in-capture sweep
    occurrences (``primary`` plus its ``repeat_responses``), on ``grid_hz``.

    Each occurrence is smoothed, then centered to its own mean over
    ``valid_band_hz``; sigma is the ddof=1 standard deviation across those
    centered curves.

    Returns ``None`` when fewer than 2 occurrences are available (a session
    whose driver never repeated) — no evidence, no sigma, never a guess — and
    when ``valid_band_hz`` does not overlap ``grid_hz`` at all, e.g. a valid
    band entirely below this module's 150 Hz floor. Centering needs at least
    one in-band bin, and the empty-mask guard makes that requirement fail the
    same honest way rather than falling through to ``np.mean`` on an empty
    slice, which returns NaN with a RuntimeWarning, not an exception.
    """
    occurrences: tuple[DriverResponse, ...] = (primary, *primary.repeat_responses)
    if len(occurrences) < 2:
        return None

    lo_hz, hi_hz = valid_band_hz
    valid_mask = (grid_hz >= lo_hz) & (grid_hz <= hi_hz)
    if not valid_mask.any():
        return None

    centered_curves = []
    for occurrence in occurrences:
        resampled_db = np.interp(grid_hz, occurrence.freqs_hz, occurrence.magnitude_db)
        smoothed_db = _ladder_smooth(grid_hz, resampled_db)
        ref_db = float(np.mean(smoothed_db[valid_mask]))
        centered_curves.append(smoothed_db - ref_db)

    stack = np.stack(centered_curves)
    # GUARD: np.std(..., ddof=1) on a single row silently divides by
    # (n - 1) == 0 and returns NaN (with a RuntimeWarning), not an exception.
    # The `len(occurrences) < 2` check above is the ONLY thing standing
    # between a real N=1 capture and a silently-NaN envelope term feeding
    # min()/argmin() downstream. Do not remove or weaken it.
    return np.std(stack, axis=0, ddof=1)


def _sigma_to_depth_db(sigma_db: np.ndarray, sigma_tolerable_db: float) -> np.ndarray:
    """``ceiling . min(1, sigma_tolerable / max(sigma, eps))`` — this module's
    ONE sigma-to-allowed-depth mapping.

    Saturates at the ceiling sentinel while sigma is small (a tight
    measurement earns no penalty) and tapers as ``1/sigma`` past
    ``sigma_tolerable`` (a loose one earns no permission to correct that
    deeply). The design doc's stated direction: "a literal
    ``allowed_depth ∝ σ`` is backwards (noisier measurement must never
    justify deeper correction)".

    Two terms consume it — :func:`repeatability_limit` (in-position repeat
    spread) and :func:`position_stability_limit` (the standard error of a
    cross-position cloud's band level) — differing only in which sigma they
    hand in. The mapping and the per-tier tolerance table
    (:data:`_SIGMA_TOLERABLE_DB`) are shared rather than near-copied, which
    is a hazard as well as a saving: a retune motivated by one term silently
    moves the other.
    """
    epsilon_db = 1e-6
    return ENVELOPE_CEILING_SENTINEL_DB * np.minimum(
        1.0, sigma_tolerable_db / np.maximum(sigma_db, epsilon_db)
    )


def repeatability_limit(
    sigma_db: np.ndarray | None,
    *,
    tier: str,
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray:
    """``D_cap(tier) . min(1, sigma_tolerable(tier) / max(sigma, eps))`` on
    ``grid_hz`` — the shared mapping, see :func:`_sigma_to_depth_db`.

    ``sigma_db=None`` (fewer than 2 in-capture occurrences —
    :func:`compute_sigma_curve`'s contract) returns an ALL-ZERO array on
    ``grid_hz``. Absence of repeatability evidence is not "no constraint" —
    it is the tightest constraint: no measured repeat data means zero
    permission to correct until a session actually repeats the sweep. Never
    treat a missing sigma as an unconstrained pass-through.
    """
    _validate_tier(tier)
    if sigma_db is None:
        return np.zeros_like(grid_hz, dtype=np.float64)
    return _sigma_to_depth_db(sigma_db, _SIGMA_TOLERABLE_DB[tier])


def mic_trust_limit(freqs_hz: np.ndarray, *, tier: str) -> np.ndarray:
    """Flat at the ceiling sentinel up to the tier's ``full_to`` frequency,
    octave-linear taper to 0 at ``taper_zero``, 0 above.

    The pairs are :data:`_MIC_TRUST_TABLE_HZ`: reference 12 k -> 20 k,
    consumer 6 k -> 12 k, phone 3 k -> 8 k. That is the design-doc-canonical
    table, NOT artifact 01's separate fit/verify ceiling pair (the research
    artifacts define a distinct table for "how far the fit may extend" vs.
    "how far VERIFY checks it"; only the design-doc one is implemented here).
    Grepping the research artifacts for HF breakpoints finds a
    different-looking table — that one is not this one.
    """
    _validate_tier(tier)
    full_to_hz, taper_zero_hz = _MIC_TRUST_TABLE_HZ[tier]
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


def class_prior_limit(freqs_hz: np.ndarray, *, driver_class: str) -> np.ndarray:
    """Flat at the ceiling sentinel up to the driver class's ``full_to``
    frequency (artifact 02 §5's table), octave-linear taper to 0 at
    ``taper_zero = full_to * 2``, 0 above.

    The ``* 2`` (one octave) is a HEURISTIC, not a researched value: it was
    chosen to match the mic-trust rows' spacing, and only the consumer row
    (6 k->12 k) actually spans an octave — reference (12 k->20 k) is ~0.74
    and phone (3 k->8 k) ~1.4. Revisit with real per-class taper research
    before trusting this width in a boundary case.
    """
    _validate_driver_class(driver_class)
    full_to_hz = _CLASS_PRIOR_FULL_TO_HZ[driver_class]
    taper_zero_hz = full_to_hz * 2.0
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


# --------------------------------------------------------------------------- #
# Cloud-derived terms
#
# Both consume evidence a SPATIAL CLOUD produces and a single-position
# capture cannot. Both are optional, both only narrow, and both are absent
# unless the caller supplied their evidence.
# --------------------------------------------------------------------------- #


def _interval_mask(
    freqs_hz: np.ndarray, intervals: Sequence[tuple[float, float]],
) -> np.ndarray:
    """Rasterize frequency intervals onto ``freqs_hz`` — True where a bin's
    own cell overlaps any interval.

    **The edge rule is "any overlap excludes", and it needs stating because
    the envelope grid is coarse next to what it is rasterizing.**
    :data:`DEFAULT_ENVELOPE_GRID_HZ` steps ~2.84 % per bin, while the
    intervals arriving from a cloud are computed on the combiner's own
    decimated analysis grid, finer by three orders of magnitude. A bin is
    therefore almost never exactly covered; it is *partly* covered, and the
    two available rules disagree by up to one bin at each end of each
    interval.

    This function takes the conservative rule. Each bin owns the cell between
    the **geometric midpoints** to its two neighbours (log-domain, the same
    convention ``_ladder_smooth`` and the fit's ``trusted_mid_hz`` use; the
    first and last bins' outer edges mirror their own inner half-step), and
    the bin is excluded when that cell intersects an interval **at all**.
    The alternative — "the bin's own frequency lies inside the interval" —
    would leave a bin whose cell is 90 % inside an identified null fully
    correctable. The cost of erring this way is bounded at one grid bin of
    bandwidth at each end of each interval; the alternative's cost is EQ
    inside a null, which the whole instrument exists to prevent.

    Intervals need not be sorted or disjoint (overlapping ones simply union).
    They must each be ascending: a reversed ``(f_hi, f_lo)`` pair would
    intersect nothing and silently under-exclude — the unsafe direction — so
    it raises instead. That check runs before any early return, so a
    malformed interval is reported whatever grid it was handed.
    """
    for f_lo, f_hi in intervals:
        if f_hi < f_lo:
            raise ValueError(
                f"exclusion interval ({f_lo}, {f_hi}) is descending; intervals "
                "must be (f_lo, f_hi) with f_lo <= f_hi"
            )
    mask = np.zeros_like(freqs_hz, dtype=bool)
    if freqs_hz.size == 0 or not intervals:
        return mask

    log_f = np.log2(freqs_hz)
    cell_lo_log = np.empty_like(log_f)
    cell_hi_log = np.empty_like(log_f)
    if log_f.size == 1:
        # A one-bin grid has no neighbour to take a midpoint against; the
        # cell degenerates to the point itself, so "any overlap" reduces to
        # "the interval contains this frequency".
        cell_lo_log[0] = cell_hi_log[0] = log_f[0]
    else:
        midpoints = 0.5 * (log_f[:-1] + log_f[1:])
        cell_lo_log[1:] = midpoints
        cell_hi_log[:-1] = midpoints
        cell_lo_log[0] = log_f[0] - (midpoints[0] - log_f[0])
        cell_hi_log[-1] = log_f[-1] + (log_f[-1] - midpoints[-1])
    cell_lo = np.exp2(cell_lo_log)
    cell_hi = np.exp2(cell_hi_log)

    for f_lo, f_hi in intervals:
        mask |= (cell_lo <= f_hi) & (cell_hi >= f_lo)
    return mask


def spatial_exclusion_limit(
    freqs_hz: np.ndarray, excluded_bands_hz: Sequence[tuple[float, float]],
) -> np.ndarray:
    """Zero allowed depth on honesty-masked bins, the ceiling sentinel
    elsewhere.

    ``excluded_bands_hz`` is the **merged honesty mask** as frequency
    intervals: the union of the combiner's power-vs-median screen
    (:attr:`~jasper.audio_measurement.spatial_combine.CombinedResponse.excluded_bands_hz`)
    and the identified-null registry
    (:attr:`~jasper.audio_measurement.interference_nulls.InterferenceNullReport.excluded_bands_hz`).
    Intervals rather than a bin mask because the two producers live on the
    combiner's analysis grid and this module on its own: an interval is
    grid-independent, and
    :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`
    stays the single owner of the merge upstream. See :func:`_interval_mask`
    for the partial-coverage rule.

    The doctrine this encodes is one sentence of
    docs/historical/linearization-campaign-2026-07.md's non-goals — "No EQ of
    interference-flagged bins, ever; they are reported instead" — expressed
    as the only thing an envelope can say: zero permission. The fit then
    corrects the response *around* an identified null and never fills it.

    The screen catches interference some positions see and others do not;
    the registry catches nulls every position sees. Neither is complete,
    which is why callers take the union of both — this function receives that
    union already formed and does not itself know which instrument
    contributed which interval.
    """
    return np.where(
        _interval_mask(freqs_hz, excluded_bands_hz), 0.0, ENVELOPE_CEILING_SENTINEL_DB
    )


def position_stability_limit(
    freqs_hz: np.ndarray,
    band_spread: Sequence[BandSpread],
    *,
    n_positions: int,
    tier: str,
) -> np.ndarray:
    """Shrink allowed depth where the cloud's positions disagree about a
    band's level.

    The quantity handed to :func:`_sigma_to_depth_db` is the **standard error
    of the combined level**, ``sigma_db / sqrt(n_positions)``, per octave
    band.

    **The standard error, not the raw spread**, because what the fit corrects
    is the cloud's *mean*, so the bound belongs on the uncertainty of that
    mean — the ``1/sqrt(N)`` law
    :class:`~jasper.audio_measurement.spatial_combine.BandSpread` names for
    exactly this statistic. The raw ``sigma_db`` would punish a cloud for
    being well dispersed, which is the protocol's own instruction.

    **``sigma_db``, not ``max_sigma_db``**: the *level* spread the accuracy
    law is about, rather than the *structure* spread, which rides comb nulls
    on purpose. Comb nulls are already owned by the two instruments feeding
    :func:`spatial_exclusion_limit`.

    The tolerance table is shared with :func:`repeatability_limit`
    (:data:`_SIGMA_TOLERABLE_DB`, keyed by mic tier): after the ``sqrt(N)``
    division both terms are the same kind of quantity — how well the level
    this fit is about to correct is actually known, in dB.

    **Calibration margin.** On a protocol-following cloud (8-12 positions)
    the standard error stays at or under ~0.98 dB and the tightest limit is
    ~12.26 dB, just above the fit's own 12 dB per-filter cut cap, so such a
    cloud pays nothing at the fit's surface. That ~0.26 dB is the whole
    margin and it is thin: a retune of the shared tolerance table can close
    it. Thin or degenerate clouds are where the term bites — four positions
    at one height give ~7.9 dB, a three-position ground-plane leg ~6.6 dB,
    both under the cap. The limit depends on N and the mic tier, never on the
    driver class; what it does to a *composed* envelope does depend on the
    class, because :func:`class_prior_limit` may already be tighter above
    ~8 kHz.

    Bands the cloud did not report (outside its grid support, or under
    ``MIN_BAND_BINS``) leave their bins at the sentinel: **no reading, no
    additional constraint.** That is deliberately NOT the "no evidence, no
    permission" rule :func:`repeatability_limit` applies to a missing sigma.
    A missing repeat sigma means the repeatability leg has no evidence at
    all; a missing octave band is a coverage fact about the capture, already
    bounded by the OUT_OF_BAND pre-mask, the mic tier and the class prior.
    Zeroing there would let a cloud that merely stopped at 19 kHz silently
    delete the 19-20 kHz envelope for a reason that has nothing to do with
    position stability.

    Overlapping bands (the ISO octave edges very nearly abut, but clipping
    to the grid can make two bands share a bin) take the **larger** standard
    error, i.e. the tighter limit.

    Raises:
        ValueError: for a ``tier`` outside :data:`MIC_TIERS`, or
            ``n_positions < 2`` (a spread across fewer than two positions is
            undefined — ``combine_positions`` returns an empty
            ``band_spread`` there, and a caller passing a non-empty one with
            N < 2 has a bug worth hearing about).
    """
    _validate_tier(tier)
    if n_positions < 2:
        raise ValueError(
            f"n_positions must be >= 2 for a cross-position spread (got {n_positions})"
        )
    standard_error_db = np.zeros_like(freqs_hz, dtype=np.float64)
    root_n = math.sqrt(n_positions)
    for band in band_spread:
        band_mask = (freqs_hz >= band.f_lo) & (freqs_hz <= band.f_hi)
        standard_error_db[band_mask] = np.maximum(
            standard_error_db[band_mask], band.sigma_db / root_n
        )
    return _sigma_to_depth_db(standard_error_db, _SIGMA_TOLERABLE_DB[tier])


@dataclass(frozen=True)
class EnvelopeTerm:
    """One term's reason code paired with its full per-bin curve — the
    building block :func:`compose_envelope` stacks to find, per bin, the
    winning (minimum) term and its code.
    """

    code: ReasonCode
    depth_db: np.ndarray


@dataclass(frozen=True)
class EnvelopeCurve:
    """The composed correction envelope for one driver role in one session.

    ``reason`` is the PRE-smoothing argmin, one :class:`ReasonCode` per bin
    of ``freqs_hz``: it names which term actually bound that bin before the
    final cliff-smoothing pass blended neighbouring bins' numbers together.
    ``terms`` holds every term's FULL, unmasked per-bin curve — not just
    where it won — for diagnostics. Its KEYS are the terms that actually
    composed this curve: the three original ones always, plus
    ``LIMITED_BY_SPATIAL_EXCLUSION`` and/or
    ``LIMITED_BY_POSITION_STABILITY`` only when the caller supplied that
    cloud evidence, so a consumer must not assume a fixed key set.

    ``sigma_db`` is the sigma actually CONSUMED by the repeatability term:
    :func:`compute_sigma_curve`'s output verbatim by default (``None`` when
    fewer than 2 occurrences existed), or whatever a caller supplied to
    :func:`compose_envelope` instead. ``n_repeats`` is independent of that
    choice — it always reports ``primary``'s own occurrence count (primary +
    ``repeat_responses``), even when a supplied ``sigma_db`` came from
    evidence outside this one driver's capture.
    """

    role: str
    freqs_hz: np.ndarray
    allowed_depth_db: np.ndarray
    reason: tuple[ReasonCode, ...]
    terms: Mapping[ReasonCode, np.ndarray]
    sigma_db: np.ndarray | None
    n_repeats: int
    mic_tier: str
    driver_class: str


# Sentinel for compose_envelope's `sigma_db` parameter. `None` is already one
# of the three states that parameter distinguishes (explicit "no evidence"),
# so unsetness needs a value distinct from both `None` and any real array.
# Module-private: checked with `is`, never compared for equality, logged, or
# handed back to a caller.
_COMPUTE: object = object()


def compose_envelope(
    role: str,
    primary: DriverResponse,
    *,
    excited_band_hz: tuple[float, float],
    mic_tier: str,
    driver_class: str = "unknown",
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
    sigma_db: np.ndarray | None | object = _COMPUTE,
    excluded_bands_hz: Sequence[tuple[float, float]] | None = None,
    band_spread: Sequence[BandSpread] | None = None,
    n_positions: int | None = None,
) -> EnvelopeCurve:
    """Compose the correction envelope: the design doc's

        allowed_depth(f) = min(
            mic_trust_limit(f, tier),
            repeatability_limit(f, sigma(f)),
            class_prior_limit(f, class),
        )

    with a hard OUT_OF_BAND pre-mask evaluated BEFORE the min, and a final
    ladder-smoothing pass so term handoffs (e.g. mic-trust's taper meeting
    class-prior's taper) have no audible cliffs.

    When a caller supplies **cloud evidence**, two further terms join that
    same min:

        spatial_exclusion_limit(f, excluded_bands_hz)
        position_stability_limit(f, band_spread, n_positions, tier)

    They are strictly narrowing — ``np.min`` cannot widen — so every claim
    about what an envelope allows above 8 kHz stays **class-regime
    dependent**: :func:`class_prior_limit` already zeroes an undeclared
    (``unknown``, ``full_to`` 6 kHz) driver by ~12 kHz before either new term
    exists, while a declared ``compression_horn`` (``full_to`` 10 kHz) still
    has real authority at 8.7 kHz. A behavior measured under one declared
    class does not describe the other.

    ``excited_band_hz`` does double duty: it is both the pre-mask's
    excitation-coverage bound AND the ``valid_band_hz`` passed to
    :func:`compute_sigma_curve` for centering — the frequencies a driver's
    sweep actually excited are, by construction, the same band its
    repeatability centering should average over.

    The in-band region (bins where OUT_OF_BAND does NOT apply) is the
    intersection of ``excited_band_hz`` and
    ``[conservative_validity_floor_hz, grid_hz's own top]``, where
    ``conservative_validity_floor_hz`` is the HIGHEST (most restrictive)
    ``validity_floor_hz`` across every occurrence (primary + repeats). Using
    the worst floor is conservative: a bin counts as validated only if it
    cleared EVERY occurrence's own reflection gate. An occurrence missing its
    floor entirely (``None``, e.g. a near-field capture) is excluded from
    that max; if EVERY occurrence lacks a floor the conservative floor is
    +inf — no gating evidence anywhere means no in-band claim anywhere, the
    same "no evidence, no permission" doctrine as the ``sigma_db=None`` case
    in :func:`repeatability_limit`.

    A bin whose winning (minimum) term value equals the ceiling sentinel
    means NO term constrained it, so its reason is :attr:`ReasonCode.FITTED`
    — free to be fitted up to the real fitting-time caps — not whichever term
    tied for the win. Without the override, ``argmin``'s first-index-wins
    tie-break would misreport ordinary unconstrained bins as
    :attr:`ReasonCode.LIMITED_BY_MIC_TIER`.

    Reason codes are taken from the PRE-smoothing argmin: smoothing blends
    neighbouring bins' NUMBERS so term handoffs read smoothly, but "why is
    this bin limited" is answered by which term won at that exact bin, not by
    a fiction re-derived from a blended curve. OUT_OF_BAND bins are
    hard-zeroed both before AND after the smoothing pass, because the ladder's
    window would otherwise leak a sliver of in-band energy across that
    boundary in either direction.

    **A term reaching exactly 0 is a hard boundary too.** Any bin where SOME
    term is exactly 0 composes to exactly 0, and the smoothing pass cannot
    blur depth back across it: an exact zero is a statement of no trust
    (``mic_trust_limit`` at 0 above its tier's ``taper_zero`` means the
    calibrated microphone resolves nothing up there), not a small number to
    average with its neighbours. Smoothing is otherwise untouched — the terms
    that reach zero taper there through their own explicit octave-linear
    shape (:func:`_flat_then_taper`), never through the blur.

    **The exclusion is applied AFTER the smoothing pass, not before, and
    that placement is load-bearing.** The composed curve is

        allowed_depth = min(
            ladder_smooth(min(every other term)),
            spatial_exclusion_limit(f),
        )

    so an excluded bin still reads exactly 0.0 (0 is the minimum), while a
    bin *outside* the mask reads exactly what it would have read with no mask
    supplied at all, bit for bit. Zeroing the excluded bins BEFORE the
    smoothing pass instead (the treatment OUT_OF_BAND gets) would blur those
    zeros outward across a half-octave window and cost the neighbourhood real
    correction depth — including the comb peaks *between* identified nulls,
    which the null registry deliberately sizes its intervals to leave
    correctable. The asymmetry with OUT_OF_BAND is deliberate: an out-of-band
    bin's term values are not *about* anything, so letting them leak inward
    is letting noise in, whereas a spatially-excluded bin's values are
    perfectly ordinary and what is wrong with it is a verdict — and a verdict
    masks, it does not taper.

    Ties at the argmin resolve first-index-wins. A bin that is BOTH spatially
    excluded and has no repeat evidence therefore reports
    ``LIMITED_BY_REPEATABILITY`` (the earlier term) even though
    ``LIMITED_BY_SPATIAL_EXCLUSION`` is equally true; both readings are
    honest, and no priority policy is invented here to pick between them.

    ``sigma_db`` is the seam a wiring layer uses to inject its own
    σ-composition POLICY (a prior floor, an N-trust gate). That policy belongs
    at the wiring boundary; this pure core knows three states:

      * default (unset) — compute sigma(f) internally from ``primary``'s own
        in-capture repeats via :func:`compute_sigma_curve`;
      * an explicit ``np.ndarray`` (must match ``grid_hz``'s shape) — used
        VERBATIM as the repeatability evidence, bypassing internal
        computation entirely, so a caller can hand in an already-floored,
        already-N-gated σ without this module knowing what either means;
      * explicit ``None`` — NO repeatability evidence regardless of how many
        occurrences ``primary`` has: the same "no evidence, no permission"
        contract :func:`compute_sigma_curve` returns for <2 occurrences, but
        caller-FORCED rather than occurrence-count-derived.

    ``excluded_bands_hz`` / ``band_spread`` / ``n_positions`` are the cloud
    seam and every one of them defaults to absent. ``None`` — and, for the
    two sequences, empty — means the corresponding term is **not added at
    all**: it does not appear in :attr:`EnvelopeCurve.terms`, contributes no
    reason code, and the returned curve is byte-identical to one composed
    without it. ``band_spread`` and ``n_positions`` must be supplied
    together, because a spread without its N cannot be turned into the
    standard error :func:`position_stability_limit` consumes.

    Raises :class:`ValueError` for a ``mic_tier`` or ``driver_class`` outside
    the closed vocabularies (:data:`MIC_TIERS` / :data:`DRIVER_CLASSES`) —
    "unknown" (class) and "phone" (tier) are valid members, the most
    conservative ones, not error cases — for an explicit ``sigma_db`` array
    whose shape does not match ``grid_hz``, for exactly one of
    ``band_spread``/``n_positions`` being supplied, or for a descending
    ``excluded_bands_hz`` interval.
    """
    _validate_tier(mic_tier)
    _validate_driver_class(driver_class)

    # Paired supply, resolved once into a single narrowed value so the term
    # construction below cannot reach a half-supplied pair.
    stability_evidence: tuple[Sequence[BandSpread], int] | None
    if band_spread is None and n_positions is None:
        stability_evidence = None
    elif band_spread is not None and n_positions is not None:
        stability_evidence = (band_spread, n_positions)
    else:
        raise ValueError(
            "band_spread and n_positions must be supplied together (got "
            f"band_spread={'a sequence' if band_spread is not None else None}, "
            f"n_positions={n_positions})"
        )

    occurrences: tuple[DriverResponse, ...] = (primary, *primary.repeat_responses)
    known_floors = [
        o.validity_floor_hz for o in occurrences if o.validity_floor_hz is not None
    ]
    conservative_floor_hz = max(known_floors) if known_floors else math.inf

    lo_hz, hi_hz = excited_band_hz
    excited_mask = (grid_hz >= lo_hz) & (grid_hz <= hi_hz)
    floor_mask = grid_hz >= conservative_floor_hz
    # Grid top is grid_hz's own maximum -- no separate upper check needed.
    in_band_mask = excited_mask & floor_mask

    # Resolve the sigma_db tri-state contract; see the docstring above.
    resolved_sigma_db: np.ndarray | None
    if sigma_db is _COMPUTE:
        resolved_sigma_db = compute_sigma_curve(
            primary, valid_band_hz=excited_band_hz, grid_hz=grid_hz
        )
    elif sigma_db is None:
        resolved_sigma_db = None
    elif isinstance(sigma_db, np.ndarray):
        resolved_sigma_db = sigma_db.astype(np.float64)
        if resolved_sigma_db.shape != grid_hz.shape:
            raise ValueError(
                f"sigma_db shape {resolved_sigma_db.shape} does not match "
                f"grid_hz shape {grid_hz.shape}"
            )
    else:
        raise TypeError(
            f"sigma_db must be an ndarray, None, or omitted; got {type(sigma_db)!r}"
        )

    # The three original terms keep their order, so an argmin tie among them
    # resolves exactly as it always did; the optional cloud terms append.
    # Position stability joins the SMOOTHED group -- it is an ordinary
    # per-band limit whose octave-band steps should be blended like any other
    # term handoff. Spatial exclusion does not; see below.
    smoothed_terms: list[EnvelopeTerm] = [
        EnvelopeTerm(ReasonCode.LIMITED_BY_MIC_TIER, mic_trust_limit(grid_hz, tier=mic_tier)),
        EnvelopeTerm(
            ReasonCode.LIMITED_BY_REPEATABILITY,
            repeatability_limit(resolved_sigma_db, tier=mic_tier, grid_hz=grid_hz),
        ),
        EnvelopeTerm(
            ReasonCode.LIMITED_BY_CLASS_PRIOR,
            class_prior_limit(grid_hz, driver_class=driver_class),
        ),
    ]
    if stability_evidence is not None and stability_evidence[0]:
        cloud_band_spread, cloud_n_positions = stability_evidence
        smoothed_terms.append(
            EnvelopeTerm(
                ReasonCode.LIMITED_BY_POSITION_STABILITY,
                position_stability_limit(
                    grid_hz,
                    cloud_band_spread,
                    n_positions=cloud_n_positions,
                    tier=mic_tier,
                ),
            )
        )

    term_specs: tuple[EnvelopeTerm, ...] = tuple(smoothed_terms)
    spatially_excluded_mask = np.zeros_like(grid_hz, dtype=bool)
    if excluded_bands_hz:
        spatial_term = EnvelopeTerm(
            ReasonCode.LIMITED_BY_SPATIAL_EXCLUSION,
            spatial_exclusion_limit(grid_hz, excluded_bands_hz),
        )
        # Read back off the term rather than rasterizing a second time: one
        # owner for "which bins did the honesty mask land on".
        spatially_excluded_mask = spatial_term.depth_db <= 0.0
        term_specs = (*term_specs, spatial_term)
    stacked = np.stack([term.depth_db for term in term_specs])
    winning_value = np.min(stacked, axis=0)
    winning_index = np.argmin(stacked, axis=0)
    codes_by_index = [term.code for term in term_specs]

    at_sentinel = np.isclose(winning_value, ENVELOPE_CEILING_SENTINEL_DB)
    n_bins = len(grid_hz)
    pre_smoothing_reason = tuple(
        ReasonCode.FITTED if at_sentinel[i] else codes_by_index[int(winning_index[i])]
        for i in range(n_bins)
    )
    final_reason = tuple(
        ReasonCode.OUT_OF_BAND if not in_band_mask[i] else pre_smoothing_reason[i]
        for i in range(n_bins)
    )

    # The smoothing input is the min over the SMOOTHED terms only -- the
    # spatial exclusion is applied to the result, not blended into it. See
    # the docstring's "The exclusion is applied after the smoothing pass"
    # paragraph for why that placement is load-bearing rather than cosmetic.
    smoothable_value = np.min(
        np.stack([term.depth_db for term in smoothed_terms]), axis=0
    )
    # A term that reaches EXACTLY zero is a hard boundary, exactly like
    # OUT_OF_BAND. Every term here is non-negative, so the min is 0 at a bin
    # iff some term said 0 there -- and 0 from a term is not a small number,
    # it is a statement that this term extends no trust to this bin at all.
    # Without this mask the ladder window blurs neighbouring in-band depth
    # back across that boundary, putting the fit band's top edge above the
    # frequency the mic is trusted to resolve anything at. `<= 0.0` rather
    # than `np.isclose` is deliberate: the rule is about an EXACT zero, and a
    # bin holding a genuinely tiny but non-zero permission keeps it rather
    # than being rounded away.
    hard_zero_mask = smoothable_value <= 0.0
    masked_depth_db = np.where(in_band_mask, smoothable_value, 0.0)
    smoothed_depth_db = _ladder_smooth(grid_hz, masked_depth_db)
    smoothed_depth_db = np.where(
        in_band_mask & ~spatially_excluded_mask & ~hard_zero_mask,
        smoothed_depth_db,
        0.0,
    )

    terms_map: Mapping[ReasonCode, np.ndarray] = {
        term.code: term.depth_db for term in term_specs
    }

    return EnvelopeCurve(
        role=role,
        freqs_hz=grid_hz,
        allowed_depth_db=smoothed_depth_db,
        reason=final_reason,
        terms=terms_map,
        sigma_db=resolved_sigma_db,
        n_repeats=len(occurrences) - 1,
        mic_tier=mic_tier,
        driver_class=driver_class,
    )
