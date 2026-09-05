# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the speaker would measure, for a candidate nothing has played.

Offline simulated evaluation over a round's BANKED per-driver solos — no build,
no tone, no device — and nothing here ranks or scores (invariants 2 and 3).
THE LANDMINE: every banked solo is windowed at its OWN direct peak, so the
physical inter-driver arrival gap is already out of the banked pair and
:attr:`SummationCandidate.residual_delay_us` is a residual in the ANALYSIS
frame; its one derivation is
``program_analysis.summed_model_residual_delay_us``, never a hand subtraction.
A candidate carries no crossover sections: a banked solo already contains
whatever the emitted graph ran during its capture. Units are in every name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .contracts import DRIVER_ROLE_TWEETER, DRIVER_ROLE_WOOFER
from .plan_assembly import SummationFrame, compose_linearized_prediction
from .position_cycle import parse_curve_complex, read_pose_curve_pair

PREDICTION_KIND = "jts_forward_model_prediction"
PREDICTION_SCHEMA_VERSION = 1

REFUSAL_UNSUPPORTED = "forward_model_unsupported"
REFUSAL_GRID_DISAGREES = "forward_model_branch_grids_disagree"

#: Nothing measured judged this prediction. What a bare prediction always is:
#: the model computed a curve and no capture ever contradicted it.
ACCEPTANCE_NOT_RUN = "not_run"

#: A banked measurement judged it, and ``judged_against`` names which one.
ACCEPTANCE_JUDGED = "judged_against_measured"


def acceptance_block(judged_against: str | None) -> dict[str, Any]:
    """Whether a measurement judged this prediction, and which one.

    ``judged_against`` is the measured comparand's own identity — the banked
    round whose VERIFY sum the prediction was deltaed against — or ``None``,
    which is what a prediction nothing measured is. Disclosure, never a gate
    and never a grade: this module ships no acceptance tolerance (#3481).
    """
    return {
        "status": ACCEPTANCE_JUDGED if judged_against else ACCEPTANCE_NOT_RUN,
        "judged_against": judged_against,
    }


