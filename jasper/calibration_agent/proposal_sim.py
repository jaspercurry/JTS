# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic simulate-and-reject gate for LLM correction proposals.

This is the safety core of P6's confirm-gated proposer. An LLM may
*propose* a room-correction filter set (bounded, schema-validated by
:mod:`jasper.calibration_agent.response`), but before anything is applied
this module SIMULATES it deterministically and REJECTS it if it would
ring or exceed headroom — and then judges it with the **same P4
acceptance evaluator** any correction faces on re-measure. The LLM gets
no special trust: a proposal it likes is applied only if deterministic
code agrees it improves the measured room and stays safe.

The pipeline, per proposal:

1. **Bounds** (already done by ``response.validate_advisor_response``):
   per-filter freq/Q/gain within the active strategy caps, cuts-only
   default, boost-stacking headroom. Re-asserted here defensively.
2. **Ring / regularization** — an AutoEQ-style steep-positive-gain
   guard: a narrow, high-gain BOOST has a long resonant tail (audible
   pre/post-ringing). Reject any boost whose Q exceeds a gain-scaled
   ceiling. Cuts are never ring-rejected (a cut removes energy).
3. **Headroom** — total stacked positive boost must stay within the
   strategy's ``max_total_boost_db`` ceiling (0 dB on the shipped
   strategies), mirroring ``jasper.correction.peq.total_max_boost_db``.
4. **Predicted response** — ``peq.predicted_response`` gives the dB shift;
   the predicted post-correction curve is ``before + shift``.
5. **Acceptance verdict** — the predicted curve is fed to
   ``jasper.correction.acceptance.evaluate_acceptance`` exactly as a real
   verify would be (before=position-1 baseline, verify=predicted,
   target=target). ``accept`` / ``surface`` may proceed to the
   user-confirm gate; a ``revert``-class verdict is rejected here.

The verdict on the *simulated* curve is advisory-optimistic (the room's
real re-measurement remains the true judge post-apply, closing the
loop). But rejecting a proposal that even the noise-free simulation says
regresses the room stops an obviously-bad apply before it touches
CamillaDSP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from jasper.correction import acceptance as _acceptance
from jasper.correction import peq as _peq

# --- Ring / regularization guard --------------------------------------
#
# A peaking boost's resonant tail lengthens with Q. Empirically (and per
# AutoEQ's max-gain discipline) a boost above a few dB with a high Q is
# where audible ringing starts. We cap a positive-gain filter's Q by a
# gain-scaled ceiling: small boosts may be a little narrower, large
# boosts must be gentle. These are conservative env-tunable placeholders
# (revision plan §5 H1 retunes from on-device listening); out-of-range or
# unparseable env values fall back to the documented default.
RING_GUARD_BASE_Q = 2.0        # a +0 dB boost may be up to this Q
RING_GUARD_Q_PER_DB = 0.35     # each dB of boost tightens the Q ceiling
RING_GUARD_MIN_Q = 1.0         # never demand narrower than this
# Cuts are not ring-limited, but a pathologically narrow cut is still
# poor practice; this is a generous upper clamp shared with the strategy
# q_max in practice, kept here as a defensive backstop only.


@dataclass(frozen=True)
class SimIssue:
    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class SimResult:
    """Outcome of simulating one proposed correction filter set.

    ``accepted`` is the deterministic go/no-go for offering the
    user-confirm apply. ``acceptance`` is the P4 verdict dict on the
    simulated curve (``None`` when the sim could not run, e.g. missing
    baseline curves). ``predicted_curve`` is the simulated post-correction
    magnitude for the UI's before/after preview.
    """

    accepted: bool
    issues: tuple[SimIssue, ...]
    acceptance: dict[str, Any] | None
    total_boost_db: float
    predicted_curve: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "issues": [i.to_dict() for i in self.issues],
            "acceptance": self.acceptance,
            "total_boost_db": round(self.total_boost_db, 3),
            "predicted_curve": self.predicted_curve,
        }


