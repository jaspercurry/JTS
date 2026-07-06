# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic verify-acceptance verdict (revision plan §4 P4).

JTS's genuine differentiator: after a correction is applied and the room is
re-measured, **deterministic code** — never a model, never the household's
optimism — decides whether the correction stays, gets surfaced for a human
call, or is automatically reverted. The LLM may *propose* a filter; this module
is the judge, and the room's own re-measurement is the evidence.

Why this is not a one-line "did RMS go down?" check
---------------------------------------------------
The naive rule the revision plan explicitly killed (§8) is: take the single
verify capture, compare it against the multi-position spatial average, and
revert if any frequency got worse. That rule reverts *good* corrections on
measurement noise, because:

  * The "before" is an N-position spatial average; the verify is **one**
    position. Comparing one seat against the average of several is not
    apples-to-apples — a single seat legitimately differs from the room mean.
  * Per-frequency seat-to-seat standard deviation of **4–6 dB is NORMAL** in
    this repo's own measurements (see :mod:`jasper.correction.spatial`,
    ``HIGH_CONFIDENCE_STD_DB`` / ``MEDIUM_CONFIDENCE_STD_DB``). A raw per-bin
    "this band got 3 dB worse" verdict sits *inside* that repeatability floor —
    it is noise, not a regression.

So the acceptance rule (plan §4 P4, points 1–4) is:

  1. **Aggregate to ≥1/3-octave smoothed bands** before *any* per-band verdict.
     Raw per-bin comparison is forbidden — a deep null wandering by one bin is
     not a regression.
  2. **"Clear regression" requires BOTH** (a) at least one band worsening
     *beyond the repeatability floor* (seeded from ``spatial.py``'s 4–6 dB std
     constants, shipped as env-tunable ``JASPER_ACCEPT_*`` knobs — placeholders
     until H1 supplies real on-device repeatability), **and** (b) the overall
     band-RMS-error-to-target moving in the *wrong* direction beyond a noise
     margin. Neither alone is enough: one band worse while the whole curve
     improved is a local trade the correction made on purpose; a whole-curve
     RMS wobble inside the noise margin with no band clearly worse is
     measurement drift, not damage.
  3. **Matched comparison basis.** The verify is captured at position 1 (a flow
     instruction). We compare it against the **stored position-1 pre-correction
     curve** whenever that curve exists — same geometry, apples-to-apples. The
     spatial average is only a fallback basis (single-position sessions, or a
     lost position-1 curve).
  4. **One confirmatory re-measure before auto-revert.** A clear regression on
     the *first* verify yields ``revert_pending_confirm`` — the flow asks for a
     second measurement. Only a *second concordant* clear regression escalates
     to ``revert`` and trips the automatic rollback. A false revert is
     trust-expensive; a second sweep is cheap.

The four verdicts
-----------------
``accept``
    The measured error-to-target dropped and no band regressed clearly. The
    correction stays.
``surface``
    Ambiguous — the numbers sit inside the noise floor, or improvement and a
    borderline regression cancel out. We show the honest before/after and let
    the household decide; we never revert on a tie.
``revert_pending_confirm``
    A clear regression on this verify. Not yet reverted — the flow asks for
    one confirmatory re-measure. Declining is simply *not doing it*: the
    verdict stays ``revert_pending_confirm``, nothing ever reverts without the
    concordant second sweep, the correction stays applied, and /start (a fresh
    measurement) and /reset (manual removal) remain available as always.
``revert``
    A clear regression, *confirmed* by a second concordant verify — strictly
    the verify immediately after the pending one; a clean verify in between
    clears the pending question (the session owns that adjacency bookkeeping).
    The session performs the automatic rollback through the **existing** reset
    path (this module never writes CamillaDSP — it returns a verdict; the
    session acts).

This module is **pure**: it takes numpy curves + thresholds and returns a typed
verdict. No I/O, no CamillaDSP, no session state. That keeps it synthetically
testable against curves with known ground truth (a genuinely-improved room, a
genuinely-regressed band, pure noise at the repeatability floor, the ambiguous
middle) — see ``tests/test_correction_acceptance.py``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from jasper.audio_measurement.analysis import (
    deviation_metrics,
    smooth_fractional_octave,
)


# --- env-knob helpers ---------------------------------------------------------
#
# Every threshold whose true value is hardware-gated is a deploy-time knob (H1
# supplies the real numbers on-device — the defaults here are conservative
# placeholders, NOT empirically derived), mirroring the
# JASPER_CAPTURE_ALIGNMENT_THRESHOLD pattern in capture_relay/alignment.py and
# the JASPER_RAMP_* knobs in audio_measurement/ramp.py. Set them in jasper.env
# once measured; no rebuild required. Out-of-range or unparseable values fall
# back to the documented default — a jasper.env edit can never brick the
# evaluator at construction time.


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


