# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the speaker would measure, for a candidate nothing has played.

Offline simulated evaluation over a round's BANKED per-driver solos: the two
branches' complex transfers, summed through one candidate's filter chains,
trims, delay and polarity, with no build, no tone and no device. Corners are
DECLARED by the operator (invariant 2); this predicts what a variation of one
would measure and nothing here ranks candidates or scores them (invariant 3).

**It reads the bank; it never re-derives from WAV.** Ruling R9: a MEASURE or
lateral pose banks magnitude AND phase for every curve
(:func:`~.spatial.pose_curve_record`), so a transfer function reconstructs
exactly from JSON that is already on disk. The one parse is
:func:`~.position_cycle.parse_curve_complex`, shared with the delay landscape.

**Same arithmetic as the shipped path, not a second one.** The composition is
:func:`~.plan_assembly.compose_linearized_prediction` — the ONE composition, a
caller of :func:`~jasper.active_speaker.branch_chain.chain_response` (the
single RBJ biquad evaluator) and of
:func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum`. There
is no biquad math and no summation arithmetic in this file, and a
re-derivation here would be a defect rather than a convenience.

**THE LANDMINE: the delay is a RESIDUAL.** Every banked solo is referenced to
its OWN direct peak — :func:`~jasper.audio_measurement.program_analysis.
_driver_response` windows each capture at its own argmax — so the physical
inter-driver arrival gap is ALREADY out of the banked pair. Phasing that pair
by a full applied delay counts the gap twice and injects a comb the graph need
not have. :attr:`SummationCandidate.residual_delay_us` is therefore a residual
in the analysis frame, and its one derivation is
:func:`~jasper.audio_measurement.program_analysis.summed_model_residual_delay_us`
— call it rather than subtracting by hand.

**The candidate carries no crossover sections, and that is the honest shape.**
A banked solo already contains whatever the emitted graph ran during its
capture. Multiplying a crossover back in would double-count it, and dividing
the emitted one out is a de-embedding this model does not do. So a candidate
varies exactly what the same-corner contract (ruling R7) lets a round vary —
linearization filters, trims, delay, polarity — over a fixed declared corner.

Units are in every name: ``_hz``, ``_us``, ``_db``, ``_deg``.
"""

from __future__ import annotations

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
REFUSAL_NO_CURVE_PAIR = "forward_model_no_banked_curve_pair"
REFUSAL_GRID_DISAGREES = "forward_model_branch_grids_disagree"

#: Nothing measured judged this prediction. What a bare prediction always is:
#: the model computed a curve and no capture ever contradicted it.
ACCEPTANCE_NOT_RUN = "not_run"

#: A banked measurement judged it, and ``judged_against`` names which one.
ACCEPTANCE_JUDGED = "judged_against_measured"


def acceptance_block(judged_against: str | None) -> dict[str, Any]:
    """Whether a measurement judged this prediction, and which one.

    The disclosure #3481 found missing: ``predict`` and ``verify-delta``
    emitted equally authoritative JSON whether or not anything had ever
    checked the model against a capture, so an untriaged misattribution
    entered round provenance indistinguishably from a validated one.

    ``judged_against`` is the measured comparand's own identity — the banked
    round whose VERIFY sum the prediction was deltaed against — or ``None``,
    which is what a prediction nothing measured is.

    Disclosure, never a gate, and never a grade: this module ships no
    acceptance tolerance and invents none, so a JUDGED record says which
    measurement judged it and leaves what the delta MEANS to the reader
    (invariant 3).
    """
    return {
        "status": ACCEPTANCE_JUDGED if judged_against else ACCEPTANCE_NOT_RUN,
        "judged_against": judged_against,
    }


class ForwardModelError(ValueError):
    """The banked curves cannot support a predicted sum.

    Carries ``refusal_reason`` and the numbers behind it in ``detail``, so a
    caller reads attributes set at the raise site instead of parsing the
    message. The reason and the numbers are the contract; the message is
    operator copy and may be reworded freely.
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
        ``{biquad_type, freq, q, gain}`` records the emitter re-validates and
        :func:`~jasper.active_speaker.branch_chain.chain_response` speaks. A
        role with no entry is corrected by unity — an unfitted branch survives
        raw.
    ``trim_db_by_role``
        The emitted per-driver trim in dB. A role with no entry trims by 0.
    ``polarity_sign``
        ``+1`` or ``-1``, applied to the TWEETER branch, matching
        :func:`~jasper.audio_measurement.program_analysis.predicted_branch_sum`'s
        own frame.
    ``residual_delay_us``
        Signed RESIDUAL delay in the analysis frame, never an applied delay.
        See the module docstring's landmine.

    Frozen and EQUAL by content, so a caller holding several variations can say
    two of them are the same proposal. NOT hashable: the mapping fields are
    ordinarily ``dict``, so the generated ``__hash__`` raises on call —
    deduplicate with ``==`` or a list, never a ``set``.
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

    ``band_hz_by_role`` is each driver's own SWEPT band, which is NOT the grid:
    :func:`~.spatial.lateral_pose_curve` resamples every curve onto one shared
    evidence grid and keeps the band it actually swept. Outside its swept band
    a driver's sample is noise rather than a small response, so
    :func:`predict_sum` contributes exactly zero from it.

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

    The serialized record's ``acceptance`` is
    :data:`ACCEPTANCE_NOT_RUN` by construction: :func:`predict_sum` sums
    banked solos and compares them to nothing, so no instance of this class
    was ever judged by a measurement. The judged form of the same disclosure
    rides the delta result that did the judging
    (:class:`~.round_views.ForwardModelDeltaResult`).
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
            "acceptance": acceptance_block(None),
        }


def load_branch_pair(
    bundle_dir: Path,
    *,
    phase: str,
    position_deg: int,
    woofer_role: str = DRIVER_ROLE_WOOFER,
    tweeter_role: str = DRIVER_ROLE_TWEETER,
) -> BranchPair | None:
    """The latest banked take carrying BOTH roles, as complex transfers.

    Take selection is :func:`~.position_cycle.read_pose_curve_pair` — the one
    reader of "which take speaks for this pose", shared with the delay
    landscape's door — and the parse is
    :func:`~.position_cycle.parse_curve_complex`. No second reader of either.

    ``None`` when no take at this pose carries both roles, or when one of them
    does not parse — ordinary shapes for a round that measured one driver,
    never an error. A take that carries both roles on DISAGREEING grids raises
    instead: that is one capture whose two curves cannot be summed elementwise,
    which is a defect rather than an absence, and folding it into the same
    ``None`` would hide it.

    **No cache, measured rather than assumed** (ticket 3.8's cache clause).
    The banked pose grid is 121 bins, so a take is ~12 KiB of JSON: this walks
    a 40-take bank — larger than a round banks — in ~7 ms, and the
    :class:`BranchPair` it returns is then reused across candidates at ~0.12 ms
    per :func:`predict_sum`. A cache would be machinery guarding milliseconds.
    """
    found = read_pose_curve_pair(
        Path(bundle_dir),
        phase=phase,
        position_deg=position_deg,
        roles=(woofer_role, tweeter_role),
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
        # are the dangerous case precisely because a shape check waves them
        # through: the sum would add bins that are not the same frequency, and
        # the result would still look like a spectrum.
        raise ForwardModelError(
            f"{take_path}: the {woofer_role!r} and {tweeter_role!r} curves "
            "disagree about their frequency grid",
            reason=REFUSAL_GRID_DISAGREES,
            detail={"take_path": take_path},
        )
    return BranchPair(
        freqs_hz=woofer[0],
        woofer_role=woofer_role,
        tweeter_role=tweeter_role,
        woofer_tf=woofer[1],
        tweeter_tf=tweeter[1],
        band_hz_by_role={woofer_role: woofer[2], tweeter_role: tweeter[2]},
        take_path=take_path,
    )


def predict_sum(
    pair: BranchPair, candidate: SummationCandidate
) -> PredictedSum:
    """This candidate's predicted summed magnitude, from the banked solos.

    The composition is :func:`~.plan_assembly.compose_linearized_prediction` —
    the one this codebase has — so a prediction from the bank and a prediction
    of the emitted graph are the same arithmetic rather than two that agree
    until they do not.

    Each branch is zeroed outside its own swept band BEFORE the sum: there was
    no stimulus there, so the banked sample is noise and summing it would let
    noise reach a delta a reader might grade.
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
    absolute SPL reference, so the raw offset between it and a measured sum is
    a level difference rather than a shape error, and reporting it as one would
    put a whole-band constant on top of every band's number. The normalisation
    offset is published as ``level_offset_db`` — the fact it removes, kept
    rather than discarded.

    ``band_hz`` defaults to the prediction's own :attr:`PredictedSum.sum_band_hz`
    intersected with the measured curve's extent, and is reported as
    ``compared_band_hz``. Fields, not a grade: what any of these numbers MEANS
    is the reader's judgement, and this returns no verdict, tolerance or score.
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
    "REFUSAL_NO_CURVE_PAIR",
    "REFUSAL_UNSUPPORTED",
    "BranchPair",
    "ForwardModelError",
    "PredictedSum",
    "SummationCandidate",
    "acceptance_block",
    "load_branch_pair",
    "predict_sum",
    "predicted_minus_measured_db",
]