def ring_guard_q_ceiling(gain_db: float) -> float:
    """The maximum Q a positive-gain boost of ``gain_db`` may have before
    it is judged to ring. Monotonically tightens with gain; floored at
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
    if curve is None:
        return None
    freqs = getattr(curve, "freqs_hz", None)
    mags = getattr(curve, "magnitude_db", None)
    if freqs is None and isinstance(curve, dict):
        freqs = curve.get("freqs_hz")
        mags = curve.get("magnitude_db")
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
    thresholds: "_acceptance.AcceptanceThresholds | None" = None,
) -> SimResult:
    """Simulate a proposed correction filter set and return the verdict.

    ``measured`` is the measured curve the proposal's response is applied
    to. ``baseline`` is the position-1 (or measured) curve the acceptance
    evaluator uses as the "before"; ``target`` the target. All three are
    CurveJSON-ish (dicts or objects with ``freqs_hz`` / ``magnitude_db``).
    The proposal is simulated on ``measured``'s own grid.

    Rejects on: a ring-unsafe boost, a headroom overflow, or a
    ``revert``-class acceptance verdict on the simulated curve. Never
    raises for a bad proposal — it returns ``accepted=False`` with
    issues.
    """
    issues: list[SimIssue] = []

    if not isinstance(peqs, list) or not peqs:
        return SimResult(
            accepted=False,
            issues=(SimIssue("empty_proposal", "no filters proposed"),),
            acceptance=None,
            total_boost_db=0.0,
            predicted_curve=None,
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
            accepted=False,
            issues=tuple(issues),
            acceptance=None,
            total_boost_db=total_boost,
            predicted_curve=None,
        )

    grid, measured_db = measured_pair
    peq_objs = _as_peq_objects(peqs)
    shift = _peq.predicted_response(peq_objs, grid)
    predicted = measured_db + shift
    predicted_curve = {
        "freqs_hz": [round(float(x), 4) for x in grid.tolist()],
        "magnitude_db": [round(float(x), 4) for x in predicted.tolist()],
    }

    acceptance_dict: dict[str, Any] | None = None
    baseline_pair = _curve_arrays(baseline)
    target_pair = _curve_arrays(target)
    if baseline_pair is not None and target_pair is not None:
        try:
            before_on_grid = _resample(baseline_pair[0], baseline_pair[1], grid)
            target_on_grid = _resample(target_pair[0], target_pair[1], grid)
            result = _acceptance.evaluate_acceptance(
                freqs=grid,
                before_db=before_on_grid,
                verify_db=predicted,
                target_db=target_on_grid,
                f_high=f_high_hz,
                basis="simulation",
                thresholds=thresholds,
            )
            acceptance_dict = result.to_dict()
            if result.verdict in (
                _acceptance.Verdict.REVERT,
                _acceptance.Verdict.REVERT_PENDING_CONFIRM,
            ):
                issues.append(SimIssue(
                    "simulation_regresses_room",
                    (
                        "the noise-free simulation says this proposal would "
                        f"make the room worse ({result.verdict.value}); "
                        "rejecting before apply"
                    ),
                    {"verdict": result.verdict.value},
                ))
        except (
            ValueError,
            IndexError,
            TypeError,
            ZeroDivisionError,
            FloatingPointError,
        ) as e:
            # A degenerate curve (empty band, NaN, mismatched grid) must
            # never crash the endpoint; surface it as a rejection instead.
            issues.append(SimIssue(
                "simulation_failed",
                f"could not evaluate the proposal: {type(e).__name__}",
            ))

    accepted = not issues
    return SimResult(
        accepted=accepted,
        issues=tuple(issues),
        acceptance=acceptance_dict,
        total_boost_db=total_boost,
        predicted_curve=predicted_curve,
    )


def _resample(
    src_freqs: np.ndarray,
    src_mags: np.ndarray,
    dst_freqs: np.ndarray,
) -> np.ndarray:
    """Linear-in-log-frequency resample of ``src_mags`` onto ``dst_freqs``.

    Matches the shape of what the session does before feeding the
    acceptance evaluator (both curves onto one grid). Endpoints hold
    flat outside the source range.
    """
    if np.array_equal(src_freqs, dst_freqs):
        return src_mags.astype(np.float64)
    src_log = np.log10(np.maximum(src_freqs, 1e-9))
    dst_log = np.log10(np.maximum(dst_freqs, 1e-9))
    order = np.argsort(src_log)
    return np.interp(dst_log, src_log[order], src_mags[order]).astype(np.float64)
