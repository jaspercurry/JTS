# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic disclosure for LLM correction proposals.

An LLM may propose a room-correction filter set (schema- and
bounds-validated by :mod:`jasper.calibration_agent.response`). This
module simulates that set deterministically and reports what it
predicts: the post-correction curve, its predicted deviation-from-target
improvement, each boost's Q against the ring-guard ceiling, and the
summed positive boost against the strategy's headroom ceiling.

It refuses nothing. The constraints that actually hold the apply path
are the strategy caps in ``response.validate_advisor_response``, the
explicit user confirm at ``/propose/apply``, and the emitter, which
re-clips headroom when the correction is applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from jasper.audio_measurement import peq as _peq
from jasper.audio_measurement.analysis import deviation_metrics

from .curves import curve_values

# --- Ring disclosure --------------------------------------------------
#
# A peaking boost's resonant tail lengthens with Q. Empirically (and per
# AutoEQ's max-gain discipline) a boost above a few dB with a high Q is
# where audible ringing starts, so a positive-gain filter's Q is
# reported against a gain-scaled ceiling. These are conservative
# placeholder constants — revision plan §5 H1 retunes them from
# on-device listening.
RING_GUARD_BASE_Q = 2.0        # a +0 dB boost may be up to this Q
RING_GUARD_Q_PER_DB = 0.35     # each dB of boost tightens the Q ceiling
RING_GUARD_MIN_Q = 1.0         # never demand narrower than this


@dataclass(frozen=True)
class SimIssue:
    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class SimResult:
    """What simulating one proposed correction filter set predicts.

    Disclosure only — no field here refuses an apply. ``issues`` are the
    notes worth surfacing (a ring-ceiling overage, a headroom overage, a
    curve too degenerate to simulate). ``predicted_curve`` is the
    simulated post-correction magnitude for the UI's before/after
    preview, and ``predicted_rms_delta_db`` how much closer to target
    that curve is predicted to sit (positive = predicted improvement);
    both are ``None`` when the needed curves were absent.
    """

    issues: tuple[SimIssue, ...]
    total_boost_db: float
    max_total_boost_db: float
    predicted_curve: dict[str, Any] | None
    predicted_rms_delta_db: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "total_boost_db": round(self.total_boost_db, 3),
            "max_total_boost_db": round(self.max_total_boost_db, 3),
            "predicted_curve": self.predicted_curve,
            "predicted_rms_delta_db": (
                None if self.predicted_rms_delta_db is None
                else round(self.predicted_rms_delta_db, 2)
            ),
        }


def ring_guard_q_ceiling(gain_db: float) -> float:
    """The Q above which a positive-gain boost of ``gain_db`` is reported
    as ring-prone. Monotonically tightens with gain; floored at
    :data:`RING_GUARD_MIN_Q`."""
    ceiling = RING_GUARD_BASE_Q - RING_GUARD_Q_PER_DB * max(0.0, gain_db)
    return max(RING_GUARD_MIN_Q, ceiling)


def _ring_issues(peqs: list[dict[str, float]]) -> list[SimIssue]:
    issues: list[SimIssue] = []
    for i, band in enumerate(peqs):
        gain = float(band.get("gain_db", 0.0))
        q = float(band.get("q", 1.0))
        if gain <= 0.0:
            continue  # cuts do not ring
        ceiling = ring_guard_q_ceiling(gain)
        if q > ceiling + 1e-9:
            issues.append(SimIssue(
                "boost_would_ring",
                (
                    f"filter {i}: a +{gain:.1f} dB boost at Q {q:.2f} exceeds "
                    f"the ring-safe Q ceiling {ceiling:.2f} for that gain — "
                    "narrow high-gain boosts ring"
                ),
                {"band_index": i, "gain_db": gain, "q": q, "q_ceiling": ceiling},
            ))
    return issues


def _headroom_issue(
    peqs: list[dict[str, float]],
    max_total_boost_db: float,
) -> tuple[float, SimIssue | None]:
    total = sum(
        float(b.get("gain_db", 0.0))
        for b in peqs
        if float(b.get("gain_db", 0.0)) > 0.0
    )
    if total > max_total_boost_db + 1e-9:
        return total, SimIssue(
            "boost_stack_exceeds_headroom",
            (
                f"summed positive boost {total:.2f} dB exceeds the "
                f"{max_total_boost_db:.2f} dB headroom ceiling"
            ),
            {"total_boost_db": round(total, 3)},
        )
    return total, None


def _as_peq_objects(peqs: list[dict[str, float]]) -> list[_peq.PEQ]:
    return [
        _peq.PEQ(
            freq=float(b["freq_hz"]),
            q=float(b["q"]),
            gain=float(b["gain_db"]),
        )
        for b in peqs
    ]


