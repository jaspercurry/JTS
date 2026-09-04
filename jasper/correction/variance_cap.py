# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-position spread as a per-frequency ceiling on room-correction depth.

Owns one curve — ``allowed_depth_db = base_max_cut_db * min(1, tolerable /
max(sigma, eps))`` — its tolerance, and the typed disclosure for one design
run. Sigma comes from :mod:`jasper.correction.spatial` and is a POPULATION std
(``ddof=0``); ``audio_measurement.spatial_combine`` uses ``ddof=1`` and only
band-level scalars, so this tolerance does not transfer to that lane. The band
comes from the caller and scopes the disclosure counts only. Depths are non-positive dB;
``max_depth_forgone_db`` is a non-negative reduction. The statistic is RAW
per-frequency sigma, not ``sigma/sqrt(N)`` — a deliberate departure ruled on
#1954. Boosts are deliberately not capped.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from . import spatial


#: The cross-position sigma at or below which a frequency keeps the whole depth
#: its strategy allows, dB. An alias for the room layer's own "medium
#: repeatability" threshold, not a second number.
TOLERABLE_STD_DB: float = spatial.MEDIUM_CONFIDENCE_STD_DB

#: Guards the division for a bin whose positions agree exactly (sigma 0).
_SIGMA_EPSILON_DB: float = 1e-6

#: Float slack, not a policy width: :func:`depth_fraction` returns exactly
#: ``1.0`` inside the tolerance, so an untouched bin is bit-identical.
PROTECTED_EPS_DB: float = 1e-9


def depth_fraction(std_db: Any) -> np.ndarray:
    """The fraction of a strategy's cut depth this spread supports, in [0, 1].

    ``min(1, TOLERABLE_STD_DB / max(sigma, eps))``, elementwise and band-free.
    Exactly ``1.0`` at or below the tolerance, so an ordinary room's allowed
    depth is bit-identical to the strategy's own scalar. Always an ``ndarray``,
    including 0-d for a scalar input.
    """
    sigma = np.asarray(std_db, dtype=np.float64)
    return np.asarray(
        np.minimum(1.0, TOLERABLE_STD_DB / np.maximum(sigma, _SIGMA_EPSILON_DB)),
        dtype=np.float64,
    )


def allowed_depth_db(std_db: Any, *, base_max_cut_db: float) -> np.ndarray:
    """Per-frequency cut floor, dB, non-positive, on ``std_db``'s own grid.

    ``base_max_cut_db`` is the strategy's scalar floor (negative). The result is
    the array :func:`jasper.audio_measurement.peq.design_peq` accepts as
    ``max_cut_db``.
    """
    return np.asarray(base_max_cut_db * depth_fraction(std_db), dtype=np.float64)


def protected_mask(depth_db: Any, *, base_max_cut_db: float) -> np.ndarray:
    """Where the spread genuinely REDUCED the allowance below the strategy's floor.

    The single definition of "this bin is protected". A bin the spread left
    alone is not protected. Always an ``ndarray`` (0-d for a scalar input).
    """
    return np.asarray(
        np.asarray(depth_db, dtype=np.float64) > base_max_cut_db + PROTECTED_EPS_DB
    )


def realized_overshoot_db(
    depth_cap_db: Any,
    realized_shift_db: Any,
    *,
    base_max_cut_db: float,
    band_mask: Any,
) -> float:
    """How far past its allowance the finished chain actually cuts, dB, worst bin.

    Measured per design over every protected in-band bin, from the whole PEQ
    chain's predicted shift, so it sees every filter's skirt. Returns ``0.0``
    when no protected bin is over its allowance. A residue exists because
    ``strategy._enforce_variance_depth_cap`` binds the allowance at protected
    filter CENTRES; what lands between centres is disclosed, not designed away.
    """
    cap = np.asarray(depth_cap_db, dtype=np.float64)
    realized = np.asarray(realized_shift_db, dtype=np.float64)
    band = np.asarray(band_mask, dtype=bool)
    mask = protected_mask(cap, base_max_cut_db=base_max_cut_db) & band
    if not mask.any():
        return 0.0
    return float(max(0.0, float(np.max(cap[mask] - realized[mask]))))


def no_correction_mask(
    depth_db: np.ndarray,
    *,
    min_filter_gain_db: float,
) -> np.ndarray:
    """Bins whose allowance cannot produce a filter the designer would keep.

    A bin qualifies when its allowed depth is shallower than the strategy's
    smallest worthwhile filter. The caller must REMOVE these bins from the
    designer's residual rather than pass the allowance through:
    :func:`jasper.audio_measurement.peq.design_peq` ``break``s out of its
    greedy loop when the tallest remaining peak clamps below
    ``min_filter_gain_db``, which would forfeit every stable peak underneath
    it. The tail only: a bin qualifies above 48 dB of cross-position sigma
    for ``safe``, 120 dB for
    ``balanced`` and 144 dB for ``assertive``.
    """
    return np.asarray(depth_db, dtype=np.float64) > -abs(min_filter_gain_db)