def _env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


class Verdict(str, Enum):
    """The deterministic acceptance decision.

    ``str`` mixin so the value serializes directly into the envelope /
    result.json / event logs as a plain string (``"accept"``, …) with no
    per-callsite ``.value``.
    """

    ACCEPT = "accept"
    SURFACE = "surface"
    REVERT_PENDING_CONFIRM = "revert_pending_confirm"
    REVERT = "revert"


@dataclass(frozen=True)
class AcceptanceThresholds:
    """Tunable statistical thresholds for the acceptance verdict.

    All defaults are **conservative placeholders** retuned at H1 from real
    on-device repeatability data (revision plan §5 H1). They are seeded from
    ``jasper.correction.spatial``'s per-frequency seat-to-seat std constants
    (4–6 dB), which are this repo's own measured repeatability floor.
    """

    # A band counts as "clearly worse" only if its error-to-target grew by more
    # than this many dB. Seeded from spatial.MEDIUM_CONFIDENCE_STD_DB (6.0): a
    # 1/3-octave-band shift inside the seat-to-seat repeatability floor is
    # noise, not damage. We use the *medium* (looser) floor, not the *high*
    # (4 dB) one, deliberately — auto-revert is the one automatic action the
    # system takes against the user's applied choice, so its trigger must clear
    # the *generous* end of the repeatability band, never the tight end.
    band_regression_db: float = field(
        default_factory=lambda: _env_float(
            "JASPER_ACCEPT_BAND_REGRESSION_DB", 6.0, lo=0.5, hi=24.0,
        )
    )
    # The overall band-RMS-error-to-target must move in the wrong direction by
    # more than this to count toward a clear regression. A smaller margin than
    # the per-band one: RMS over many bands averages out per-band noise, so a
    # real whole-curve regression shows up at a lower dB. Seeded below the 4 dB
    # high-confidence std because it is an aggregate, not a single band.
    overall_rms_regression_db: float = field(
        default_factory=lambda: _env_float(
            "JASPER_ACCEPT_OVERALL_RMS_REGRESSION_DB", 1.0, lo=0.1, hi=12.0,
        )
    )
    # To call an *accept* (not merely "surface"), the overall band-RMS error
    # must have *improved* by at least this much. Below it the result is a wash
    # — honest "surface", neither revert nor a claimed win. Small: even a
    # modest, real modal cut clears it, but pure noise (mean ~0) does not.
    # The range floor deliberately permits 0.0, and the comparison is >=, so
    # an operator setting 0 opts into ACCEPT-ON-TIE ("confirmed improved" on
    # an exactly-zero delta) — keep it > 0 unless that is the intent.
    overall_rms_improvement_db: float = field(
        default_factory=lambda: _env_float(
            "JASPER_ACCEPT_OVERALL_RMS_IMPROVEMENT_DB", 0.5, lo=0.0, hi=12.0,
        )
    )
    # Fractional-octave band width for aggregation. 3 = 1/3-octave (the plan's
    # ">=1/3-octave" floor; the audiometric standard). Higher = finer bands =
    # closer to per-bin (do not raise past ~6 without re-deriving the floor —
    # finer bands have a higher repeatability std).
    smoothing_fraction: int = field(
        default_factory=lambda: _env_int(
            "JASPER_ACCEPT_SMOOTHING_FRACTION", 3, lo=1, hi=6,
        )
    )

    @classmethod
    def from_env(cls) -> "AcceptanceThresholds":
        """Read all knobs from the environment (each field already does).

        A cross-field sanity net: if the improvement floor is somehow set at or
        above the per-band regression floor (a nonsensical combination that
        would make *accept* harder to reach than *revert*), fall back to the
        whole default set rather than shipping an incoherent threshold pair —
        mirrors ``MeasurementRamp.from_env``'s all-or-nothing fallback.
        """
        t = cls()
        if t.overall_rms_improvement_db >= t.band_regression_db:
            return cls(
                band_regression_db=6.0,
                overall_rms_regression_db=1.0,
                overall_rms_improvement_db=0.5,
                smoothing_fraction=3,
            )
        return t