def _curve_arrays(curve: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """Coerce a CurveJSON-ish object/dict into (freqs, mags) arrays."""
    values = curve_values(curve)
    if values is None:
        return None
    freqs, mags = values
    if not isinstance(freqs, (list, tuple, np.ndarray)):
        return None
    if not isinstance(mags, (list, tuple, np.ndarray)):
        return None
    f = np.asarray(freqs, dtype=np.float64)
    m = np.asarray(mags, dtype=np.float64)
    n = min(f.shape[0], m.shape[0])
    if n < 3:
        return None
    return f[:n], m[:n]


def simulate_correction_proposal(
    peqs: list[dict[str, float]],
    *,
    measured: Any,
    baseline: Any,
    target: Any,
    max_total_boost_db: float = 0.0,
    f_high_hz: float = 350.0,
) -> SimResult:
    """Simulate a proposed correction filter set and report what it predicts.

    ``measured`` is the measured curve the proposal's response is applied
    to. ``baseline`` is the position-1 (or measured) curve the predicted
    improvement is measured against; ``target`` the target. All three are
    CurveJSON-ish (dicts or objects with ``freqs_hz`` / ``magnitude_db``).
    The proposal is simulated on ``measured``'s own grid.

    Never raises for a bad proposal and never refuses one: a filter set
    that would ring or overflow headroom comes back with that note in
    ``issues``, and curves too degenerate to simulate leave the predicted
    fields ``None``.
    """
    issues: list[SimIssue] = []

    if not isinstance(peqs, list) or not peqs:
        return SimResult(
            issues=(SimIssue("empty_proposal", "no filters proposed"),),
            total_boost_db=0.0,
            max_total_boost_db=max_total_boost_db,
            predicted_curve=None,
            predicted_rms_delta_db=None,
        )

    issues.extend(_ring_issues(peqs))
    total_boost, headroom_issue = _headroom_issue(peqs, max_total_boost_db)
    if headroom_issue is not None:
        issues.append(headroom_issue)

    measured_pair = _curve_arrays(measured)
    if measured_pair is None:
        # Can't simulate without a measured curve.
        issues.append(SimIssue(
            "missing_measured_curve",
            "no measured curve available to simulate the proposal against",
        ))
        return SimResult(
            issues=tuple(issues),
            total_boost_db=total_boost,
            max_total_boost_db=max_total_boost_db,
            predicted_curve=None,
            predicted_rms_delta_db=None,
        )

    grid, measured_db = measured_pair
    peq_objs = _as_peq_objects(peqs)
    shift = _peq.predicted_response(peq_objs, grid)
    predicted = measured_db + shift
    predicted_curve = {
        "freqs_hz": [round(float(x), 4) for x in grid.tolist()],
        "magnitude_db": [round(float(x), 4) for x in predicted.tolist()],
    }

    rms_delta: float | None = None
    baseline_pair = _curve_arrays(baseline)
    target_pair = _curve_arrays(target)
    if baseline_pair is not None and target_pair is not None:
        try:
            before_on_grid = _resample(baseline_pair[0], baseline_pair[1], grid)
            target_on_grid = _resample(target_pair[0], target_pair[1], grid)
            # Both sides read through the shared deviation metric over one
            # band, so the difference compares like with like.
            rms_delta = (
                deviation_metrics(
                    before_on_grid, target_on_grid, grid, f_high=f_high_hz,
                )["rms_db"]
                - deviation_metrics(
                    predicted, target_on_grid, grid, f_high=f_high_hz,
                )["rms_db"]
            )
        except (
            ValueError,
            IndexError,
            TypeError,
            ZeroDivisionError,
            FloatingPointError,
        ) as e:
            # A degenerate curve (empty band, NaN, mismatched grid) must
            # never crash the endpoint; disclose it instead.
            issues.append(SimIssue(
                "simulation_failed",
                f"could not predict the improvement: {type(e).__name__}",
            ))

    return SimResult(
        issues=tuple(issues),
        total_boost_db=total_boost,
        max_total_boost_db=max_total_boost_db,
        predicted_curve=predicted_curve,
        predicted_rms_delta_db=rms_delta,
    )


def _resample(
    src_freqs: np.ndarray,
    src_mags: np.ndarray,
    dst_freqs: np.ndarray,
) -> np.ndarray:
    """Linear-in-log-frequency resample of ``src_mags`` onto ``dst_freqs``.

    Matches the shape of what the session does before comparing two
    curves (both onto one grid). Endpoints hold flat outside the source
    range.
    """
    if np.array_equal(src_freqs, dst_freqs):
        return src_mags.astype(np.float64)
    src_log = np.log10(np.maximum(src_freqs, 1e-9))
    dst_log = np.log10(np.maximum(dst_freqs, 1e-9))
    order = np.argsort(src_log)
    return np.interp(dst_log, src_log[order], src_mags[order]).astype(np.float64)
