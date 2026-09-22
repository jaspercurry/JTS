# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Audibility co-metrics and measured directivity (ADR-0202)."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.crossover_v2.feature_classifier import load_round_pose_curves
from jasper.active_speaker.flat_spec_views import DirectivityTable, directivity_table
from jasper.audio_measurement.olive_metrics import nbd_and_sm

from .banked import DEFAULT_PRIMARY_ROLE, BankedRound

#: The lateral-walk curve role this view pools onto the on-axis curve: the
#: composed acoustic response, not one driver's isolated branch. A local literal
#: on :data:`DEFAULT_PRIMARY_ROLE`'s own precedent.
_SUMMED_CURVE_ROLE = "summed"

_ON_AXIS_POSITION_UNAVAILABLE = (
    f"this round banked no {DEFAULT_PRIMARY_ROLE!r}-role cloud position"
)
_POOLED_WINDOW_UNAVAILABLE = (
    f"this round's lateral walk banked no {_SUMMED_CURVE_ROLE!r}-role curve "
    "at any bearing"
)


@dataclass(frozen=True)
class PooledWindowResult:
    """:func:`pooled_window_horizontal`'s output curve, plus its own provenance.

    NOT CTA-2034's "listening window": that average includes vertical poses this
    rig does not capture, and the name is deliberate (ADR-0202).
    This is the power average of whatever horizontal bearings — 0/±7/±22° or
    fewer — the round's lateral walk banked a :data:`_SUMMED_CURVE_ROLE` curve
    for. ``bearings_deg`` discloses the round's own coverage rather than assuming
    it complete, and ``n_curves`` never counts a superseded retake.
    """

    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    bearings_deg: tuple[float, ...]
    n_curves: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "freqs_hz": self.freqs_hz.tolist(),
            "magnitude_db": self.magnitude_db.tolist(),
            "bearings_deg": list(self.bearings_deg),
            "n_curves": self.n_curves,
        }


def pooled_window_horizontal(
    bundle_dir: Path, *, grid_hz: np.ndarray,
) -> PooledWindowResult | None:
    """Power-average the round's banked SUMMED lateral-pose curves.

    **Reused, not re-walked.** :func:`~.feature_classifier.load_round_pose_curves`
    is the same banked-curve reader every other lateral-pose consumer uses; this
    function only adds the ``role == "summed"`` filter and the power-average.

    **Power-averaged, never dB-averaged** — a dB mean over-emphasises deep
    nulls. This reduction is across CURVES at one frequency rather than across
    frequencies within one curve, so it is not a call to
    ``analysis.smooth_fractional_octave``. Per curve: resampled onto ``grid_hz``
    and masked to the curve's OWN driven ``band_hz``, since a point outside it
    was never measured. Distinct stops sharing a bearing are power-averaged
    together FIRST, so a bearing visited three times cannot outweigh one visited
    once; a RETAKE is not such a repeat, the reader having already superseded
    the older attempts.

    Returns ``None`` when the lateral walk banked no :data:`_SUMMED_CURVE_ROLE`
    curve at ANY bearing; the absence is disclosed by the caller, never
    fabricated.
    """

    grid = np.asarray(grid_hz, dtype=float)
    by_bearing: dict[float, list[np.ndarray]] = {}
    for curve in load_round_pose_curves(Path(bundle_dir)):
        # A raised pose is SKIPPED rather than given its own bucket: an elevated
        # seat sharing a bearing with a mark-height one is a different
        # measurement, not a repeat visit to the same stop.
        if (
            curve.role != _SUMMED_CURVE_ROLE
            or curve.position_deg is None
            or curve.vertical_deg
        ):
            continue
        resampled_db = np.interp(grid, curve.freqs_hz, curve.magnitude_db)
        in_band = (grid >= curve.band_hz[0]) & (grid <= curve.band_hz[1])
        power = np.where(in_band, 10.0 ** (resampled_db / 10.0), np.nan)
        by_bearing.setdefault(float(curve.position_deg), []).append(power)
    if not by_bearing:
        return None

    # A grid point outside every contributing curve's own band is legitimate:
    # ``np.nanmean`` of an all-NaN slice is the correct "no bearing covered this
    # frequency" answer. Only the WARNINGS are silenced — numpy's "Mean of empty
    # slice" comes through the stdlib ``warnings`` module, and "invalid value
    # encountered in log10" needs ``errstate`` for the same NaN.
    n_curves = sum(len(powers) for powers in by_bearing.values())
    bearing_means = []
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for _deg, powers in sorted(by_bearing.items()):
            bearing_means.append(np.nanmean(np.stack(powers, axis=0), axis=0))
        pooled_power = np.nanmean(np.stack(bearing_means, axis=0), axis=0)
        pooled_db = 10.0 * np.log10(np.maximum(pooled_power, 1e-12))
    return PooledWindowResult(
        freqs_hz=grid,
        magnitude_db=pooled_db,
        bearings_deg=tuple(sorted(by_bearing)),
        n_curves=n_curves,
    )


