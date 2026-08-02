# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction envelope (Layer 1a driver linearization, #1668 PR-B).

Pure computation only: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:class:`jasper.audio_measurement.program_analysis.DriverResponse`. No I/O, no
product policy, no CamillaDSP/emission imports — this module answers "how
many dB of correction depth is this frequency bin allowed," nothing more.
This module shipped with no production callers; **it has one now** — #1668
PR-C wired it into the fit/apply flow, and
``jasper.active_speaker.crossover_v2_flow``'s ``_fit_linearization`` calls
:func:`compose_envelope` per driver role and hands the result to
:func:`~jasper.active_speaker.linearization_fit.fit_driver_linearization`,
whose filters are persisted and applied on hardware. Purity is still the
contract here; being uncalled is not. Read a change to any term as a change
to a live correction profile (see
docs/active-speaker-tuning-layers-design.md "Layer 1a concretely").

**Disambiguation — three unrelated "envelope" modules share vocabulary but
not concepts:**

* This module (:mod:`jasper.active_speaker.linearization_envelope`) — the
  correction envelope: ``allowed_depth(f) = min(term_1(f), …, term_n(f))``,
  a per-frequency DSP-fitting constraint.
* :mod:`jasper.correction.envelope` — a server-computed *screen* envelope
  for the room-correction wizard's dumb-frontend/smart-backend contract
  (what to draw on the /correction/ Room page).
* :mod:`jasper.active_speaker.crossover_envelope` and
  :mod:`jasper.active_speaker.crossover_envelope_v2` — pure *screen*
  envelopes for the crossover commissioning wizard flow (which step, which
  verdict text, which next action).

None of the other three compute a correction depth; do not confuse "screen
envelope" (UI state machine) with "correction envelope" (DSP constraint)
when grepping for "envelope" in this codebase.

See docs/active-speaker-tuning-layers-design.md "The correction envelope"
for the adopted design (2026-07-23) this module implements:

    allowed_depth(f) = min(
        mic_trust_limit(f, tier),
        repeatability_limit(f, sigma(f)),
        linearity_limit(f),
        invertibility_limit(f),
        class_prior_limit(f, class),
    )

and the sigma(f) reference implementation this mirrors:
``captures/xover-e0-2026-07-21/sigma-seeding-20260723/compute_sigma.py``
(session-artifact; see the ``REPORT.md`` beside it for the corpus findings
that seeded the tier/class tables below).