@dataclass(frozen=True)
class BandVerdict:
    """Per-band before/after error-to-target for the verdict table.

    ``center_hz`` is the geometric-mean center of the 1/3-octave band;
    ``before_err_db`` / ``after_err_db`` are absolute deviations from target
    (|curve − target|) averaged over the band; ``delta_db`` is
    ``before − after`` (positive = improved); ``regressed`` is True when the
    band grew worse by more than the regression threshold.
    """

    center_hz: float
    before_err_db: float
    after_err_db: float
    delta_db: float
    regressed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_hz": round(self.center_hz, 1),
            "before_err_db": round(self.before_err_db, 2),
            "after_err_db": round(self.after_err_db, 2),
            "delta_db": round(self.delta_db, 2),
            "regressed": self.regressed,
        }


@dataclass(frozen=True)
class AcceptanceResult:
    """The typed output of :func:`evaluate_acceptance`.

    ``verdict`` is the decision; ``reasons`` are short machine-stable strings
    explaining it (for logs + the evidence bundle); ``bands`` is the per-band
    table the decision was made on; the scalar fields are the aggregate numbers
    that drove it. ``confirmed`` is True only for the terminal ``REVERT``
    (a second concordant clear regression). ``basis`` records whether the
    matched position-1 curve or the spatial-average fallback was used.
    """

    verdict: Verdict
    reasons: tuple[str, ...]
    bands: tuple[BandVerdict, ...]
    overall_before_rms_db: float
    overall_after_rms_db: float
    overall_rms_delta_db: float
    regressed_band_count: int
    worst_band_delta_db: float
    worst_band_center_hz: float | None
    basis: str
    confirmed: bool = False
    verify_index: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "confirmed": self.confirmed,
            "verify_index": self.verify_index,
            "basis": self.basis,
            "overall_before_rms_db": round(self.overall_before_rms_db, 2),
            "overall_after_rms_db": round(self.overall_after_rms_db, 2),
            "overall_rms_delta_db": round(self.overall_rms_delta_db, 2),
            "regressed_band_count": self.regressed_band_count,
            "worst_band_delta_db": round(self.worst_band_delta_db, 2),
            "worst_band_center_hz": (
                round(self.worst_band_center_hz, 1)
                if self.worst_band_center_hz is not None
                else None
            ),
            "bands": [b.to_dict() for b in self.bands],
        }

    @property
    def clear_regression(self) -> bool:
        """True when this verify shows a clear regression (both criteria met).

        ``revert_pending_confirm`` (first verify) and ``revert`` (confirmed)
        both rest on this; ``surface``/``accept`` do not. Used by the session
        to decide whether a *second* verify is concordant with the first.
        """
        return self.verdict in (
            Verdict.REVERT_PENDING_CONFIRM,
            Verdict.REVERT,
        )


def _band_edges(
    f_low: float, f_high: float, fraction: int,
) -> np.ndarray:
    """Fractional-octave band edges spanning [f_low, f_high].

    Returns edge frequencies (N+1 edges for N bands), each a factor of
    ``2**(1/fraction)`` apart, anchored so the band grid starts at or below
    ``f_low`` and covers ``f_high``. Bands are the aggregation unit for the
    per-band verdict — never raw FFT bins.
    """
    if f_high <= f_low:
        return np.asarray([f_low, f_high], dtype=np.float64)
    ratio = 2.0 ** (1.0 / fraction)
    n_bands = int(np.ceil(np.log(f_high / f_low) / np.log(ratio)))
    n_bands = max(1, n_bands)
    return f_low * ratio ** np.arange(n_bands + 1, dtype=np.float64)