@dataclass(frozen=True)
class AudibilityMetrics:
    """NBD + SM (Olive 2004 / US 8,311,232 B2) for ONE curve.

    A co-metric (ADR-0202 rule 2): it informs a graded round and never
    gates or vetoes it — ``flat_spec.SPEC_BANDS`` stays the sole acceptance
    metric.
    """

    nbd_db: float
    sm_r2: float
    band_hz: tuple[float, float]
    smoothing_fraction: int
    input_smoothing_fraction: int | None

    @classmethod
    def compute(
        cls,
        freqs_hz: np.ndarray,
        magnitude_db: np.ndarray,
        band_hz: tuple[float, float],
        *,
        input_smoothing_fraction: int | None = None,
    ) -> "AudibilityMetrics":
        nbd_result, sm_result = nbd_and_sm(
            freqs_hz, magnitude_db, band_hz,
            input_smoothing_fraction=input_smoothing_fraction,
        )
        return cls(
            nbd_db=nbd_result.nbd_db,
            sm_r2=sm_result.sm_r2,
            band_hz=nbd_result.band_hz,
            smoothing_fraction=nbd_result.smoothing_fraction,
            input_smoothing_fraction=nbd_result.input_smoothing_fraction,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nbd_db": self.nbd_db,
            "sm_r2": self.sm_r2,
            "band_hz": list(self.band_hz),
            "smoothing_fraction": self.smoothing_fraction,
            "input_smoothing_fraction": self.input_smoothing_fraction,
        }


@dataclass(frozen=True)
class AudibilityCoMetrics:
    """NBD + SM on both curves ADR-0202 names, for one graded round.

    ``on_axis`` / ``pooled_window`` are ``None`` exactly when their own
    ``*_reason`` is non-empty. A round missing one lens is not an unreadable
    round: co-metrics inform and never gate (ADR-0202 rule 2).

    ``on_axis`` is NBD/SM on ``banked.positions``' own
    :data:`DEFAULT_PRIMARY_ROLE` curve(s), power-averaged when the round banked
    more than one; ``pooled_window_bearings_deg`` is ``()`` when there is no
    pooled window.
    """

    round_dir: str
    on_axis: AudibilityMetrics | None
    on_axis_reason: str
    pooled_window: AudibilityMetrics | None
    pooled_window_reason: str
    pooled_window_bearings_deg: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_dir": self.round_dir,
            "on_axis": None if self.on_axis is None else self.on_axis.to_dict(),
            "on_axis_reason": self.on_axis_reason,
            "pooled_window": (
                None if self.pooled_window is None else self.pooled_window.to_dict()
            ),
            "pooled_window_reason": self.pooled_window_reason,
            "pooled_window_bearings_deg": list(self.pooled_window_bearings_deg),
        }


def audibility_co_metrics(
    banked: BankedRound, *, band_hz: tuple[float, float] | None = None,
) -> AudibilityCoMetrics:
    """NBD + SM on the on-axis curve and the pooled horizontal window, for one
    graded round (ADR-0202).

    A co-metric surface, additive beside the round's grade: ``banked.report`` is
    read once, for its own ``graded_band_hz`` default, and never touched again.
    ``band_hz`` defaults to that same span, so a co-metric and the grade beside
    it describe the same stretch of spectrum.
    """

    band = banked.graded_report.graded_band_hz if band_hz is None else band_hz

    on_axis_positions = [
        position for position in banked.graded_positions
        if position.role == DEFAULT_PRIMARY_ROLE
    ]
    on_axis_metrics: AudibilityMetrics | None
    on_axis_reason: str
    if not on_axis_positions:
        on_axis_metrics, on_axis_reason = None, _ON_AXIS_POSITION_UNAVAILABLE
    else:
        # Power-averaged on the same convention as everywhere else in this
        # module — a no-op when there is exactly one, the ordinary case.
        power = np.mean(
            [10.0 ** (p.magnitude_db / 10.0) for p in on_axis_positions], axis=0,
        )
        on_axis_db = 10.0 * np.log10(np.maximum(power, 1e-12))
        on_axis_metrics = AudibilityMetrics.compute(
            banked.curve_grid_hz, on_axis_db, band,
            # The coarsest attested fraction among the contributing curves —
            # smaller N is coarser, and the coarser pass is the one that
            # already averaged ripple away (olive_metrics' module docstring).
            input_smoothing_fraction=min(
                p.smoothing_fraction for p in on_axis_positions
            ),
        )
        on_axis_reason = ""

    pooled = pooled_window_horizontal(banked.session_dir, grid_hz=banked.curve_grid_hz)
    pooled_metrics: AudibilityMetrics | None
    pooled_reason: str
    bearings: tuple[float, ...]
    if pooled is None:
        pooled_metrics, pooled_reason, bearings = None, _POOLED_WINDOW_UNAVAILABLE, ()
    else:
        # The lateral bank attests no smoothing fraction on its curves, so
        # None ("unknown") is the honest statement here — never a guess.
        pooled_metrics = AudibilityMetrics.compute(pooled.freqs_hz, pooled.magnitude_db, band)
        pooled_reason, bearings = "", pooled.bearings_deg

    return AudibilityCoMetrics(
        round_dir=str(banked.round_dir),
        on_axis=on_axis_metrics,
        on_axis_reason=on_axis_reason,
        pooled_window=pooled_metrics,
        pooled_window_reason=pooled_reason,
        pooled_window_bearings_deg=bearings,
    )


def directivity_view(banked: BankedRound) -> DirectivityTable:
    """This round's cloud seats as departures from their on-axis reference.

    Each band's level difference and residual shape come from
    :func:`~jasper.active_speaker.flat_spec_views.directivity_table`.
    This is not sound-power DI; a shared trim leaves the difference unchanged.
    Observed only: no grade moves.

    A round banked before the seat bearings were written still answers, as a
    table with ``angles_recorded`` false and every ``degrees`` ``None``:
    role-labelled directivity is a narrower reading, not an unreadable round.
    """
    return directivity_table(
        banked.graded_report,
        banked.graded_positions,
        reference_role=DEFAULT_PRIMARY_ROLE,
    )