class ForwardModelError(ValueError):
    """The banked curves cannot support a predicted sum.

    ``refusal_reason`` and ``detail`` are the contract; the message is operator
    copy and may be reworded freely.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = REFUSAL_UNSUPPORTED,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal_reason = reason
        self.detail: dict[str, Any] = dict(detail or {})


@dataclass(frozen=True)
class SummationCandidate:
    """One proposal's variable axes, in the emitted graph's own vocabulary.

    ``filters_by_role``
        Each branch's linearization biquads as the PERSISTED
        ``{biquad_type, freq, q, gain}`` records the emitter re-validates. A
        role with no entry is corrected by unity.
    ``trim_db_by_role``
        The emitted per-driver trim in dB. A role with no entry trims by 0.
    ``polarity_sign``
        ``+1`` or ``-1``, applied to the TWEETER branch, matching
        ``program_analysis.predicted_branch_sum``'s own frame.
    ``residual_delay_us``
        Signed RESIDUAL delay in the analysis frame, never an applied delay.
        See the module docstring's landmine.

    Frozen and EQUAL by content. NOT hashable: the mapping fields are ordinarily
    ``dict``, so the generated ``__hash__`` raises on call — deduplicate with
    ``==`` or a list, never a ``set``.
    """

    filters_by_role: Mapping[str, Sequence[Mapping[str, Any]]] = field(
        default_factory=dict
    )
    trim_db_by_role: Mapping[str, float] = field(default_factory=dict)
    polarity_sign: int = 1
    residual_delay_us: float = 0.0


@dataclass(frozen=True)
class BranchPair:
    """One round's two banked solos, on one grid, ready to sum.

    ``band_hz_by_role`` is each driver's own SWEPT band, which is NOT the grid.
    Outside its swept band a driver's sample is noise rather than a small
    response, so :func:`predict_sum` contributes exactly zero from it.

    ``take_path`` names the take both curves were read from — both roles must
    ride ONE take, since two captures would be summed across whatever moved
    between them.
    """

    freqs_hz: np.ndarray
    woofer_role: str
    tweeter_role: str
    woofer_tf: np.ndarray
    tweeter_tf: np.ndarray
    band_hz_by_role: Mapping[str, tuple[float, float]]
    take_path: str

    def driven(self, role: str) -> np.ndarray:
        """Boolean mask of the bins this driver was actually excited at."""
        lo_hz, hi_hz = self.band_hz_by_role[role]
        return (self.freqs_hz >= float(lo_hz)) & (self.freqs_hz <= float(hi_hz))

    @property
    def sum_band_hz(self) -> tuple[float, float]:
        """The union of the two swept bands — where the sum says anything."""
        lows, highs = zip(*self.band_hz_by_role.values())
        return (float(min(lows)), float(max(highs)))


@dataclass(frozen=True)
class PredictedSum:
    """One candidate's predicted summed magnitude, and what it was read on.

    ``predicted_db`` is on ``freqs_hz``, the banked pair's own shared grid.
    Outside :attr:`sum_band_hz` neither driver was swept and the prediction is
    the floor rather than a response — a reader compares inside that band.
    Whether a measurement judged it is the CONTAINING record's field, since
    :func:`predict_sum` compares against nothing and cannot answer it.
    """

    freqs_hz: np.ndarray
    predicted_db: np.ndarray
    sum_band_hz: tuple[float, float]
    take_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "kind": PREDICTION_KIND,
            "freqs_hz": [float(hz) for hz in self.freqs_hz],
            "predicted_db": [float(db) for db in self.predicted_db],
            "sum_band_hz": [float(edge) for edge in self.sum_band_hz],
            "take_path": self.take_path,
        }


def candidate_from_json(
    path: str | Path | None,
    *,
    polarity_sign: int | None = None,
    residual_delay_us: float | None = None,
) -> SummationCandidate:
    """One candidate from its JSON source plus the two single-value overrides.

    No ``path`` is an uncorrected, untrimmed, in-phase pair at zero residual
    delay. The two overrides WIN over the file when given, so a held-EQ
    postdiction varies only the delay: the same candidate file, one flag moved.
    Raises ``OSError``, ``ValueError`` or ``TypeError`` for a source that is not
    a readable JSON object of the shape :class:`SummationCandidate` documents;
    every message names ``path``, since the operator has more than one file open.
    """
    raw: Mapping[str, Any] = {}
    if path is not None:
        try:
            loaded = json.loads(Path(path).read_text())
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{path}: candidate JSON must be an object")
        raw = loaded
    filters = raw.get("filters_by_role") or {}
    trims = raw.get("trim_db_by_role") or {}
    if not isinstance(filters, Mapping) or not isinstance(trims, Mapping):
        raise ValueError(
            f"{path}: filters_by_role and trim_db_by_role must be objects"
        )
    return SummationCandidate(
        filters_by_role={
            str(role): list(entries) for role, entries in filters.items()
        },
        trim_db_by_role={str(role): float(db) for role, db in trims.items()},
        polarity_sign=int(
            raw.get("polarity_sign", 1) if polarity_sign is None else polarity_sign
        ),
        residual_delay_us=float(
            raw.get("residual_delay_us", 0.0)
            if residual_delay_us is None
            else residual_delay_us
        ),
    )


def load_branch_pair(
    bundle_dir: Path, *, phase: str, position_deg: int
) -> BranchPair | None:
    """The latest banked take carrying BOTH roles, as complex transfers.

    ``None`` when no take at this pose carries both roles, or when one of them
    does not parse — ordinary shapes for a round that measured one driver,
    never an error. A take that carries both roles on DISAGREEING grids raises
    instead: that is a defect rather than an absence, and folding it into the
    same ``None`` would hide it.

    No cache: a 40-take bank walks in ~7 ms, and the returned pair
    is reused across candidates at ~0.12 ms per :func:`predict_sum`.
    """
    found = read_pose_curve_pair(
        Path(bundle_dir),
        phase=phase,
        position_deg=position_deg,
        roles=(DRIVER_ROLE_WOOFER, DRIVER_ROLE_TWEETER),
    )
    if found is None:
        return None
    woofer_curve, tweeter_curve, take_path = found
    woofer = parse_curve_complex(woofer_curve)
    tweeter = parse_curve_complex(tweeter_curve)
    if woofer is None or tweeter is None:
        return None
    if not np.array_equal(woofer[0], tweeter[0]):
        # VALUES, not shape. Two grids of equal length over different abscissae
        # would sum bins that are not the same frequency, and the result would
        # still look like a spectrum.
        raise ForwardModelError(
            f"{take_path}: the {DRIVER_ROLE_WOOFER!r} and "
            f"{DRIVER_ROLE_TWEETER!r} curves disagree about their frequency grid",
            reason=REFUSAL_GRID_DISAGREES,
            detail={"take_path": take_path},
        )
    return BranchPair(
        freqs_hz=woofer[0],
        woofer_role=DRIVER_ROLE_WOOFER,
        tweeter_role=DRIVER_ROLE_TWEETER,
        woofer_tf=woofer[1],
        tweeter_tf=tweeter[1],
        band_hz_by_role={
            DRIVER_ROLE_WOOFER: woofer[2], DRIVER_ROLE_TWEETER: tweeter[2],
        },
        take_path=take_path,
    )


def predict_sum(
    pair: BranchPair, candidate: SummationCandidate
) -> PredictedSum:
    """This candidate's predicted summed magnitude, from the banked solos.

    The composition is :func:`~.plan_assembly.compose_linearized_prediction`,
    the one this codebase has; there is no biquad or summation arithmetic here.
    Each branch is zeroed outside its own swept band BEFORE the sum: there was
    no stimulus there, so the banked sample is noise.
    """
    frame = SummationFrame(
        freqs_hz=pair.freqs_hz,
        branch_tf={
            pair.woofer_role: np.where(
                pair.driven(pair.woofer_role), pair.woofer_tf, 0.0
            ),
            pair.tweeter_role: np.where(
                pair.driven(pair.tweeter_role), pair.tweeter_tf, 0.0
            ),
        },
        polarity_sign=int(candidate.polarity_sign),
        residual_delay_us=float(candidate.residual_delay_us),
    )
    trims = {
        role: float(candidate.trim_db_by_role.get(role, 0.0))
        for role in (pair.woofer_role, pair.tweeter_role)
    }
    freqs_hz, predicted_db = compose_linearized_prediction(
        frame,
        filters_by_role=candidate.filters_by_role,
        role_attenuations_db=trims,
    )
    return PredictedSum(
        freqs_hz=freqs_hz,
        predicted_db=predicted_db,
        sum_band_hz=pair.sum_band_hz,
        take_path=pair.take_path,
    )


def predicted_minus_measured_db(
    predicted: PredictedSum,
    measured_freqs_hz: Any,
    measured_db: Any,
    *,
    band_hz: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """The predicted-vs-measured delta, as facts and no verdict.

    Both curves are level-normalised against their OWN median over the compared
    band before subtracting: a forward model over banked solos carries no
    absolute SPL reference, so the raw offset between it and a measured sum is a
    level difference rather than a shape error. The offset removed is published
    as ``level_offset_db``.

    ``band_hz`` defaults to the prediction's own
    :attr:`PredictedSum.sum_band_hz` intersected with the measured curve's
    extent, and is reported as ``compared_band_hz``. No verdict, tolerance or
    score is returned (invariant 3).
    """
    measured_grid = np.asarray(measured_freqs_hz, dtype=float)
    measured_curve = np.asarray(measured_db, dtype=float)
    if measured_grid.size != measured_curve.size or measured_grid.size == 0:
        raise ForwardModelError(
            "the measured curve and its grid disagree in length",
            detail={
                "measured_points": int(measured_curve.size),
                "grid_points": int(measured_grid.size),
            },
        )
    lo_hz = max(predicted.sum_band_hz[0], float(measured_grid.min()))
    hi_hz = min(predicted.sum_band_hz[1], float(measured_grid.max()))
    if band_hz is not None:
        lo_hz = max(lo_hz, float(band_hz[0]))
        hi_hz = min(hi_hz, float(band_hz[1]))
    grid = predicted.freqs_hz
    mask = (grid >= lo_hz) & (grid <= hi_hz)
    if not np.any(mask):
        raise ForwardModelError(
            f"no predicted bin falls in {lo_hz:g}-{hi_hz:g} Hz",
            detail={"compared_lo_hz": lo_hz, "compared_hi_hz": hi_hz},
        )
    compared_grid = grid[mask]
    predicted_curve = predicted.predicted_db[mask]
    measured_on_grid = np.interp(compared_grid, measured_grid, measured_curve)
    offset_db = float(np.median(predicted_curve) - np.median(measured_on_grid))
    delta = (predicted_curve - offset_db) - measured_on_grid
    return {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "compared_band_hz": [lo_hz, hi_hz],
        "compared_points": int(compared_grid.size),
        "level_offset_db": offset_db,
        "freqs_hz": [float(hz) for hz in compared_grid],
        "delta_db": [float(db) for db in delta],
        "max_abs_db": float(np.max(np.abs(delta))),
        "rms_db": float(np.sqrt(np.mean(delta**2))),
        "take_path": predicted.take_path,
    }


__all__ = [
    "ACCEPTANCE_JUDGED",
    "ACCEPTANCE_NOT_RUN",
    "PREDICTION_KIND",
    "PREDICTION_SCHEMA_VERSION",
    "REFUSAL_GRID_DISAGREES",
    "REFUSAL_UNSUPPORTED",
    "BranchPair",
    "ForwardModelError",
    "PredictedSum",
    "SummationCandidate",
    "acceptance_block",
    "candidate_from_json",
    "load_branch_pair",
    "predict_sum",
    "predicted_minus_measured_db",
]
