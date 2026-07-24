# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The Layer-1a driver-linearization fit engine (#1668 PR-C).

Consumes ONE driver's :class:`~jasper.audio_measurement.program_analysis.
DriverResponse` (the primary, gated, calibrated measurement) plus its
:class:`~jasper.active_speaker.linearization_envelope.EnvelopeCurve` (from
:func:`jasper.active_speaker.linearization_envelope.compose_envelope`) and
produces a cut-only PEQ/shelf fit that flattens the driver toward a
per-session target level, honoring the envelope's per-bin correction-depth
ceiling everywhere. Pure computation: numpy plus
:func:`jasper.audio_measurement.analysis.smooth_fractional_octave` and
:func:`jasper.correction.peq.design_peq` (the existing greedy cuts-only PEQ
designer, extended here — backward-compatibly — to accept a per-bin cut
ceiling). No I/O, no CamillaDSP emission — this module answers "what filters
would flatten this driver," nothing more. Wiring the result into the v2
conductor's candidate and the eventual APPLY emission stage are separate
concerns (the conductor wiring is this same PR; APPLY emission is later).

See docs/active-speaker-tuning-layers-design.md "Layer 1a concretely" for
the adopted design this module implements (fit domain, adaptive band trim,
target level, cut-preferred/normalize-downward policy, per-bin caps).

**Cut-only invariant.** Every filter this module emits carries ``gain <= 0``
— the whole correction posture is "spend sensitivity headroom downward,"
never boost. This is asserted before returning (see
:func:`fit_driver_linearization`) and pinned by a test.

**The fit domain is whatever grid the caller's ``EnvelopeCurve`` was
composed on** — :data:`~jasper.active_speaker.linearization_envelope.
DEFAULT_ENVELOPE_GRID_HZ` for every production caller (`compose_envelope`'s
own default), read here as ``envelope.freqs_hz`` rather than re-imported as
a separate constant, so this module can never silently disagree with the
grid the envelope it is fitting against actually used.

**Artifact-02 §6's boost-cap table is DORMANT, not implemented.** The
driver-linearization research (``docs/research/2026-07-23-driver-
linearization/02-engineering-spec.md`` §6) describes a future boost-capable
mode (global +6 dB max, Q<=2, gated by closed-loop achieved-vs-predicted
verification). This PR implements only the cut-only side of the design doc
("Fitting policy: cut-preferred / normalize-downward... cuts generous").
Boost support is intentionally NOT built here — it needs the closed-loop
verify machinery (design doc build-order step 2) to land first, so an
unverified boost claim never reaches a driver. Until then every filter this
module can produce is a cut (see the cut-only invariant above).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.program_analysis import DriverResponse
from jasper.correction.peq import PEQ, design_peq, predicted_response
from jasper.sound.profile import RESPONSE_SAMPLE_RATE_HZ

from .linearization_envelope import (
    ENVELOPE_CEILING_SENTINEL_DB,
    EnvelopeCurve,
    ReasonCode,
)

# --------------------------------------------------------------------------- #
# fitting policy constants
# --------------------------------------------------------------------------- #

# Per-filter cut ceiling, dB. Shared by the shelf stage (its own single
# gain) and the peaking loop's per-bin cap array (min'd against the
# envelope's own allowed depth) — design doc "cuts generous (-12 dB, Q<=8)".
PER_FILTER_CUT_CAP_DB: float = 12.0

# The coordinator's ruling (new vs. the original design-doc brief, decided
# 2026-07-23): a bound on TOTAL normalization spend across the whole fit —
# how far below the driver's own core-passband peak the fit is allowed to
# settle. Cut-only slope-flattening "spends" driver sensitivity (every dB
# cut here is a dB of max-SPL headroom the corrected driver gives up); left
# unbounded, a driver with a naturally rolling-off passband (e.g. a
# compression driver approaching its own Fc rolloff) could have the fit
# chase that rolloff arbitrarily deep, burning sensitivity for a shape the
# driver was never going to deliver cleanly anyway. 6 dB is a starting
# value — the owner's listening ladder is the actual arbiter of whether it
# is too tight or too loose; this constant is the single knob to revisit.
#
# Enforcement (see _shelf_stage): the shelf's own gain is clamped so the
# region it corrects never gets pulled more than this many dB below the
# core-passband peak. `target_level_db` itself (the median used by BOTH the
# shelf and the peaking loop) is left UNCLAMPED — it is a plain median of
# the trusted core region (see _target_and_plateau_db) and in the
# overwhelmingly common case already sits well within this budget of the
# core's own peak. Only when a correction would additionally push a region
# further down does the budget bind, and it binds the SHELF specifically
# (the peaking loop's own per-bin envelope caps are a separate, independent
# ceiling). A clamped shelf leaves an honest gap between the corrected
# curve and target in its affected region — that gap is not hidden; it
# shows up as ordinary fit residual (residual_max_db/residual_rms_db), not
# a new reason code.
MAX_NORMALIZATION_SPEND_DB: float = 6.0

# Linear-regression slope (dB per octave, over log2(f)) above which the fit
# band is treated as a genuine tilted shelf shape (CD-horn compensation,
# baffle-step) rather than local ripple the peaking loop alone should
# handle. Only a RISING (positive) slope fires the shelf stage — cut-only
# fitting cannot correct a naturally FALLING response (that would need
# boost), so a falling slope is left to the peaking loop / accepted as the
# driver's honest natural rolloff, matching the design doc's "textbook
# slopes are never assumed" backstop.
SHELF_SLOPE_THRESHOLD_DB_PER_OCT: float = 3.0

# Hard cap on filters per driver (shelf + peaking combined) — design doc
# "Fitting policy" via the engineering-spec build-order.
MAX_FILTERS_PER_DRIVER: int = 8

# A bin below this allowed-depth is treated as "the envelope permits
# nothing here" (float noise / a taper's asymptotic tail rather than a
# real allowance). Matches the value validated against the real N=3
# capture during PR-C scoping.
_ENVELOPE_NONZERO_EPS_DB: float = 0.05

# Below this magnitude a filter is cosmetic (inaudible, wastes a filter
# slot) — mirrors design_peq's own default `min_filter_gain_db`. Kept as a
# LOCAL constant (not imported) because it also gates the shelf stage's own
# worth-adding check, which is this module's logic, not design_peq's; if
# design_peq's default ever changes independently, revisit this mirror.
_MIN_FILTER_GAIN_DB: float = 0.5

_PEAKING_Q_MAX: float = 8.0
_PEAKING_FLATNESS_TARGET_DB: float = 1.0

# The RBJ Highshelf's fixed Butterworth Q — mirrors
# jasper.sound.profile._SHELF_Q (module-private there; see this module's
# top docstring for why it is duplicated rather than imported). CamillaDSP
# realizes this exact biquad family for its own Highshelf/Lowshelf filters
# (jasper.sound.profile._biquad_coeffs's Highshelf branch), so using the
# SAME Q here keeps the modeled response this module subtracts during
# fitting consistent with what a later APPLY stage would actually emit.
_HIGHSHELF_Q: float = 1.0 / math.sqrt(2.0)

# Octave-band centers for the candidate artifact's compact reason summary
# (design doc "UX reason codes" — an octave-band summary, not a per-bin
# dump). Mirrors the PR-C scoping experiment's own diagnostic printout.
_OCTAVE_BAND_CENTERS_HZ: tuple[float, ...] = (
    250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 12000.0, 16000.0, 20000.0,
)


def _ladder_smooth(grid_hz: np.ndarray, magnitude_db: np.ndarray) -> np.ndarray:
    """The design doc's smoothing ladder: 1/6 oct below 4 kHz, 1/3 oct
    4-10 kHz, 1/2 oct at/above 10 kHz.

    PARITY DUPLICATE of
    ``jasper.active_speaker.linearization_envelope._ladder_smooth``
    (module-private there, so not imported — see this module's top
    docstring). LOCKSTEP REQUIREMENT: any change to that helper's
    breakpoints/fractions must be mirrored here, or this fit engine and the
    envelope it fits against disagree about what "smoothed" means.
    ``tests/test_active_speaker_linearization_fit.py`` pins the two
    functions numerically identical.
    """
    fine = smooth_fractional_octave(grid_hz, magnitude_db, fraction=6)
    mid = smooth_fractional_octave(grid_hz, magnitude_db, fraction=3)
    coarse = smooth_fractional_octave(grid_hz, magnitude_db, fraction=2)
    return np.where(grid_hz < 4_000.0, fine, np.where(grid_hz < 10_000.0, mid, coarse))


def _highshelf_response_db(
    freqs_hz: np.ndarray, corner_hz: float, gain_db: float, q: float,
) -> np.ndarray:
    """RBJ Audio EQ Cookbook Highshelf magnitude response, in dB, evaluated
    at ``freqs_hz`` for a filter designed at ``corner_hz``/``gain_db``/``q``.

    Mirrors ``jasper.sound.profile``'s ``_biquad_coeffs``/``_filter_response_db``
    Highshelf math (module-private there — see this module's top docstring
    for why it is duplicated rather than imported) — the same digital
    biquad family CamillaDSP realizes, at
    :data:`jasper.sound.profile.RESPONSE_SAMPLE_RATE_HZ`. At ``gain_db=0``
    this is identically 0 dB everywhere (unity); at ``freq==corner_hz`` the
    response is ``gain_db/2`` (the RBJ shelf's well-known half-gain-at-corner
    property — pinned by a test against ``jasper.sound.profile``'s own
    fixture-anchored behavior in ``test_sound_peq_response.py``).
    """
    fs = float(RESPONSE_SAMPLE_RATE_HZ)
    w0 = 2.0 * math.pi * max(float(corner_hz), 1e-6) / fs
    cw0, sw0 = math.cos(w0), math.sin(w0)
    amp = 10.0 ** (float(gain_db) / 40.0)
    alpha = sw0 / (2.0 * float(q))
    beta = 2.0 * math.sqrt(amp) * alpha
    b0 = amp * ((amp + 1) + (amp - 1) * cw0 + beta)
    b1 = -2.0 * amp * ((amp - 1) + (amp + 1) * cw0)
    b2 = amp * ((amp + 1) + (amp - 1) * cw0 - beta)
    a0 = (amp + 1) - (amp - 1) * cw0 + beta
    a1 = 2.0 * ((amp - 1) - (amp + 1) * cw0)
    a2 = (amp + 1) - (amp - 1) * cw0 - beta

    f = np.asarray(freqs_hz, dtype=np.float64)
    w = 2.0 * np.pi * np.maximum(f, 1e-6) / fs
    c1, s1 = np.cos(w), np.sin(w)
    c2, s2 = np.cos(2.0 * w), np.sin(2.0 * w)
    num_re = b0 + b1 * c1 + b2 * c2
    num_im = -(b1 * s1 + b2 * s2)
    den_re = a0 + a1 * c1 + a2 * c2
    den_im = -(a1 * s1 + a2 * s2)
    num = num_re * num_re + num_im * num_im
    den = den_re * den_re + den_im * den_im
    mag2 = np.divide(num, den, out=np.zeros_like(num), where=den > 0.0)
    return 10.0 * np.log10(np.maximum(mag2, 1e-12))


@dataclass(frozen=True)
class LinearizationFilter:
    """One filter in a :class:`LinearizationFit` — a plain, JSON-safe record
    (not :class:`jasper.correction.peq.PEQ`, which has no ``biquad_type`` and
    is always implicitly Peaking).
    """

    biquad_type: str  # "Peaking" | "Highshelf"
    freq: float
    q: float
    gain: float  # dB; always <= 0 (cut-only invariant)

    def to_dict(self) -> dict[str, float | str]:
        return {
            "biquad_type": self.biquad_type,
            "freq": self.freq,
            "q": self.q,
            "gain": self.gain,
        }


@dataclass(frozen=True)
class LinearizationFit:
    """One driver's fitted linearization — the Layer-1a artifact.

    ``fit_band_hz == (0.0, 0.0)`` signals no fit was attempted (the
    envelope allowed correction nowhere — e.g. genuinely no in-band
    evidence); ``filters`` is empty in that case. ``target_level_db`` and
    ``reason_summary`` still carry honest values in that degenerate case
    (target 0.0, reason summary reflecting the envelope's own out-of-band
    verdicts).
    """

    role: str
    filters: tuple[LinearizationFilter, ...]
    fit_band_hz: tuple[float, float]
    target_level_db: float
    residual_rms_db: float
    residual_max_db: float
    reason_summary: Mapping[str, str]
    mic_tier: str
    driver_class: str
    n_repeats: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "filters": [f.to_dict() for f in self.filters],
            "fit_band_hz": list(self.fit_band_hz),
            "target_level_db": self.target_level_db,
            "residual_rms_db": self.residual_rms_db,
            "residual_max_db": self.residual_max_db,
            "reason_summary": dict(self.reason_summary),
            "mic_tier": self.mic_tier,
            "driver_class": self.driver_class,
            "n_repeats": self.n_repeats,
        }


def predicted_correction_db(
    filters: Sequence[LinearizationFilter], freqs_hz: np.ndarray,
) -> np.ndarray:
    """The summed dB correction ``filters`` apply across ``freqs_hz``.

    Peaking entries reuse ``jasper.correction.peq.predicted_response``
    (the SAME Lorentzian-bell model the peaking loop's own greedy residual
    tracking used while fitting, so this is not a second, possibly-drifted
    model); Highshelf entries reuse :func:`_highshelf_response_db` (the
    SAME RBJ evaluation the shelf stage subtracted while fitting). Callers
    apply this in the LINEAR domain: ``W_lin = W * 10**(db/20)``.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    total = np.zeros_like(freqs)
    peaking = [
        PEQ(freq=f.freq, q=f.q, gain=f.gain)
        for f in filters if f.biquad_type == "Peaking"
    ]
    if peaking:
        total = total + predicted_response(peaking, freqs)
    for f in filters:
        if f.biquad_type == "Highshelf":
            total = total + _highshelf_response_db(freqs, f.freq, f.gain, f.q)
    return total


def _octave_band_reason_summary(envelope: EnvelopeCurve) -> dict[str, str]:
    grid = envelope.freqs_hz
    out: dict[str, str] = {}
    for center in _OCTAVE_BAND_CENTERS_HZ:
        if center < grid[0] or center > grid[-1]:
            continue
        idx = int(np.argmin(np.abs(grid - center)))
        out[str(int(center))] = envelope.reason[idx].value
    return out


def _empty_fit(envelope: EnvelopeCurve) -> LinearizationFit:
    return LinearizationFit(
        role=envelope.role,
        filters=(),
        fit_band_hz=(0.0, 0.0),
        target_level_db=0.0,
        residual_rms_db=0.0,
        residual_max_db=0.0,
        reason_summary=_octave_band_reason_summary(envelope),
        mic_tier=envelope.mic_tier,
        driver_class=envelope.driver_class,
        n_repeats=envelope.n_repeats,
    )


def _core_or_fallback_mask(
    envelope: EnvelopeCurve, envelope_mask: np.ndarray,
) -> np.ndarray:
    """The "core passband" — bins where BOTH mic-trust and class-prior still
    sit at the ceiling sentinel (not yet tapering) — intersected with the
    fit-eligible mask. Falls back to the whole fit-eligible mask when the
    core is empty (an aggressively-tapered tier/class with no untapered
    region at all — e.g. a "phone" tier whose mic-trust taper starts low).
    """
    mic_trust = envelope.terms[ReasonCode.LIMITED_BY_MIC_TIER]
    class_prior = envelope.terms[ReasonCode.LIMITED_BY_CLASS_PRIOR]
    core = (
        np.isclose(mic_trust, ENVELOPE_CEILING_SENTINEL_DB)
        & np.isclose(class_prior, ENVELOPE_CEILING_SENTINEL_DB)
        & envelope_mask
    )
    return core if core.any() else envelope_mask


def _target_and_plateau_db(
    smoothed_db: np.ndarray, level_mask: np.ndarray,
) -> tuple[float, float]:
    """``(target_level_db, plateau_level_db)`` — the design doc's "Target
    level" rule (median, NOT the band minimum — proven bad on real data)
    plus the coordinator's normalization-budget plateau (the SAME region's
    own maximum).
    """
    band = smoothed_db[level_mask]
    return float(np.median(band)), float(np.max(band))


def _adaptive_band_trim(
    grid_hz: np.ndarray,
    smoothed_db: np.ndarray,
    envelope_mask: np.ndarray,
    target_level_db: float,
) -> tuple[int, int]:
    """Adaptive fit-band trim (design doc "Layer 1a concretely" — the
    scoping experiment's mechanism that pulled a real woofer's edge from
    4000 to ~2600 Hz as it rolled off approaching its own crossover point).
    Returns inclusive ``(lo_idx, hi_idx)`` grid indices.

    The seed is CURVE-SHAPE-DRIVEN, not trust-driven: the extremes of
    ``envelope_mask`` bins whose smoothed value is already within one
    cut-budget of ``target_level_db`` (``smoothed_db >= target - cut_budget``
    — the SAME per-filter cut budget the peaking loop's per-bin caps use).
    This is deliberately NOT the mic-trust/class-prior "core" region
    (:func:`_core_or_fallback_mask`, used only for the target/plateau level):
    a driver's own natural acoustic rolloff toward its crossover point has
    nothing to do with mic trust or driver class, and for a woofer band
    entirely below its class/tier taper breakpoints the "core" spans the
    WHOLE envelope-eligible range — seeding from ITS extremes would start
    the walk already at the outer edge, with no room left to trim the
    rolloff at all (the bug an earlier version of this function had).

    From that seed, extends outward toward each edge of ``envelope_mask``,
    stopping the FIRST time either: the smoothed curve drops below the
    floor, or ``envelope_mask`` itself ends (handles a non-contiguous mask
    safely, though a contiguous mask is the overwhelmingly common case —
    the OUT_OF_BAND premask plus smooth monotone tapers make one).
    """
    idxs = np.flatnonzero(envelope_mask)
    floor_db = target_level_db - PER_FILTER_CUT_CAP_DB
    within_budget = envelope_mask & (smoothed_db >= floor_db)
    seed_idxs = np.flatnonzero(within_budget)
    if seed_idxs.size:
        seed_lo, seed_hi = int(seed_idxs[0]), int(seed_idxs[-1])
    else:
        # Degenerate: no bin anywhere is within budget of target (a wildly
        # noisy or ill-fitting target). Seed from the single closest bin so
        # the walk below still has somewhere to start; both loops then find
        # that bin itself already violates (or exactly meets) the floor and
        # go no further, collapsing to a 1-bin band rather than crashing.
        nearest = int(idxs[np.argmin(np.abs(smoothed_db[idxs] - target_level_db))])
        seed_lo = seed_hi = nearest

    lo_bound = int(idxs[0])
    fit_lo_idx = seed_lo
    for i in range(seed_lo, lo_bound - 1, -1):
        if not envelope_mask[i] or smoothed_db[i] < floor_db:
            break
        fit_lo_idx = i

    hi_bound = int(idxs[-1])
    fit_hi_idx = seed_hi
    for i in range(seed_hi, hi_bound + 1):
        if not envelope_mask[i] or smoothed_db[i] < floor_db:
            break
        fit_hi_idx = i

    return fit_lo_idx, fit_hi_idx


def _shelf_stage(
    grid_hz: np.ndarray,
    smoothed_db: np.ndarray,
    band_mask: np.ndarray,
    fit_lo_hz: float,
    fit_hi_hz: float,
    target_level_db: float,
    plateau_level_db: float,
) -> LinearizationFilter | None:
    """Fit ONE cut-only Highshelf if the fit band's smoothed slope rises
    faster than :data:`SHELF_SLOPE_THRESHOLD_DB_PER_OCT`. Returns ``None``
    when no shelf is warranted (falling/shallow slope, too few points to
    regress, or the normalization budget leaves nothing to spend).
    """
    if int(band_mask.sum()) < 2:
        return None
    log2_f = np.log2(grid_hz[band_mask])
    slope_db_per_oct, intercept = np.polyfit(log2_f, smoothed_db[band_mask], 1)
    if slope_db_per_oct <= SHELF_SLOPE_THRESHOLD_DB_PER_OCT:
        return None

    pred_lo = slope_db_per_oct * math.log2(fit_lo_hz) + intercept
    pred_hi = slope_db_per_oct * math.log2(fit_hi_hz) + intercept
    dev_lo = abs(pred_lo - target_level_db)
    dev_hi = abs(pred_hi - target_level_db)
    if dev_hi >= dev_lo:
        corner_hz, total_drop_db = fit_hi_hz, max(0.0, pred_hi - target_level_db)
    else:
        corner_hz, total_drop_db = fit_lo_hz, max(0.0, pred_lo - target_level_db)
    if total_drop_db <= 0.0:
        return None

    # Coordinator's normalization-budget clamp: how much of the total spend
    # budget is left once the plain target-vs-plateau gap is accounted for.
    # See MAX_NORMALIZATION_SPEND_DB's docstring for the full reasoning.
    remaining_budget_db = max(
        0.0, MAX_NORMALIZATION_SPEND_DB - (plateau_level_db - target_level_db)
    )
    shelf_cut_db = min(total_drop_db, PER_FILTER_CUT_CAP_DB, remaining_budget_db)
    if shelf_cut_db < _MIN_FILTER_GAIN_DB:
        return None
    return LinearizationFilter(
        biquad_type="Highshelf", freq=corner_hz, q=_HIGHSHELF_Q, gain=-shelf_cut_db,
    )


def fit_driver_linearization(
    primary: DriverResponse, envelope: EnvelopeCurve,
) -> LinearizationFit:
    """Fit one driver's cut-only linearization from its measured response
    and correction envelope.

    ``envelope`` carries everything besides the raw magnitude curve —
    role, mic tier, driver class, repeat count, and (critically) the
    per-bin allowed correction depth — so this function reads context off
    ``envelope`` rather than taking redundant separate parameters.

    Algorithm (design doc "Layer 1a concretely"):
      1. Resample ``primary``'s magnitude onto ``envelope``'s grid, ladder-
         smooth it.
      2. Fit band = envelope-nonzero bins, trimmed by the adaptive-band-trim
         walk (never fit past where the curve has already fallen more than
         one filter's cut budget below target).
      3. Target level = median of the smoothed curve over the trusted core
         passband (NOT the band minimum).
      4. Shelf stage: one cut-only Highshelf if the fit band's regression
         slope rises faster than the threshold, budget-clamped.
      5. Peaking loop: ``jasper.correction.peq.design_peq`` on the
         post-shelf residual, cuts-only, capped per-bin by
         ``min(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db)``.

    Returns a :class:`LinearizationFit` with zero filters (an honest no-op)
    when the envelope allows correction nowhere.
    """
    grid_hz = envelope.freqs_hz
    measured_db = np.interp(grid_hz, primary.freqs_hz, primary.magnitude_db)
    smoothed_db = _ladder_smooth(grid_hz, measured_db)

    envelope_mask = envelope.allowed_depth_db > _ENVELOPE_NONZERO_EPS_DB
    if not envelope_mask.any():
        return _empty_fit(envelope)

    level_mask = _core_or_fallback_mask(envelope, envelope_mask)
    target_level_db, plateau_level_db = _target_and_plateau_db(smoothed_db, level_mask)

    fit_lo_idx, fit_hi_idx = _adaptive_band_trim(
        grid_hz, smoothed_db, envelope_mask, target_level_db,
    )
    band_mask = np.zeros_like(envelope_mask)
    band_mask[fit_lo_idx:fit_hi_idx + 1] = True
    band_mask &= envelope_mask
    fit_lo_hz = float(grid_hz[fit_lo_idx])
    fit_hi_hz = float(grid_hz[fit_hi_idx])

    filters: list[LinearizationFilter] = []
    working_db = smoothed_db.copy()
    remaining_filters = MAX_FILTERS_PER_DRIVER

    if fit_hi_idx > fit_lo_idx:
        shelf = _shelf_stage(
            grid_hz, smoothed_db, band_mask, fit_lo_hz, fit_hi_hz,
            target_level_db, plateau_level_db,
        )
        if shelf is not None:
            working_db = working_db + _highshelf_response_db(
                grid_hz, shelf.freq, shelf.gain, shelf.q,
            )
            filters.append(shelf)
            remaining_filters -= 1

    if remaining_filters > 0 and fit_hi_idx > fit_lo_idx:
        target_array = np.full_like(grid_hz, target_level_db)
        per_bin_cap_db = -np.minimum(PER_FILTER_CUT_CAP_DB, envelope.allowed_depth_db)
        peqs = design_peq(
            working_db, target_array, grid_hz,
            f_low=fit_lo_hz, f_high=fit_hi_hz,
            max_filters=remaining_filters,
            max_cut_db=per_bin_cap_db,
            max_boost_db=0.0,
            cuts_only=True,
            flatness_target_db=_PEAKING_FLATNESS_TARGET_DB,
            q_max=_PEAKING_Q_MAX,
            min_filter_gain_db=_MIN_FILTER_GAIN_DB,
        )
        if peqs:
            working_db = working_db + predicted_response(peqs, grid_hz)
            filters.extend(
                LinearizationFilter(
                    biquad_type="Peaking", freq=p.freq, q=p.q, gain=p.gain,
                )
                for p in peqs
            )

    assert all(f.gain <= 0.0 for f in filters), "linearization fit emitted a boost"

    residual = (working_db - target_level_db)[band_mask]
    residual_rms_db = float(np.sqrt(np.mean(residual ** 2))) if residual.size else 0.0
    residual_max_db = float(np.max(np.abs(residual))) if residual.size else 0.0

    return LinearizationFit(
        role=envelope.role,
        filters=tuple(filters),
        fit_band_hz=(fit_lo_hz, fit_hi_hz),
        target_level_db=target_level_db,
        residual_rms_db=residual_rms_db,
        residual_max_db=residual_max_db,
        reason_summary=_octave_band_reason_summary(envelope),
        mic_tier=envelope.mic_tier,
        driver_class=envelope.driver_class,
        n_repeats=envelope.n_repeats,
    )