@dataclass(frozen=True)
class VarianceCapDisclosure:
    """What the cross-position depth cap did to one design's **ceiling**.

    A claim about the ceiling the designer worked under, not about the filters
    that came out: ``n_bins_capped`` counts in-band frequencies allowed less
    depth than the strategy alone would have allowed, not filters removed.
    Policy fields (``tolerable_std_db``, ``base_max_cut_db``) are always
    populated. Measurement fields are ``None`` when nothing was measured, never
    a neutral-looking zero — ``max_depth_forgone_db`` of ``0.0`` means the cap
    took nothing, ``None`` means no cap ran, and ``available``/``reason`` are
    the discriminator. ``filters_depth_trimmed`` and ``max_overshoot_db`` are
    the exception: both are facts about shipped filters, set by the caller that
    enforces the cap.
    """

    available: bool
    reason: str | None
    position_count: int
    tolerable_std_db: float
    base_max_cut_db: float
    n_bins: int = 0
    n_bins_capped: int = 0
    n_bins_no_cut: int = 0
    max_depth_forgone_db: float | None = None
    worst_freq_hz: float | None = None
    filters_depth_trimmed: int = 0
    max_overshoot_db: float | None = None

    @property
    def active(self) -> bool:
        """Whether the cap actually reduced the depth allowed anywhere in band.

        The room layer gates on this, so a run in which nothing was capped
        takes the identical code path it took before this cap existed.
        """
        return self.available and self.n_bins_capped > 0

    def with_enforcement(
        self,
        *,
        filters_depth_trimmed: int,
        max_overshoot_db: float,
    ) -> VarianceCapDisclosure:
        """A copy carrying what the caller's enforcement actually did."""
        return replace(
            self,
            filters_depth_trimmed=filters_depth_trimmed,
            max_overshoot_db=max_overshoot_db,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe disclosure record."""
        return {
            "available": self.available,
            "reason": self.reason,
            "position_count": self.position_count,
            "tolerable_std_db": self.tolerable_std_db,
            "base_max_cut_db": self.base_max_cut_db,
            "n_bins": self.n_bins,
            "n_bins_capped": self.n_bins_capped,
            "n_bins_no_cut": self.n_bins_no_cut,
            "max_depth_forgone_db": self.max_depth_forgone_db,
            "worst_freq_hz": self.worst_freq_hz,
            "filters_depth_trimmed": self.filters_depth_trimmed,
            "max_overshoot_db": self.max_overshoot_db,
        }


def _unavailable(
    reason: str,
    *,
    position_count: int,
    base_max_cut_db: float,
) -> VarianceCapDisclosure:
    return VarianceCapDisclosure(
        available=False,
        reason=reason,
        position_count=position_count,
        tolerable_std_db=TOLERABLE_STD_DB,
        base_max_cut_db=base_max_cut_db,
    )


def plan_depth_cap(
    matrix: spatial.SpatialMatrix | None,
    *,
    base_max_cut_db: float,
    min_filter_gain_db: float,
    band_mask: np.ndarray,
    unavailable_reason: str | None = None,
) -> tuple[np.ndarray | None, VarianceCapDisclosure]:
    """Plan one design run's per-frequency cut ceiling from cross-position spread.

    Returns ``(allowed_depth_db, disclosure)`` — the array to hand
    :func:`jasper.audio_measurement.peq.design_peq` as ``max_cut_db``, or
    ``None`` when no cap could be planned, in which case the disclosure says why.
    Refusing is never an error and never raises. ``base_max_cut_db`` must be
    deeper than ``min_filter_gain_db``, otherwise every frequency would be
    marked undesignable; that is refused instead. ``band_mask`` scopes the
    disclosure counts and never shapes the curve. Raises ``ValueError`` when
    ``band_mask`` does not match the matrix's grid — a caller bug, not a
    measurement outcome.
    """
    if matrix is None:
        return None, _unavailable(
            unavailable_reason or spatial.TOO_FEW_POSITIONS_REASON,
            position_count=0,
            base_max_cut_db=base_max_cut_db,
        )

    band = np.asarray(band_mask, dtype=bool)
    if band.shape != matrix.std_db.shape:
        raise ValueError(
            f"band_mask shape {band.shape} does not match the spatial "
            f"matrix grid {matrix.std_db.shape}"
        )

    positions = matrix.position_count
    if positions < spatial.MIN_POSITIONS_FOR_SPREAD:
        return None, _unavailable(
            spatial.TOO_FEW_POSITIONS_REASON,
            position_count=positions,
            base_max_cut_db=base_max_cut_db,
        )
    if base_max_cut_db > -abs(min_filter_gain_db):
        return None, _unavailable(
            "strategy designs no cuts",
            position_count=positions,
            base_max_cut_db=base_max_cut_db,
        )
    n_bins = int(band.sum())
    if n_bins == 0:
        return None, _unavailable(
            "no points in band",
            position_count=positions,
            base_max_cut_db=base_max_cut_db,
        )

    depth_db = allowed_depth_db(matrix.std_db, base_max_cut_db=base_max_cut_db)
    # Reduction in allowed depth, non-negative, on the same grid.
    forgone_db = abs(base_max_cut_db) - np.abs(depth_db)
    band_forgone = forgone_db[band]
    # `depth_fraction` returns exactly 1.0 at or below the knee, so no epsilon
    # is needed to tell "capped" from "not capped".
    capped = band_forgone > 0.0
    n_capped = int(capped.sum())
    no_cut = no_correction_mask(depth_db, min_filter_gain_db=min_filter_gain_db)
    worst_freq_hz: float | None = None
    if n_capped:
        band_freqs = matrix.freqs_hz[band]
        worst_freq_hz = float(band_freqs[int(np.argmax(band_forgone))])

    return depth_db, VarianceCapDisclosure(
        available=True,
        reason=None,
        position_count=positions,
        tolerable_std_db=TOLERABLE_STD_DB,
        base_max_cut_db=base_max_cut_db,
        n_bins=n_bins,
        n_bins_capped=n_capped,
        n_bins_no_cut=int((no_cut & band).sum()),
        max_depth_forgone_db=float(np.max(band_forgone)),
        worst_freq_hz=worst_freq_hz,
    )