**Two further terms are optional and cloud-derived** (flat-linearization
productization plan, PR-6a; the plan's interpretation call (A)). When a
caller has a *spatial cloud* — several mic positions combined by
:func:`~jasper.audio_measurement.spatial_combine.combine_positions` — it may
hand :func:`compose_envelope` that cloud's honesty evidence, and the min
above gains:

    spatial_exclusion_limit(f, merged honesty intervals)
    position_stability_limit(f, cloud BandSpread, N)

Both are **narrowing only** — they enter the same ``np.min`` as everything
else, so an envelope with them can never allow more depth than the same
envelope without them. Both default to absent, and an absent term composes
to output byte-identical to before they existed. They sit on opposite sides
of the final smoothing pass, for a reason
:func:`compose_envelope` states in full: position stability is an ordinary
limit and is blended like one, while the exclusion is a hard verdict applied
to the smoothed result so it cannot bleed correction depth out of the
correctable response next to a null.
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
    it is. Snake_case values, domain-prefixed, mirroring the
    ``ReconstructionRefusal`` / ``BassExtensionRefusal`` house style so a
    logged/persisted value is self-identifying without a lookup table.

    ``FITTED`` and ``LIMITED_BY_VERIFY_DIVERGENCE`` are part of the closed
    vocabulary but are not produced by any function in THIS module yet:
    ``FITTED`` is emitted by :func:`compose_envelope` when no term actually
    binds a bin (see its docstring); ``LIMITED_BY_VERIFY_DIVERGENCE`` is
    reserved for the closed-loop verification feedback described in the
    design doc (build-order step 2, "closed-loop verify" — a later PR).
    Keeping it in the enum now means the vocabulary never needs a breaking
    rename once that PR lands.

    ``LIMITED_BY_SPATIAL_EXCLUSION`` and ``LIMITED_BY_POSITION_STABILITY``
    are produced by :func:`compose_envelope` only when a caller supplied the
    corresponding cloud evidence (``excluded_bands_hz`` / ``band_spread``);
    a per-driver session with no spatial cloud never sees either, and the
    matching terms are absent from :attr:`EnvelopeCurve.terms` entirely
    rather than present-but-inert.

    ``BEYOND_MEASUREMENT_CONFIDENCE`` is likewise vocabulary-only here:
    :func:`compose_envelope` never produces it. It is produced by the
    Layer-1a FIT layer's octave-band reason summary
    (:func:`jasper.active_speaker.linearization_fit.fit_driver_linearization`)
    for octave centers ABOVE the mic-trust confidence ceiling when the
    CD-horn continuation stage fired — the top octave's relative lift there
    is a declared-driver-type-informed continuation, not a measured claim,
    so it is disclosed as beyond-confidence rather than as any measured
    limit. Kept in this closed vocabulary (not the fit module) so every
    persisted reason code, wherever produced, stays self-identifying against
    the one enum.
    """

    FITTED = "envelope_fitted"
    LIMITED_BY_MIC_TIER = "envelope_limited_by_mic_tier"
    LIMITED_BY_REPEATABILITY = "envelope_limited_by_repeatability"
    LIMITED_BY_NONLINEARITY = "envelope_limited_by_nonlinearity"
    LIMITED_BY_EXCESS_PHASE = "envelope_limited_by_excess_phase"
    LIMITED_BY_CLASS_PRIOR = "envelope_limited_by_class_prior"
    LIMITED_BY_SPATIAL_EXCLUSION = "envelope_limited_by_spatial_exclusion"
    LIMITED_BY_POSITION_STABILITY = "envelope_limited_by_position_stability"
    LIMITED_BY_VERIFY_DIVERGENCE = "envelope_limited_by_verify_divergence"
    BEYOND_MEASUREMENT_CONFIDENCE = "envelope_beyond_measurement_confidence"
    OUT_OF_BAND = "envelope_out_of_band"


# Closed vocabularies (design doc "Microphone doctrine" / artifact 02 §5).
# `compose_envelope` and every term function that accepts a tier/class
# rejects anything outside these tuples — see `_validate_tier` /
# `_validate_driver_class`. DRIVER_CLASSES itself now lives in ._common
# (#1665 vocabulary hoist) and is re-exported here so this module's own
# callers/tests are unaffected.
MIC_TIERS: tuple[str, ...] = ("reference", "consumer", "phone")

# Shared working grid: 1/24-octave-scale log spacing, 150 Hz (the design
# doc's stated gated-measurement validity floor, "~143-200 Hz in the JTS3
# room") to 20 kHz. Deliberately NOT the same grid as compute_sigma.py's
# LOG_GRID_HZ (80 Hz-anchored, 185 points) — that script's floor predates
# this design doc's validity-floor contract; 176 points on a 150 Hz floor
# is this module's own choice, still >=4x finer than the ladder's finest
# step (1/6 oct), so it loses no ladder-relevant detail either.
DEFAULT_ENVELOPE_GRID_HZ: np.ndarray = np.geomspace(150.0, 20_000.0, 176)
# Frozen dataclasses (EnvelopeCurve) only stop reassigning FIELDS -- a
# `freqs_hz` field that defaults to this exact array object still holds a
# live reference to it, so an in-place mutation by one caller would
# silently corrupt the grid for every other caller. Read-only closes that.
DEFAULT_ENVELOPE_GRID_HZ.flags.writeable = False

# Every term function below is capped at this value. It is NOT a policy
# number — the real ceiling on what fitting is allowed to do lives in
# PR-C's cut/boost caps (-12 dB cut / +6 dB boost, design doc "Correction
# is clamped to the envelope..."). Any sentinel value strictly above those
# caps is behaviorally equivalent: its only job is to lose the min() to
# whichever real term actually constrains a bin, never to constrain
# anything itself. 24.0 was picked as "comfortably above 12" with room to
# spare, nothing more.
ENVELOPE_CEILING_SENTINEL_DB: float = 24.0

# sigma_tolerable(tier), dB — design doc "Cold-start priors" / REPORT.md
# finding 5's seed values. The corpus measured 1-2 orders of magnitude
# BELOW these on a single rig/room/night (REPORT.md finding 1), so they
# are validated as generous floors, not tightened.
_SIGMA_TOLERABLE_DB: Mapping[str, float] = {
    "reference": 0.5,
    "consumer": 1.0,
    "phone": 1.5,
}

# mic_trust_limit's (full_to_hz, taper_zero_hz) by tier — design doc
# "Cold-start priors: artifact 01's per-tier table". THIS is the
# design-doc-canonical table, not artifact 01's separate fit/verify
# ceiling pair (two different tables exist in the research artifacts;
# this module implements only the one the adopted design doc states).
_MIC_TRUST_TABLE_HZ: Mapping[str, tuple[float, float]] = {
    "reference": (8_000.0, 16_000.0),
    "consumer": (6_000.0, 12_000.0),
    "phone": (3_000.0, 8_000.0),
}

# class_prior_limit's full_to_hz by driver class — artifact 02 §5's
# driver-class table, bottom row ("unknown" = one class more conservative
# than "consumer-tier" mic trust, i.e. below even the phone-tier mic
# taper's own full_to). taper_zero is DERIVED as full_to * 2 (see
# `class_prior_limit`'s docstring for why this specific multiplier is a
# heuristic, not a researched value).
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

    Mirrors
    ``captures/xover-e0-2026-07-21/sigma-seeding-20260723/compute_sigma.py``'s
    "centered + smoothed" treatment (REPORT.md's headline numbers) with one
    deliberate order difference: this function smooths each occurrence
    individually, THEN centers each smoothed curve to its own mean over
    ``valid_band_hz`` — the reference script centers the RAW curve first,
    then smooths the centered curve. The two orders are mathematically
    equivalent for a FIXED centering constant (subtracting a per-curve dB
    offset is a linear rescale in power domain, and the ladder smoother is
    a linear box-average in power domain, so "scale then average" equals
    "average then scale"); they differ only in which curve's masked mean
    supplies that constant (smoothed vs. raw), a difference that is
    negligible for a response this smooth across a multi-octave valid
    band. This order is the one specified for this module; see
    REPORT.md's methodology note for the reference script's own raw/
    centered distinction (methodology parity, not a re-derivation).

    Returns ``None`` when fewer than 2 occurrences are available (a
    session whose driver never repeated) — no evidence, no sigma, never a
    guess.

    Also returns ``None`` when ``valid_band_hz`` does not overlap
    ``grid_hz`` at all (an empty valid mask — e.g. a valid band that sits
    entirely below this module's 150 Hz floor grid). ``valid_band_hz`` is
    REQUIRED to overlap ``grid_hz`` for a non-``None`` result: centering
    needs at least one in-band bin to compute each occurrence's reference
    mean, and the empty-mask guard below exists so that requirement fails
    the same honest "no evidence" way as the occurrence-count guard above,
    rather than falling through to ``np.mean`` on an empty slice (which
    returns NaN with a RuntimeWarning, not an exception).
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
    # (n - 1) == 0 and returns NaN (with a RuntimeWarning), not an
    # exception. The `len(occurrences) < 2` check above is the ONLY thing
    # standing between a real N=1 capture and a silently-NaN envelope term
    # feeding min()/argmin() downstream — this is the single most
    # important correctness assertion in this module (see
    # tests/test_active_speaker_linearization_envelope.py's NaN-guard
    # test). Do not remove or weaken this guard.
    return np.std(stack, axis=0, ddof=1)


def _sigma_to_depth_db(sigma_db: np.ndarray, sigma_tolerable_db: float) -> np.ndarray:
    """``ceiling . min(1, sigma_tolerable / max(sigma, eps))`` — this module's
    ONE sigma-to-allowed-depth mapping.

    Saturates at the ceiling sentinel while sigma is small (a tight
    measurement earns no penalty) and tapers as ``1/sigma`` past
    ``sigma_tolerable`` (a loose one earns no permission to correct that
    deeply). The design doc's stated direction, REPORT.md finding 5: "a
    literal ``allowed_depth ∝ σ`` is backwards (noisier measurement must
    never justify deeper correction)".

    Two terms consume it — :func:`repeatability_limit` (in-position repeat
    spread) and :func:`position_stability_limit` (the standard error of a
    cross-position cloud's band level). They differ ONLY in which sigma they
    hand in; the mapping and the per-tier tolerance table
    (:data:`_SIGMA_TOLERABLE_DB`) are deliberately shared rather than
    near-copied, because a second formula meaning the same thing is a second
    formula to drift. Same instinct as
    :mod:`jasper.audio_measurement.interference_nulls`'s
    ``LADDER_ARRIVAL_TOLERANCE``, which is an *alias* of
    ``GEOMETRY_CLUSTER_TOLERANCE`` rather than a second number meaning the
    same thing — the aliasing pattern, not that constant's own width
    argument.

    **The tolerance table now has two consumers, which is a hazard as well as
    a saving.** A retune motivated by one term silently moves the other; see
    ``test_shared_sigma_tolerable_keeps_the_s0_worst_case_above_the_fit_cap``
    for the specific one that is guarded.
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
    """``D_cap(tier) . min(1, sigma_tolerable(tier) / max(sigma, eps))``.

    Saturates at the ceiling sentinel while sigma is small (tight
    repeatability earns no penalty), tapers toward 0 as sigma grows past
    ``sigma_tolerable`` (loose repeatability earns no permission to
    correct that deeply). This is the design doc's stated mapping
    direction (REPORT.md finding 5): "a literal ``allowed_depth ∝ σ`` is
    backwards (noisier measurement must never justify deeper correction)".

    ``sigma_db=None`` (fewer than 2 in-capture occurrences —
    :func:`compute_sigma_curve`'s contract) returns an ALL-ZERO array on
    ``grid_hz``. Absence of repeatability evidence is not "no
    constraint" — it is the tightest constraint: no measured repeat data
    means zero permission to correct until a session actually repeats the
    sweep. Never treat a missing sigma as an unconstrained pass-through.
    """
    _validate_tier(tier)
    if sigma_db is None:
        return np.zeros_like(grid_hz, dtype=np.float64)
    return _sigma_to_depth_db(sigma_db, _SIGMA_TOLERABLE_DB[tier])


def mic_trust_limit(freqs_hz: np.ndarray, *, tier: str) -> np.ndarray:
    """Flat at the ceiling sentinel up to the tier's ``full_to`` frequency,
    octave-linear taper to 0 at ``taper_zero``, 0 above.

    Table is the DESIGN-DOC-CANONICAL per-tier pair (reference 8 k -> 16 k,
    consumer 6 k -> 12 k, phone 3 k -> 8 k) — this is NOT artifact 01's
    separate fit/verify ceiling pair (the two research artifacts define a
    distinct table for "how far the fit may extend" vs. "how far VERIFY
    checks it"; only the design-doc table above is implemented here).
    Grepping the research artifacts for HF breakpoints will find a
    different-looking table — that one is not this one.
    """
    _validate_tier(tier)
    full_to_hz, taper_zero_hz = _MIC_TRUST_TABLE_HZ[tier]
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


def class_prior_limit(freqs_hz: np.ndarray, *, driver_class: str) -> np.ndarray:
    """Flat at the ceiling sentinel up to the driver class's ``full_to``
    frequency (artifact 02 §5's table), octave-linear taper to 0 at
    ``taper_zero = full_to * 2``, 0 above.

    The ``* 2`` (one octave) is a HEURISTIC, not a researched value —
    it was chosen only because the two rows of the design-doc's own
    mic-trust table that use a full octave (reference 8 k->16 k, consumer
    6 k->12 k) look like this shape; the third mic-trust row (phone,
    3 k->8 k) is actually ~1.4 octaves, so "matching the mic-table
    spacing" is approximate, not exact. Revisit with real per-class taper
    research before trusting this width in a boundary case.
    """
    _validate_driver_class(driver_class)
    full_to_hz = _CLASS_PRIOR_FULL_TO_HZ[driver_class]
    taper_zero_hz = full_to_hz * 2.0
    return _flat_then_taper(freqs_hz, full_to_hz, taper_zero_hz)


def linearity_limit(freqs_hz: np.ndarray) -> np.ndarray:
    """STUB — design doc build-order item 5 ("multi-level linearity": a
    two-level test extending existing pilots). Returns the ceiling
    sentinel everywhere, i.e. imposes no additional constraint yet.

    Returns the finite sentinel rather than ``np.inf`` so composition's
    ``min()`` AND its ``argmin`` reason-code selection both stay
    well-defined while this term is unimplemented: ``inf`` would never
    win the min (harmless there), but mixing ``inf`` into a stack of
    otherwise-finite sentinel values is a footgun the moment any future
    caller compares, sorts, or serializes the raw term array. Every term
    in this module stays finite for that reason — do not change this to
    ``np.inf`` when implementing the real computation without re-checking
    every finite-value assumption downstream (:func:`compose_envelope`'s
    ``FITTED``-at-sentinel detection included).
    """
    return np.full_like(freqs_hz, ENVELOPE_CEILING_SENTINEL_DB, dtype=np.float64)


def invertibility_limit(freqs_hz: np.ndarray) -> np.ndarray:
    """STUB — design doc build-order item 6 ("excess-phase ADVISORY —
    build last"). Same finite-sentinel contract as :func:`linearity_limit`;
    see that docstring for why ``np.inf`` is wrong here.

    Excess-phase correction is explicitly advisory in the design doc:
    do not wire this to a real computation without a closed-loop
    achieved-vs-predicted verification landing first (design doc:
    "Evidence can EARN depth beyond the priors (clean measured excess
    phase + closed-loop verification passing...) but never beyond what
    the measurement chain resolves"). An excess-phase estimate without
    that verification backstop is exactly the kind of unverified model
    claim this whole envelope exists to bound.
    """
    return np.full_like(freqs_hz, ENVELOPE_CEILING_SENTINEL_DB, dtype=np.float64)


# --------------------------------------------------------------------------- #
# Cloud-derived terms (flat-linearization productization plan, PR-6a)
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
    :data:`DEFAULT_ENVELOPE_GRID_HZ` is 176 log-spaced bins, a 2.8354 % step
    — 245.1 Hz wide at 8644.8 Hz — while the intervals arriving from a cloud
    are computed on the combiner's own decimated analysis grid, 1.4648 Hz on
    the S0 corpus. A bin is therefore almost never exactly covered; it is
    *partly* covered, and the two available rules disagree by up to one bin
    at each end of each interval. (Measured 2026-07-26 on the S0 registry's
    three intervals, which is what that costs in practice: cell overlap
    excludes **17** envelope bins where point-containment would exclude
    **14** — one extra bin per interval, not two.)

    This function takes the conservative rule. Each bin owns the cell between
    the **geometric midpoints** to its two neighbours (log-domain, the same
    convention ``_ladder_smooth`` and the fit's ``trusted_mid_hz`` use; the
    first and last bins' outer edges mirror their own inner half-step), and
    the bin is excluded when that cell intersects an interval **at all**.
    The alternative — "the bin's own frequency lies inside the interval" —
    would leave a bin whose cell is 90 % inside an identified null fully
    correctable. The plan's non-goal is absolute ("No EQ of
    interference-flagged bins, ever"), so a partly-covered bin resolves
    toward exclusion. The cost is bounded at one grid bin of bandwidth at
    each end of each interval, permanently; the alternative's cost is EQ
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

    ``excluded_bands_hz`` is the plan's **merged honesty mask** as frequency
    intervals: the union of the combiner's power-vs-median screen
    (:attr:`~jasper.audio_measurement.spatial_combine.CombinedResponse.excluded_bands_hz`)
    and the identified-null registry
    (:attr:`~jasper.audio_measurement.interference_nulls.InterferenceNullReport.excluded_bands_hz`).
    Intervals rather than a bin mask because the two producers live on the
    combiner's analysis grid and this module lives on its own envelope grid:
    an interval is grid-independent, and
    :func:`~jasper.audio_measurement.spatial_combine.merged_true_intervals`
    stays the single owner of the merge upstream. See :func:`_interval_mask`
    for the partial-coverage rule.

    The doctrine this encodes is one sentence of
    docs/flat-linearization-plan.md's non-goals — "No EQ of
    interference-flagged bins, ever; they are reported instead" — expressed
    as the only thing an envelope can say: zero permission. The fit then
    corrects the response *around* an identified null and never fills it.

    **Whose blind spot this is not.** The screen catches interference some
    positions see and others do not; the registry catches nulls every
    position sees. Neither is complete, which is why the plan's consumers
    take the union of both plus ``geometry.locked`` — this function receives
    that union already formed and does not itself know which instrument
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

    **Why the standard error and not the raw spread.** What the fit corrects
    is the cloud's *mean* — the power-mean curve — so the uncertainty that
    should bound correction depth is the uncertainty of that mean, which is
    the ``1/sqrt(N)`` law
    :class:`~jasper.audio_measurement.spatial_combine.BandSpread` names in
    its own docstring for exactly this statistic. Using the raw ``sigma_db``
    would punish a cloud for being well dispersed, and dispersing the mic is
    the protocol's own instruction (plan fundamental 1) — a term that bites
    harder the better the household follows the instructions is backwards.

    **Why ``sigma_db`` and not ``max_sigma_db``.** ``BandSpread`` draws the
    line and this function keeps it: ``sigma_db`` is the *level* spread the
    accuracy law is about, while ``max_sigma_db`` is the *structure* spread
    and "rides comb nulls on purpose". Comb nulls are already owned, twice
    and better, by the two instruments feeding
    :func:`spatial_exclusion_limit` — reading ``max_sigma_db`` here would
    re-litigate their evidence with a cruder statistic and double-count it.

    **The tolerance table is shared with** :func:`repeatability_limit`
    (:data:`_SIGMA_TOLERABLE_DB`, keyed by mic tier) because after the
    ``sqrt(N)`` division both terms are the same kind of quantity: how well
    is the level this fit is about to correct actually known, in dB. No
    second table, so no second table to drift.

    **Calibration — measured 2026-07-26 on the S0 corpus** (five cloud
    groupings of the 2026-07-25 JTS3 session, at ``reference`` tier;
    re-derived in full by
    ``test_s0_position_stability_calibration_populations``). Per-band
    ``sigma_db``, the ``sigma/sqrt(N)`` this function actually consumes, and
    the limit that produces — over the seven octave bands inside
    :data:`DEFAULT_ENVELOPE_GRID_HZ`'s 150 Hz support (250 Hz-16 kHz), all
    ranges min-max:

      grouping                      N    sigma_db      sigma/sqrt(N)  limit dB
      main leg, all 10 positions   10    0.946-3.088   0.299-0.976    12.29-24
      main leg, tweeter height      6    0.340-1.234   0.139-0.504    23.82-24
      main leg, a hand-width low    4    0.964-3.029   0.482-1.514     7.92-24
      desk front edge               3    0.306-1.735   0.177-1.002    11.98-24
      ground plane                  3    0.362-3.173   0.209-1.832     6.55-24

    Every grouping's tightest limit is its **16 kHz band** except the ground
    plane's, whose 8 kHz band is worse; the low bands sit at the sentinel
    throughout.

    **This term's own limit depends on N, not on the driver class** — it
    reads only the cloud and the mic tier, so the table above is
    class-independent. **What it does to a composed envelope is not.** Both
    regimes, measured end-to-end on the S0 replay and re-derived by
    ``test_s0_position_stability_narrows_the_envelope_but_not_the_fit``:

      declared class      bins narrowed   span            worst drop
      compression_horn    30              5082-11434 Hz   7.03 dB
      unknown             18              5082-8175 Hz    5.20 dB

    The ``unknown`` reading is smaller and stops lower because
    :func:`class_prior_limit` (``full_to`` 6 kHz) is already tighter than
    this term above ~8 kHz — the class prior gets there first, so there is
    less left for stability to take. A statement about what this term does
    in 8-16 kHz is therefore only true of the class it was measured under,
    which is the work order's rule and applies here too.

    **Under BOTH classes it changes zero bins** of the per-bin cut cap the
    fit actually consumes, ``min(12, allowed_depth)``, so the emitted filters
    come out byte-identical either way: on the plan's own cloud shape
    (fundamental 1's 8-12 positions — the ten-position main leg) the
    standard error stays at or under 0.976 dB and the tightest limit is
    **12.29 dB**, just above the fit's own 12 dB per-filter cut cap. A
    protocol-following cloud pays nothing; the term is holding a ceiling
    that is simply above where the fit was already bound. That 0.29 dB is
    the whole margin, and it is thin — a hardware-free test
    (``test_shared_sigma_tolerable_keeps_the_s0_worst_case_above_the_fit_cap``)
    pins it against a retune of the shared tolerance table, because the
    corpus test that measured it skips in CI.

    The thin and deliberately degenerate groupings are where the term bites
    — four positions at one height give 7.92 dB, and the three-position
    ground-plane leg 6.55 dB, both under that cap.

    There is **no measured negative population**: no S0 grouping produced a
    cloud whose positions disagree so badly that this term dominates the
    envelope, and this docstring does not pretend one exists. What the
    calibration establishes is the positive side — the readings a
    protocol-following cloud produces, that the term costs such a cloud
    nothing at the fit's own surface, and that its limit falls as N does.

    Bands the cloud did not report (outside its grid support, or under
    ``MIN_BAND_BINS``) leave their bins at the sentinel: **no reading, no
    additional constraint.** That is deliberately NOT the "no evidence, no
    permission" rule :func:`repeatability_limit` applies to a missing sigma,
    and the two cases are different. A missing repeat sigma means the
    repeatability leg of the doctrine has no evidence at all. A missing
    octave band means the cloud's grid did not reach it — a coverage fact
    about the capture, already bounded by the OUT_OF_BAND pre-mask, the mic
    tier and the class prior. Zeroing there would let a cloud that merely
    stopped at 19 kHz silently delete the 19-20 kHz envelope for a reason
    that has nothing to do with position stability.

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
    of ``freqs_hz`` — it names which term actually bound that bin before
    the final cliff-smoothing pass blends neighboring bins' numbers
    together (see :func:`compose_envelope`'s docstring for why the reason
    does not re-derive from the smoothed curve). ``terms`` holds every
    term's FULL, unmasked, per-bin curve (not just where it won) for
    diagnostics — e.g. showing "here's what mic-trust alone would have
    allowed at every frequency" even at bins some other term actually
    decided. Its KEYS are the terms that actually composed this curve: the
    five original ones always, plus ``LIMITED_BY_SPATIAL_EXCLUSION`` and/or
    ``LIMITED_BY_POSITION_STABILITY`` only when the caller supplied that
    cloud evidence — so a consumer reads the mapping to learn which
    instruments were available, and must not assume a fixed key set.
    ``sigma_db`` is the sigma actually CONSUMED by the
    repeatability term: by default that is :func:`compute_sigma_curve`'s
    output verbatim (``None`` when fewer than 2 occurrences existed); when
    a caller supplies ``sigma_db`` explicitly to :func:`compose_envelope`
    (the PR-C σ-composition seam — see that function's docstring), this
    field records exactly what was supplied instead of the internally
    computed value — the same diagnostic transparency, now for whichever
    σ evidence actually decided the term. ``n_repeats`` is independent of
    which σ source was used: it always reports ``primary``'s own
    occurrence count (primary + ``repeat_responses``), even when a
    caller-supplied ``sigma_db`` was composed from evidence outside this
    one driver's own capture (e.g. combined across a driver pair).
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


# Sentinel for compose_envelope's `sigma_db` parameter (review finding S1).
# `None` is already one of the three states the parameter distinguishes
# (explicit "no evidence"), so unsetness needs a value distinct from both
# `None` and any real array. Module-private: this sentinel's only job is
# to be checked with `is`, never compared for equality, logged, or handed
# back to a caller.
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
            linearity_limit(f),
            invertibility_limit(f),
            class_prior_limit(f, class),
        )

    with a hard OUT_OF_BAND pre-mask evaluated BEFORE the min, and a final
    ladder-smoothing pass so term handoffs (e.g. mic-trust's taper meeting
    class-prior's taper) have no audible cliffs.

    When a caller supplies **cloud evidence**, two further terms join that
    same min (flat-linearization productization plan, PR-6a; interpretation
    call (A)):

        spatial_exclusion_limit(f, excluded_bands_hz)
        position_stability_limit(f, band_spread, n_positions, tier)

    They are strictly narrowing — ``np.min`` cannot widen — so every claim
    about what an envelope allows above 8 kHz remains **class-regime
    dependent** exactly as it was: :func:`class_prior_limit` already zeroes
    an undeclared (``unknown``, ``full_to`` 6 kHz) driver by ~12 kHz before
    either new term exists, while a declared ``compression_horn``
    (``full_to`` 10 kHz) still has real authority at 8.7 kHz. A behavior
    measured under one declared class does not describe the other.

    ``excited_band_hz`` does double duty: it is both the pre-mask's
    excitation-coverage bound AND the ``valid_band_hz`` passed to
    :func:`compute_sigma_curve` for centering — the frequencies a driver's
    sweep actually excited are, by construction, the same band its
    repeatability centering should average over.

    The in-band region (bins where OUT_OF_BAND does NOT apply) is the
    intersection of ``excited_band_hz`` and
    ``[conservative_validity_floor_hz, grid_hz's own top]`` —
    ``conservative_validity_floor_hz`` is the HIGHEST (most restrictive)
    ``validity_floor_hz`` across every occurrence (primary + repeats),
    mirroring
    ``jasper.active_speaker.crossover_v2_flow._measure_validity_floor_hz``'s
    "worse (higher) of the...responses' own reflection-gate floor". Using
    the worst floor is conservative: a bin only counts as "validated" if
    it cleared EVERY occurrence's own reflection gate, not just one lucky
    occurrence's. An occurrence missing its floor entirely (``None`` —
    e.g. a near-field capture, per ``DriverResponse.validity_floor_hz``'s
    own docstring) is excluded from the max like
    ``_measure_validity_floor_hz`` does; if EVERY occurrence lacks a floor,
    the conservative floor is treated as +inf — no gating evidence
    anywhere means no in-band claim anywhere, the same "no evidence, no
    permission" doctrine as the ``sigma_db=None`` case in
    :func:`repeatability_limit`.

    A bin whose winning (minimum) term value equals the ceiling sentinel
    means NO term actually constrained it — every term returned "no
    limit" at that bin — so the honest reason is :attr:`ReasonCode.FITTED`
    ("free to be fitted up to the real fitting-time caps"), not whichever
    term happened to tie for the win there. Without this override, ties at
    the sentinel would resolve to the first-listed term
    (:attr:`ReasonCode.LIMITED_BY_MIC_TIER`) via ``argmin``'s
    first-index-wins tie-break, which would misreport perfectly ordinary
    unconstrained bins as "limited by mic tier."

    Reason codes are taken from the PRE-smoothing argmin: smoothing blends
    neighboring bins' NUMBERS (so term handoffs read smoothly), but "why
    is this bin limited" is still best answered by which term actually won
    at that exact bin before any blending, not a fiction re-derived from a
    blended curve. OUT_OF_BAND bins are hard-zeroed both before AND after
    the smoothing pass — the pre-mask exists to make a crisp claim
    boundary, and the smoothing ladder's window would otherwise leak a
    sliver of in-band energy across that boundary in either direction.

    **A term reaching exactly 0 is a hard boundary too (issue #1752).** Any
    bin where SOME term is exactly 0 composes to exactly 0, and the smoothing
    pass cannot blur depth back across it — the same treatment OUT_OF_BAND
    gets, for the same reason. An exact zero from a term is not a small
    number that should average with its neighbours; it is a trust statement
    (``mic_trust_limit`` at 0 above its tier's ``taper_zero`` means the
    calibrated microphone resolves nothing up there), and a statement of no
    trust cannot be softened by an adjacent bin that happens to have some.
    Before this rule the ladder leaked depth past every such zero: on the S0
    replay at ``reference`` tier, mic-trust is exactly 0 from 16444.9 Hz up
    yet the composed envelope carried 1.4846 dB at that bin and stayed
    non-zero to 18912.3 Hz, putting the fit band's top edge at 18390.9 Hz.
    It now ends at 15991.5 Hz, the last bin mic-trust actually trusts.

    This makes all terms consistent rather than leaving two regimes: the
    newer ``spatial_exclusion`` term already preserved its exact zeros, by
    composing AFTER the smooth (see the placement paragraph below). The two
    mechanisms differ in shape because they differ in what else they must
    protect — exclusion must not blur its zeros *outward* into the
    neighbourhood's depth, which is why it composes after rather than being
    hard-masked before — but they now agree that a zero means zero.

    Smoothing is otherwise untouched: it still smooths INSIDE the non-zero
    region, and the terms that reach zero do so through their own explicit
    octave-linear taper (:func:`_flat_then_taper`), so the soft handoff at a
    band edge is the term's own shape and never something the blur supplied.

    **The exclusion is applied AFTER the smoothing pass, not before, and
    that placement is load-bearing.** The composed curve is

        allowed_depth = min(
            ladder_smooth(min(every other term)),
            spatial_exclusion_limit(f),
        )

    so an excluded bin still reads exactly 0.0 (0 is the minimum), while a
    bin *outside* the mask reads exactly what it would have read with no
    mask supplied at all — bit for bit, pinned by a test. Zeroing the
    excluded bins BEFORE the smoothing pass instead (the treatment
    OUT_OF_BAND gets) would blur those zeros outward across a half-octave
    window and quietly cost the neighbourhood real correction depth.
    Measured 2026-07-26 by mutating that one rule and nothing else, on the
    S0 ten-position cloud at ``compression_horn`` (re-derived by
    ``test_s0_pre_smoothing_exclusion_would_have_cost_the_comb_peaks_real_depth``):
    18 in-band bins lose depth, worst **6.06 dB** — the comb peak at
    12786.4 Hz, sitting *between* the second and third identified nulls,
    would have fallen from 9.18 to 3.12 dB of allowed depth, and the one at
    10223.7 Hz from 15.23 to 11.09 dB. Those peaks are
    exactly what the registry sized its intervals to keep correctable
    (``IdentifiedNull.f_lo_hz``: half-depth width, "rather than the whole
    flank-to-flank span because the span includes the comb *peaks*, which
    are ordinary response the fit should still correct"), so pre-smoothing
    would have silently undone an upstream design decision.

    The asymmetry with OUT_OF_BAND is deliberate and the two cases differ:
    an out-of-band bin's term values are not *about* anything (no excitation
    reached it, no gate validated it), so letting them leak inward is
    letting noise in. A spatially-excluded bin's term values are perfectly
    ordinary; what is wrong with the bin is an interference verdict, and a
    verdict masks, it does not taper.

    Ties at the argmin resolve first-index-wins, as they always have. A bin
    that is BOTH spatially excluded and has no repeat evidence therefore
    reports ``LIMITED_BY_REPEATABILITY`` (the earlier term) even though
    ``LIMITED_BY_SPATIAL_EXCLUSION`` is equally true; both readings are
    honest, and no priority policy is invented here to pick between them.

    ``sigma_db`` is the seam the PR-C wiring layer uses to inject its own
    σ-composition POLICY instead of this module's default internal
    computation — the σ-seeding report's finding 5: ``σ_eff = max(
    σ_prior_floor, live)``, and "don't trust live σ alone until N>=3 for
    both drivers." That floor + N-trust gate belongs at the wiring
    boundary, not here — this pure core stays policy-free and only knows
    three states:

      * default (unset) — compute sigma(f) internally from ``primary``'s
        own in-capture repeats via :func:`compute_sigma_curve`, exactly
        today's behavior for every existing caller;
      * an explicit ``np.ndarray`` (must match ``grid_hz``'s shape) — use
        it VERBATIM as the repeatability evidence, bypassing internal
        computation entirely. This is how PR-C hands in its own already-
        floored, already-N-gated composed σ without this module needing
        to know what "floored" or "N-gated" mean;
      * explicit ``None`` — treat as NO repeatability evidence regardless
        of how many occurrences ``primary`` actually has (the same "no
        evidence, no permission" contract :func:`compute_sigma_curve`
        returns for <2 occurrences, but caller-FORCED rather than
        occurrence-count-derived — e.g. one driver of a pair has real
        repeats but its partner doesn't yet, so a paired N>=3 gate isn't
        satisfied for either).

    ``excluded_bands_hz`` / ``band_spread`` / ``n_positions`` are the cloud
    seam, and every one of them defaults to absent. ``None`` — and, for the
    two sequences, empty — means the corresponding term is **not added at
    all**: it does not appear in :attr:`EnvelopeCurve.terms`, contributes no
    reason code, and the returned curve is byte-identical to what this
    function produced before either term existed (pinned by a test).
    ``band_spread`` and ``n_positions`` must be supplied together, because a
    spread without its N cannot be turned into the standard error
    :func:`position_stability_limit` consumes and silently dropping the term
    would be a footgun.

    Raises :class:`ValueError` for a ``mic_tier`` or ``driver_class``
    outside the closed vocabularies (:data:`MIC_TIERS` /
    :data:`DRIVER_CLASSES`) — "unknown" (class) and "phone" (tier) are
    themselves valid, closed-vocabulary members (the most conservative
    ones), not error cases — for an explicit ``sigma_db`` array whose
    shape does not match ``grid_hz``, for exactly one of
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

    # Resolve the sigma_db tri-state contract (see docstring above): the
    # default sentinel computes internally, exactly as this function always
    # did before this parameter existed; an explicit array is trusted
    # verbatim (PR-C's composed/floored/N-gated σ); explicit None forces
    # "no evidence" regardless of primary's own occurrence count.
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

    # The five original terms keep their order, so an argmin tie among them
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
        EnvelopeTerm(ReasonCode.LIMITED_BY_NONLINEARITY, linearity_limit(grid_hz)),
        EnvelopeTerm(ReasonCode.LIMITED_BY_EXCESS_PHASE, invertibility_limit(grid_hz)),
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
    # OUT_OF_BAND (issue #1752). Every term here is non-negative, so the min
    # is 0 at a bin iff some term said 0 there -- and 0 from a term is not a
    # small number, it is a statement that this term extends no trust to this
    # bin at all. Without this mask the ladder window blurs neighbouring
    # in-band depth back across that boundary: measured on the S0 replay at
    # `reference` tier, where `mic_trust_limit` is exactly 0 from 16444.9 Hz
    # up, the composed envelope carried 1.4846 dB of allowed depth at that
    # very bin and stayed non-zero to 18912.3 Hz, putting the fit band's top
    # edge at 18390.9 Hz -- above the frequency the mic is trusted to resolve
    # anything at. `<= 0.0` rather than `np.isclose` is deliberate: the rule
    # is about an EXACT zero, and a bin holding a genuinely tiny but non-zero
    # permission should keep it rather than be rounded away.
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
