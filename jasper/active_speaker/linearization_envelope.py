# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction envelope (Layer 1a driver linearization, #1668 PR-B).

Pure computation only: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:class:`jasper.audio_measurement.program_analysis.DriverResponse`. No I/O, no
product policy, no CamillaDSP/emission imports — this module answers "how
many dB of correction depth is this frequency bin allowed," nothing more.
Wiring this into the fit/apply flow is a later PR (see
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
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse


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
    """

    FITTED = "envelope_fitted"
    LIMITED_BY_MIC_TIER = "envelope_limited_by_mic_tier"
    LIMITED_BY_REPEATABILITY = "envelope_limited_by_repeatability"
    LIMITED_BY_NONLINEARITY = "envelope_limited_by_nonlinearity"
    LIMITED_BY_EXCESS_PHASE = "envelope_limited_by_excess_phase"
    LIMITED_BY_CLASS_PRIOR = "envelope_limited_by_class_prior"
    LIMITED_BY_VERIFY_DIVERGENCE = "envelope_limited_by_verify_divergence"
    OUT_OF_BAND = "envelope_out_of_band"


# Closed vocabularies (design doc "Microphone doctrine" / artifact 02 §5).
# `compose_envelope` and every term function that accepts a tier/class
# rejects anything outside these tuples — see `_validate_tier` /
# `_validate_driver_class`.
MIC_TIERS: tuple[str, ...] = ("reference", "consumer", "phone")
DRIVER_CLASSES: tuple[str, ...] = (
    "compression_horn",
    "soft_dome",
    "metal_dome",
    "beryllium_diamond_dome",
    "ribbon_amt",
    "unknown",
)

# Shared working grid: 1/24-octave-scale log spacing, 150 Hz (the design
# doc's stated gated-measurement validity floor, "~143-200 Hz in the JTS3
# room") to 20 kHz. Deliberately NOT the same grid as compute_sigma.py's
# LOG_GRID_HZ (80 Hz-anchored, 185 points) — that script's floor predates
# this design doc's validity-floor contract; 176 points on a 150 Hz floor
# is this module's own choice, still >=4x finer than the ladder's finest
# step (1/6 oct), so it loses no ladder-relevant detail either.
DEFAULT_ENVELOPE_GRID_HZ: np.ndarray = np.geomspace(150.0, 20_000.0, 176)

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
    """
    occurrences: tuple[DriverResponse, ...] = (primary, *primary.repeat_responses)
    if len(occurrences) < 2:
        return None

    lo_hz, hi_hz = valid_band_hz
    valid_mask = (grid_hz >= lo_hz) & (grid_hz <= hi_hz)

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
    sigma_tolerable_db = _SIGMA_TOLERABLE_DB[tier]
    epsilon_db = 1e-6
    return ENVELOPE_CEILING_SENTINEL_DB * np.minimum(
        1.0, sigma_tolerable_db / np.maximum(sigma_db, epsilon_db)
    )


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
    decided. ``sigma_db`` is :func:`compute_sigma_curve`'s output verbatim
    (``None`` when fewer than 2 occurrences existed) — the same
    diagnostic transparency for the repeatability evidence itself.
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


def compose_envelope(
    role: str,
    primary: DriverResponse,
    *,
    excited_band_hz: tuple[float, float],
    mic_tier: str,
    driver_class: str = "unknown",
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
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

    Raises :class:`ValueError` for a ``mic_tier`` or ``driver_class``
    outside the closed vocabularies (:data:`MIC_TIERS` /
    :data:`DRIVER_CLASSES`) — "unknown" (class) and "phone" (tier) are
    themselves valid, closed-vocabulary members (the most conservative
    ones), not error cases.
    """
    _validate_tier(mic_tier)
    _validate_driver_class(driver_class)

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

    sigma_db = compute_sigma_curve(primary, valid_band_hz=excited_band_hz, grid_hz=grid_hz)

    term_specs: tuple[EnvelopeTerm, ...] = (
        EnvelopeTerm(ReasonCode.LIMITED_BY_MIC_TIER, mic_trust_limit(grid_hz, tier=mic_tier)),
        EnvelopeTerm(
            ReasonCode.LIMITED_BY_REPEATABILITY,
            repeatability_limit(sigma_db, tier=mic_tier, grid_hz=grid_hz),
        ),
        EnvelopeTerm(ReasonCode.LIMITED_BY_NONLINEARITY, linearity_limit(grid_hz)),
        EnvelopeTerm(ReasonCode.LIMITED_BY_EXCESS_PHASE, invertibility_limit(grid_hz)),
        EnvelopeTerm(
            ReasonCode.LIMITED_BY_CLASS_PRIOR,
            class_prior_limit(grid_hz, driver_class=driver_class),
        ),
    )
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

    masked_depth_db = np.where(in_band_mask, winning_value, 0.0)
    smoothed_depth_db = _ladder_smooth(grid_hz, masked_depth_db)
    smoothed_depth_db = np.where(in_band_mask, smoothed_depth_db, 0.0)

    terms_map: Mapping[ReasonCode, np.ndarray] = {
        term.code: term.depth_db for term in term_specs
    }

    return EnvelopeCurve(
        role=role,
        freqs_hz=grid_hz,
        allowed_depth_db=smoothed_depth_db,
        reason=final_reason,
        terms=terms_map,
        sigma_db=sigma_db,
        n_repeats=len(occurrences) - 1,
        mic_tier=mic_tier,
        driver_class=driver_class,
    )