def _aggregate_band_errors(
    freqs: np.ndarray,
    curve_db: np.ndarray,
    target_db: np.ndarray,
    *,
    f_low: float,
    f_high: float,
    fraction: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Mean absolute error-to-target per fractional-octave band.

    The curve is first 1/N-octave power-smoothed (the plan's ">=1/3-octave
    aggregation" — no per-bin judgement), then the mean |smoothed − target| is
    taken within each band. Returns (band_centers_hz, band_err_db). A band with
    no grid points in it is dropped (its center never appears in the output).

    The band grid over-shoots ``f_high`` (the last 2^(1/N) edge lands past
    it), so the mask additionally clamps to ``freqs <= f_high`` — otherwise
    the per-band criterion would judge content above ``f_high`` that the
    overall-RMS criterion (``deviation_metrics``, inclusive of ``f_high``)
    excludes, and the two halves of the AND rule would read different bands.
    The clamped last band is narrower than 1/N octave; its per-point values
    are already 1/N-octave-smoothed, so a narrow band is not noisy.
    """
    smoothed = smooth_fractional_octave(freqs, curve_db, fraction=fraction)
    err = np.abs(smoothed - target_db)
    edges = _band_edges(f_low, f_high, fraction)

    centers: list[float] = []
    band_errs: list[float] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (freqs >= lo) & (freqs < hi) & (freqs <= f_high)
        if not mask.any():
            continue
        centers.append(float(np.sqrt(lo * hi)))  # geometric-mean center
        band_errs.append(float(np.mean(err[mask])))
    if not centers:
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    return np.asarray(centers, dtype=np.float64), np.asarray(
        band_errs, dtype=np.float64,
    )


def _surface_reason_result(
    reason: str,
    *,
    basis: str = "unavailable",
    verify_index: int = 1,
) -> AcceptanceResult:
    """A ``surface`` verdict with empty numbers, for degraded inputs.

    We never ``accept`` or ``revert`` when the inputs are missing or malformed
    — the honest fallback is to show what we have and let the household decide.
    """
    return AcceptanceResult(
        verdict=Verdict.SURFACE,
        reasons=(reason,),
        bands=(),
        overall_before_rms_db=0.0,
        overall_after_rms_db=0.0,
        overall_rms_delta_db=0.0,
        regressed_band_count=0,
        worst_band_delta_db=0.0,
        worst_band_center_hz=None,
        basis=basis,
        verify_index=verify_index,
    )


def evaluate_acceptance(
    *,
    freqs: np.ndarray,
    before_db: np.ndarray,
    verify_db: np.ndarray,
    target_db: np.ndarray,
    f_low: float = 50.0,
    f_high: float = 350.0,
    thresholds: AcceptanceThresholds | None = None,
    basis: str = "position_1",
    verify_index: int = 1,
    prior_clear_regression: bool = False,
) -> AcceptanceResult:
    """Decide accept / surface / revert_pending_confirm / revert.

    All curves share one frequency grid (they do in the verify path — every
    capture resamples onto the same log grid). ``before_db`` is the
    pre-correction curve at the **matched** geometry (position 1 preferred);
    ``verify_db`` is the post-correction re-measurement; ``target_db`` is the
    shared correction target.

    ``verify_index`` is 1 for the first verify, 2+ for a confirmatory
    re-measure. ``prior_clear_regression`` is True when an *earlier* verify in
    this session already showed a clear regression: the confirmatory-re-measure
    concordance gate. The escalation rule (plan §4 P4 point 4):

      * first clear regression (``verify_index == 1`` or no prior) →
        ``revert_pending_confirm`` (ask for one more sweep, do not revert);
      * a clear regression that is **concordant** with a prior clear regression
        (``prior_clear_regression and verify_index >= 2``) → ``revert``
        (confirmed; the session auto-reverts);
      * anything not a clear regression → ``accept`` (improved beyond the
        noise floor) or ``surface`` (a wash / ambiguous).

    Band width, the regression floor, the RMS margins, and the smoothing
    fraction come from ``thresholds`` (env-tunable; H1 retunes). Returns a fully
    populated :class:`AcceptanceResult`; degraded inputs return ``surface`` with
    an explanatory reason, never a crash and never an accept/revert on bad data.
    """
    t = thresholds or AcceptanceThresholds.from_env()

    f = np.asarray(freqs, dtype=np.float64)
    b = np.asarray(before_db, dtype=np.float64)
    v = np.asarray(verify_db, dtype=np.float64)
    tgt = np.asarray(target_db, dtype=np.float64)
    if not (f.size == b.size == v.size == tgt.size) or f.size == 0:
        return _surface_reason_result(
            "curve length mismatch or empty", basis=basis,
            verify_index=verify_index,
        )
    if not (
        np.all(np.isfinite(f))
        and np.all(np.isfinite(b))
        and np.all(np.isfinite(v))
        and np.all(np.isfinite(tgt))
    ):
        return _surface_reason_result(
            "curve contains non-finite values", basis=basis,
            verify_index=verify_index,
        )

    # --- overall aggregate error-to-target (band-RMS over [f_low, f_high]) ---
    # deviation_metrics computes RMS |curve − target| over the raw grid in the
    # band. That is the aggregate the "overall RMS moved wrong" criterion tests.
    # (No per-band smoothing needed for the RMS aggregate — RMS over the band
    # already averages out per-bin noise; smoothing is what protects the *per
    # band* verdict below.)
    before_rms = deviation_metrics(
        b, tgt, f, f_low=f_low, f_high=f_high,
    )["rms_db"]
    after_rms = deviation_metrics(
        v, tgt, f, f_low=f_low, f_high=f_high,
    )["rms_db"]
    overall_delta = before_rms - after_rms  # positive = improved

    # --- per-band verdict on >=1/3-octave smoothed bands --------------------
    centers, before_band_err = _aggregate_band_errors(
        f, b, tgt, f_low=f_low, f_high=f_high, fraction=t.smoothing_fraction,
    )
    _, after_band_err = _aggregate_band_errors(
        f, v, tgt, f_low=f_low, f_high=f_high, fraction=t.smoothing_fraction,
    )
    if centers.size == 0:
        return _surface_reason_result(
            "no measurement points in the correction band", basis=basis,
            verify_index=verify_index,
        )

    band_delta = before_band_err - after_band_err  # positive = improved
    regressed_mask = (after_band_err - before_band_err) > t.band_regression_db

    bands = tuple(
        BandVerdict(
            center_hz=float(centers[i]),
            before_err_db=float(before_band_err[i]),
            after_err_db=float(after_band_err[i]),
            delta_db=float(band_delta[i]),
            regressed=bool(regressed_mask[i]),
        )
        for i in range(centers.size)
    )
    regressed_count = int(regressed_mask.sum())
    worst_idx = int(np.argmin(band_delta))  # most-negative delta = worst
    worst_delta = float(band_delta[worst_idx])
    worst_center = float(centers[worst_idx])

    # --- combine into a verdict ---------------------------------------------
    # "Clear regression" = BOTH a band clearly worse AND the overall RMS moved
    # the wrong way beyond the noise margin (plan §4 P4 point 2). Neither alone
    # trips it.
    band_regressed = regressed_count > 0
    overall_worsened = overall_delta < -t.overall_rms_regression_db
    clear_regression = band_regressed and overall_worsened

    reasons: list[str] = []
    if clear_regression:
        reasons.append(
            f"{regressed_count} band(s) worse by >"
            f"{t.band_regression_db:.1f} dB "
            f"(worst {worst_delta:.1f} dB at {worst_center:.0f} Hz)"
        )
        reasons.append(
            f"overall RMS error grew {-overall_delta:.2f} dB "
            f"(> {t.overall_rms_regression_db:.1f} dB margin)"
        )
        if prior_clear_regression and verify_index >= 2:
            reasons.append("confirmed by a second concordant re-measure")
            verdict = Verdict.REVERT
            confirmed = True
        else:
            reasons.append("first regression — one confirmatory re-measure")
            verdict = Verdict.REVERT_PENDING_CONFIRM
            confirmed = False
        return AcceptanceResult(
            verdict=verdict,
            reasons=tuple(reasons),
            bands=bands,
            overall_before_rms_db=before_rms,
            overall_after_rms_db=after_rms,
            overall_rms_delta_db=overall_delta,
            regressed_band_count=regressed_count,
            worst_band_delta_db=worst_delta,
            worst_band_center_hz=worst_center,
            basis=basis,
            confirmed=confirmed,
            verify_index=verify_index,
        )

    # Not a clear regression. Accept only if the overall RMS improved beyond the
    # improvement floor; otherwise it is a wash / ambiguous → surface. A single
    # band worse (but not paired with an overall regression) is a local trade
    # the correction made on purpose — it degrades an accept to surface if the
    # overall win is small, but never triggers a revert.
    if band_regressed:
        # A band cleared the per-band floor but the overall RMS did not worsen
        # beyond its margin — a borderline local trade. Never accept silently;
        # surface it honestly.
        reasons.append(
            f"{regressed_count} band(s) worse by >"
            f"{t.band_regression_db:.1f} dB, but overall RMS held "
            f"({overall_delta:+.2f} dB) — surfacing, not reverting"
        )
        verdict = Verdict.SURFACE
    elif overall_delta >= t.overall_rms_improvement_db:
        reasons.append(
            f"overall RMS error dropped {overall_delta:.2f} dB "
            f"(>= {t.overall_rms_improvement_db:.1f} dB) with no band regressed"
        )
        verdict = Verdict.ACCEPT
    else:
        reasons.append(
            f"overall RMS change {overall_delta:+.2f} dB is within the noise "
            f"floor (< {t.overall_rms_improvement_db:.1f} dB) — a wash"
        )
        verdict = Verdict.SURFACE

    return AcceptanceResult(
        verdict=verdict,
        reasons=tuple(reasons),
        bands=bands,
        overall_before_rms_db=before_rms,
        overall_after_rms_db=after_rms,
        overall_rms_delta_db=overall_delta,
        regressed_band_count=regressed_count,
        worst_band_delta_db=worst_delta,
        worst_band_center_hz=worst_center,
        basis=basis,
        confirmed=False,
        verify_index=verify_index,
    )
