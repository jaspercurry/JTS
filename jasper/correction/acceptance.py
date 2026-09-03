# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic verify-acceptance verdict (revision plan §4 P4).

After a correction is applied and the room re-measured, deterministic code —
never a model — decides whether it stays (``accept``), is shown for a human
call (``surface``), or is rolled back (``revert_pending_confirm`` then
``revert``). The rule: aggregate to >=1/3-octave bands before any per-band
verdict; a "clear regression" needs BOTH a band past the repeatability floor
AND the overall band-RMS moving the wrong way past a noise margin; compare
against the matched position-1 curve when it exists; and escalate to ``revert``
only on a second concordant clear regression. Pure — numpy curves and
thresholds in, a typed verdict out; the session performs any rollback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from jasper.audio_measurement.analysis import (
    deviation_metrics,
    smooth_fractional_octave,
)
from jasper.audio_measurement.room_boundary import ROOM_BOUNDARY_DEFAULT_HZ
from jasper.env_load import bounded_env_float, bounded_env_int


# Every threshold whose true value is hardware-gated is a deploy-time knob; the
# defaults here are conservative placeholders, NOT empirically derived (H1
# supplies the real numbers on-device). Out-of-range or unparseable values fall
# back to the documented default.


class Verdict(str, Enum):
    """The deterministic acceptance decision.

    ``str`` mixin so the value serializes directly as a plain string.
    """

    ACCEPT = "accept"
    SURFACE = "surface"
    REVERT_PENDING_CONFIRM = "revert_pending_confirm"
    REVERT = "revert"


@dataclass(frozen=True)
class AcceptanceThresholds:
    """Tunable statistical thresholds for the acceptance verdict.

    All defaults are conservative placeholders, retuned at H1 from real
    on-device repeatability data, and seeded from
    ``jasper.correction.spatial``'s 4-6 dB seat-to-seat std constants.
    """

    # A band counts as "clearly worse" only if its error-to-target grew by more
    # than this many dB. Seeded from spatial.MEDIUM_CONFIDENCE_STD_DB (6.0),
    # the GENEROUS end of the repeatability band: auto-revert is the one
    # automatic action taken against the user's applied choice.
    band_regression_db: float = field(
        default_factory=lambda: bounded_env_float(
            "JASPER_ACCEPT_BAND_REGRESSION_DB", 6.0, lo=0.5, hi=24.0,
        )
    )
    # The overall band-RMS-error-to-target must move the wrong way by more than
    # this to count toward a clear regression. Smaller than the per-band margin
    # because RMS over many bands already averages out per-band noise.
    overall_rms_regression_db: float = field(
        default_factory=lambda: bounded_env_float(
            "JASPER_ACCEPT_OVERALL_RMS_REGRESSION_DB", 1.0, lo=0.1, hi=12.0,
        )
    )
    # To call an *accept* rather than "surface", the overall band-RMS error must
    # have improved by at least this much. The range floor permits 0.0 and the
    # comparison is >=, so setting 0 opts into ACCEPT-ON-TIE.
    overall_rms_improvement_db: float = field(
        default_factory=lambda: bounded_env_float(
            "JASPER_ACCEPT_OVERALL_RMS_IMPROVEMENT_DB", 0.5, lo=0.0, hi=12.0,
        )
    )
    # Fractional-octave band width. 3 = 1/3-octave (the plan's floor; the
    # audiometric standard). Do not raise past ~6 without re-deriving the
    # repeatability floor — finer bands have a higher std.
    smoothing_fraction: int = field(
        default_factory=lambda: bounded_env_int(
            "JASPER_ACCEPT_SMOOTHING_FRACTION", 3, lo=1, hi=6,
        )
    )

    @classmethod
    def from_env(cls) -> "AcceptanceThresholds":
        """Read all knobs from the environment (each field already does).

        Cross-field sanity net: an improvement floor at or above the per-band
        regression floor would make accept harder to reach than revert, so the
        whole default set is used instead.
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

    ``center_hz`` is the geometric-mean band center; the error fields are
    ``|curve - target|`` averaged over the band; ``delta_db`` is
    ``before - after`` (positive = improved).
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

    ``reasons`` are short machine-stable strings; ``bands`` is the table the
    decision was made on. ``confirmed`` is True only for the terminal
    ``REVERT``. ``basis`` records whether the matched position-1 curve or the
    spatial-average fallback was used.
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

        The session reads it to decide whether a second verify is concordant.
        """
        return self.verdict in (
            Verdict.REVERT_PENDING_CONFIRM,
            Verdict.REVERT,
        )


def _band_edges(
    f_low: float, f_high: float, fraction: int,
) -> np.ndarray:
    """Fractional-octave band edges spanning [f_low, f_high].

    Returns N+1 edges for N bands, each a factor of ``2**(1/fraction)`` apart,
    anchored at or below ``f_low`` and covering ``f_high``.
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

    Returns ``(band_centers_hz, band_err_db)``; a band with no grid points is
    dropped. The band grid overshoots ``f_high``, so the mask also clamps to
    ``freqs <= f_high`` — otherwise the per-band criterion would judge content
    the overall-RMS criterion excludes and the two halves of the AND rule would
    read different bands.
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

    Missing or malformed inputs never ``accept`` or ``revert``.
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
    f_high: float = ROOM_BOUNDARY_DEFAULT_HZ,
    thresholds: AcceptanceThresholds | None = None,
    basis: str = "position_1",
    verify_index: int = 1,
    prior_clear_regression: bool = False,
) -> AcceptanceResult:
    """Decide accept / surface / revert_pending_confirm / revert.

    All curves share one frequency grid. ``before_db`` is the pre-correction
    curve at the matched geometry (position 1 preferred); ``verify_db`` is the
    post-correction re-measurement. ``verify_index`` is 1 for the first verify,
    2+ for a confirmatory re-measure, and ``prior_clear_regression`` is the
    concordance gate: a first clear regression yields
    ``revert_pending_confirm``, and only a concordant second yields ``revert``.
    Degraded inputs return ``surface`` with a reason, never a crash.
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

    # deviation_metrics computes RMS |curve - target| over the raw grid in the
    # band; RMS already averages out per-bin noise, so no smoothing is needed
    # for the aggregate (smoothing protects the per-band verdict below).
    before_rms = deviation_metrics(
        b, tgt, f, f_low=f_low, f_high=f_high,
    )["rms_db"]
    after_rms = deviation_metrics(
        v, tgt, f, f_low=f_low, f_high=f_high,
    )["rms_db"]
    overall_delta = before_rms - after_rms  # positive = improved

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

    # "Clear regression" = BOTH a band clearly worse AND the overall RMS moved
    # the wrong way beyond the noise margin (plan §4 P4 point 2).
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

    # Not a clear regression: accept only if the overall RMS improved past the
    # improvement floor, else surface. A single band worse without an overall
    # regression is a local trade and never triggers a revert.
    if band_regressed:
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
