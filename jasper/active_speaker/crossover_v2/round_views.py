# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round-grading comparison views a laptop campaign had re-derived by hand.

Reads and grades a BANKED ROUND DIRECTORY (``scripts/bank-crossover-round.sh``),
which comes in two shapes — a MEASURE round and a VERIFY round (#2769). Every
DSP and grading primitive is imported from the seam that owns it; this module
performs no DSP of its own beyond :func:`~.feature_optics.detrend`. A seat is
keyed by its stable ``position_id``, but the 2026-08-24 geometry ruling moved
what a given id points at, so each row's own ``position_deg`` rides beside it
and a mixed comparison is disclosed rather than refused. A lateral walk pose is
never joined to a cloud seat: they are different captures.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker import flat_spec
from jasper.active_speaker.flat_spec import REFERENCE_BAND_HZ, FlatSpecReport, evaluate_flat_spec
from jasper.audio_measurement.gating import ENTANGLEMENT_SOURCE_UNKNOWN
from jasper.active_speaker.flat_spec_views import (
    DirectivityTable,
    PositionCurve,
    directivity_table,
    role_split_flatness,
    _evaluate_position,
    _exclusion_mask,
    _pool,
)
from jasper.active_speaker.repeat_floor import SHIPPED_POOL_METRIC
from jasper.active_speaker.crossover_v2 import forward_model, position_cycle
from jasper.active_speaker.crossover_v2.contracts import DESIGN_AXIS_DEG
from jasper.active_speaker.crossover_v2.durable_state import (
    verify_measured_curve_from_state,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    _mapping,
    _read_candidate,
    CrossoverEvidencePacketError,
    build_crossover_evidence_packet,
    round_artifact_dir,
)
from jasper.active_speaker.crossover_v2.feature_classifier import (
    load_round_pose_curves,
)
from jasper.active_speaker.crossover_v2.feature_optics import detrend
from jasper.active_speaker.crossover_v2.gate_sweep import (
    DEFAULT_RUNGS_MS,
    GRID_HI_HZ,
    GRID_LO_HZ,
    REFUSE_SINGLE_POSE,
    analysis_grid,
    frame_descriptor,
    sweep_features,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE
from jasper.active_speaker.crossover_v2.round_captures import (
    RoundCapturesRefused,
    discover_captures,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    RoundInputs,
    RoundViewsError,
    round_inputs,
)
from jasper.active_speaker.branch_target import SIGNIFICANT_GAIN_DB
from jasper.active_speaker.linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    MIC_TIERS,
)
from jasper.audio_measurement.olive_metrics import nbd_and_sm
from jasper.audio_measurement.spatial_combine import BandSpread, octave_bands_hz

__all__ = [
    "AGREEMENT_DISSENT_MAX",
    "AGREEMENT_TESTIFY_MIN",
    "NOT_SWEPT_BAND_NOT_EVALUABLE",
    "NOT_SWEPT_BIN_OFF_ANALYSIS_GRID",
    "NOT_SWEPT_CAPTURES_UNREADABLE",
    "NOT_SWEPT_SINGLE_POSE",
    "AgreementFeature",
    "AudibilityCoMetrics",
    "BOUND_FLOOR_DB",
    "CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE",
    "CLOUD_BINDING_ENTRY_INCOMPLETE",
    "CLOUD_BINDING_FIT_INPUTS_NOT_BANKED",
    "CLOUD_BINDING_NOT_FITTED",
    "CLOUD_BINDING_NOT_A_PAIR",
    "CLOUD_BINDING_NO_CLOUD_EVIDENCE",
    "CLOUD_BINDING_NO_FIT",
    "CLOUD_BINDING_REFIT_DRIFTED",
    "CloudBindingBand",
    "CloudBindingRole",
    "CloudBindingView",
    "REFIT_TOLERANCE_DB",
    "SEVERED_CLOUD_INPUTS",
    "AudibilityMetrics",
    "BankedRound",
    "ENTRY_STATE_UNREADABLE",
    "EntryStateGrade",
    "ForwardModelDeltaResult",
    "FrozenReferenceResult",
    "PooledWindowResult",
    "RepeatabilityMetric",
    "RepeatabilityResult",
    "RoundInputs",
    "RoundViewsError",
    "SeatCurve",
    "VerifyPoseResult",
    "agreement_table",
    "audibility_co_metrics",
    "cloud_binding_view",
    "default_agreement_lo_hz",
    "directivity_view",
    "entry_state_grade",
    "forward_model_verify_delta",
    "frozen_reference_grade",
    "load_banked_round",
    "per_seat_curves",
    "pooled_window_horizontal",
    "repeat_floor_provenance",
    "repeatability_spread",
    "spec_with_gate_sensitivity",
    "verify_pose_curve",
]

#: Mirrors ``.spatial.POSITION_ROLE_ONAX`` as a local literal rather than
#: importing that large, orchestration-heavy module for one string. This package
#: never owns the constant; it takes it as a caller-supplied value.
DEFAULT_PRIMARY_ROLE = "onax"

#: The synthetic role/position-id this module mints for a VERIFY-phase
#: capture, which a round's bundle never carries a ``positions`` row for.
VERIFY_ROLE = "verify"
VERIFY_POSITION_ID = "verify"

#: The campaign's own LITERAL agreement thresholds (``agreement.py``:
#: ``test >= 3 and diss <= 1``), never a seat-count-relative generalisation —
#: see :func:`agreement_table` for why a generalisation is the wrong port.
AGREEMENT_TESTIFY_MIN = 3
AGREEMENT_DISSENT_MAX = 1

# --------------------------------------------------------------------------- #
# Loading one round
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BankedRound:
    """One round — banked or still live on the box — read once, ready for every
    view below.

    ``report`` carries the round's own grading frame; ``positions`` is built from
    the evidence packet's ``positions`` block, and nothing here re-parses
    ``cloud_verify.json``. Both are ABSENT on a round that banked no cloud group,
    and that is a round SHAPE rather than a defect (#3478): only the verify stage
    banks one. The two accessors below are where a view that NEEDS them says so.
    """

    round_dir: Path
    inputs: RoundInputs
    positions: tuple[PositionCurve, ...]
    curve_grid_hz: np.ndarray
    report: FlatSpecReport | None
    packet: Mapping[str, Any] = field(repr=False)

    @property
    def session_dir(self) -> Path:
        """The commissioning bundle this round's evidence was read from."""
        return self.inputs.session_dir

    @property
    def graded_report(self) -> FlatSpecReport:
        """The round's own graded spec, or :class:`RoundViewsError`."""
        if self.report is None:
            raise RoundViewsError(
                f"{self.round_dir}: evidence packet carries no graded spec"
            )
        return self.report

    @property
    def graded_positions(self) -> tuple[PositionCurve, ...]:
        """The round's cloud seats, or :class:`RoundViewsError`.

        TWO refusals, because two different things are missing: a round that
        banked no cloud group is the measure-stage SHAPE above, while a block
        that says ``available`` with nothing left after the ``magnitude_db``
        filter is a TRUNCATED packet from a round that did walk one.
        """
        if self.positions:
            return self.positions
        block = self.packet.get("positions") or {}
        if block.get("available"):
            raise RoundViewsError(
                f"{self.round_dir}: every position row is missing its magnitude_db"
            )
        raise RoundViewsError(
            f"{self.round_dir}: evidence packet carries no position evidence"
        )


def _row_degrees(row: Mapping[str, Any]) -> float | None:
    """One packet position row's banked bearing, or ``None`` for "not recorded".

    ``None`` covers every way a row can lack one and they are deliberately not
    told apart HERE; the packet's own ``angle_deg`` block publishes the
    distinction. ``bool`` is rejected before ``int`` because it subclasses it,
    so a hand-edited ``true`` would otherwise publish as a 1° bearing.
    """
    value = row.get("position_deg")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def load_banked_round(round_dir: Path) -> BankedRound:
    """Read one round — banked tree or LIVE session bundle — into a
    :class:`BankedRound`.

    Which of the two it is, and so where the flow state, design draft and
    applied profile come from, is :func:`~.round_inputs.round_inputs`' answer;
    it rides on :attr:`BankedRound.inputs` so the views read the same files this
    packet was built from. Raises :class:`RoundViewsError` when the directory is
    neither shape, when a banked tree holds more than one session, or when the
    bundle carries no readable evidence packet. It does NOT judge what the round
    banked — what a view needs, the view says (#3478, #3482).
    """
    round_dir = Path(round_dir)
    inputs = round_inputs(round_dir)
    try:
        packet = build_crossover_evidence_packet(
            inputs.session_dir,
            state_path=inputs.state_path,
            driver_draft_path=inputs.design_draft_path,
            applied_profile_path=inputs.applied_profile_path,
            repeat_floor_path=inputs.repeat_floor_path,
            declared_geometry_path=inputs.declared_geometry_path,
        )
    except CrossoverEvidencePacketError as exc:
        raise RoundViewsError(f"{round_dir}: {exc}") from exc

    positions_block = packet.get("positions") or {}
    spec_block = packet.get("spec") or {}
    grid_block = positions_block.get("curve_grid") or {}
    grid = np.asarray(grid_block.get("freqs_hz") or [], dtype=float)
    smoothing = int(grid_block.get("smoothing_fraction") or 0)
    positions = tuple(
        PositionCurve(
            position_id=str(row.get("position_id") or ""),
            role=str(row.get("role") or ""),
            freqs_hz=grid,
            magnitude_db=np.asarray(row.get("magnitude_db") or [], dtype=float),
            smoothing_fraction=smoothing,
            # The seat's OWN banked bearing, read rather than defaulted. Absent
            # stays ``None`` — "not recorded", never zero. ``bool`` is excluded
            # because it subclasses ``int``.
            degrees=_row_degrees(row),
            take_id=str(row.get("take_id") or ""),
        )
        for row in positions_block.get("positions") or []
        if row.get("magnitude_db")
    )
    report = FlatSpecReport.from_dict(spec_block) if spec_block.get("bands") else None
    return BankedRound(
        round_dir=round_dir,
        inputs=inputs,
        positions=positions,
        curve_grid_hz=grid,
        report=report,
        packet=packet,
    )


# --------------------------------------------------------------------------- #
# The room/speaker read, stamped onto the round's own spec verdict
# --------------------------------------------------------------------------- #


#: Why a band carries no gate sensitivity when the LADDER NEVER RAN on it. The
#: ``not_swept_`` prefix tells these apart from the sweep's own refusals
#: (``gate_sweep.NULL_*``), which mean the ladder ran and then declined to
#: publish; both land in the one ``BandResult.gate_sensitivity_note`` field.
NOT_SWEPT_SINGLE_POSE = "not_swept_single_pose"
NOT_SWEPT_BAND_NOT_EVALUABLE = "not_swept_band_not_evaluable"
#: Every ``RoundCapturesRefused`` the ladder can raise EXCEPT the single-pose
#: one. They are one word here because the answer is the same — this round's
#: captures did not become curves — and the refusal carries the detail.
NOT_SWEPT_CAPTURES_UNREADABLE = "not_swept_captures_unreadable"
NOT_SWEPT_BIN_OFF_ANALYSIS_GRID = "not_swept_bin_outside_analysis_grid"


def _stamped_band(
    band: flat_spec.BandResult,
    feature: Mapping[str, Any] | None,
    note: str | None,
    detail: dict[str, Any] | None,
) -> flat_spec.BandResult:
    """One band plus the ladder's read at its own worst bin, or the reason why not.

    ``n_valid_rungs`` and ``gate_window_verdict`` are stamped whenever the ladder
    RAN, including on a null: ``"unresolved"`` rather than absent, so silence is
    never mistaken for "never swept". ``detail`` carries only beside a
    capture-refusal note and is ``None`` for every other note.
    """
    if feature is None:
        return replace(band, gate_sensitivity_note=note, gate_sensitivity_detail=detail)
    sensitivity = feature.get("sensitivity")
    return replace(
        band,
        gate_sensitivity_db=(
            None if sensitivity is None else float(sensitivity["corrected_delta_db"])
        ),
        sigma_growth_ratio=(
            None if sensitivity is None else float(sensitivity["sigma_growth_ratio"])
        ),
        n_valid_rungs=int(feature["n_valid_rungs"]),
        gate_sensitivity_note=feature.get("sensitivity_null_reason"),
        gate_window_verdict=feature["window_verdict"],
        gate_window_verdict_reasons=tuple(feature["window_verdict_reasons"]),
    )


def spec_with_gate_sensitivity(
    banked: BankedRound, *, rungs_ms: Sequence[float] = DEFAULT_RUNGS_MS,
) -> FlatSpecReport:
    """The round's graded spec, with "room or speaker" answered at every band's
    own worst bin.

    The spec verdict names the bin; :mod:`.gate_sweep` says whether that bin
    moves with the analysis window. Each
    :class:`~jasper.active_speaker.flat_spec.BandResult` carries the ladder's
    headline at its own ``max_deviation_hz``, and the report carries the frame
    those numbers are stated in. **Disclosure only: no grade moves.**

    It reads a BANKED round rather than the live combine because the cloud
    pipeline's seam keeps each position's magnitude and drops the ``ir``. The IR
    reachable there has already been through ``deconv.direct_arrival_window``
    and the adaptive reflection gate, whose search stops at
    ``gating.SEARCH_T_MAX_MS`` — 7 ms — so the ladder's 9, 12 and 20 ms rungs
    would read a window closed before they got there and ``sigma_growth_ratio``
    would come back at ~1.0 by construction. Only a banked round's raw
    ``summed_*.wav``, deconvolved here against its own program, can answer what
    a longer window admits.

    Cost is one ladder pass at up to three bins. Every way there can be no
    number is named in ``gate_sensitivity_note`` and none of them raises; a
    capture refusal additionally stamps ``gate_sensitivity_detail`` with the
    specific input that was missing.
    """
    report = banked.graded_report
    targets: list[tuple[int, float]] = []
    notes: dict[int, str] = {}
    for index, band in enumerate(report.bands):
        worst_hz = band.max_deviation_hz
        if worst_hz is None:
            notes[index] = NOT_SWEPT_BAND_NOT_EVALUABLE
        elif not GRID_LO_HZ <= float(worst_hz) <= GRID_HI_HZ:
            # Named per band rather than allowed to raise: ``sweep_features``
            # rejects the whole CALL on one off-grid bin.
            notes[index] = NOT_SWEPT_BIN_OFF_ANALYSIS_GRID
        else:
            targets.append((index, float(worst_hz)))

    features: dict[int, Mapping[str, Any]] = {}
    details: dict[int, dict[str, Any]] = {}
    frame: dict[str, Any] | None = None
    if targets:
        rungs = tuple(sorted(float(rung) for rung in rungs_ms))
        try:
            swept = sweep_features(
                discover_captures(banked.round_dir),
                rungs_ms=rungs,
                at_hz=[hz for _index, hz in targets],
            )
        except RoundCapturesRefused as exc:
            # The engine's own bar, echoed rather than re-judged: it refuses
            # fewer than two captures.
            refused = (
                NOT_SWEPT_SINGLE_POSE
                if exc.reason == REFUSE_SINGLE_POSE
                else NOT_SWEPT_CAPTURES_UNREADABLE
            )
            notes.update({index: refused for index, _hz in targets})
            # The bucket slug names only the shape; what was missing rides on
            # the exception the engine already raised.
            details.update(
                {index: {"reason": exc.reason, **exc.detail} for index, _hz in targets}
            )
        else:
            # Positional, never keyed by frequency: two bands asking about one
            # bin is a coincidence, not a reason to share an answer.
            features = {
                index: feature
                for (index, _hz), feature in zip(targets, swept, strict=True)
            }
            frame = frame_descriptor(rungs, analysis_grid())

    return replace(
        report,
        bands=tuple(
            _stamped_band(band, features.get(index), notes.get(index), details.get(index))
            for index, band in enumerate(report.bands)
        ),
        gate_sweep_frame=frame,
    )


# --------------------------------------------------------------------------- #
# View 0 — grading the state the round STARTED from
# --------------------------------------------------------------------------- #


#: Why an entry state could not be graded when the take IS banked but will not
#: rehydrate. ``EntryBaseline.from_dict`` owns that rule and answers ``None`` for
#: every member of the set; it does not say which member it was, and neither
#: does this.
ENTRY_STATE_UNREADABLE = (
    "this round banked an entry_baseline take, but it does not rehydrate into "
    "a gradeable baseline — its curve, exclusion mask, or identity fields are "
    "absent, disagree in length, or are not finite"
)


@dataclass(frozen=True)
class EntryStateGrade:
    """The graph a round ENTERED on, graded — or the named reason it was not.

    A reader, not a new capture and not a second grader: the entry-baseline take
    is write-once (ruling S3's offline promise) and ``report`` is a real
    :class:`~jasper.active_speaker.flat_spec.FlatSpecReport` from the shipped
    evaluator, so it reads side by side with the round's own ``spec`` block.

    **The frame is the ROUND's, the exclusion mask is the TAKE's.** The mask is
    :func:`~.round_evidence._validity_clamp`'s output — the bins below THIS
    capture's own reflection gate — not the cloud's interference screen over a
    different capture. Grading this curve through the round's post-apply
    exclusions would report deviations at bins nobody vouched for.
    :func:`_entry_frame` owns what a round that graded no after is stated in.

    ``round_ordinal`` / ``round_ordinal_epoch`` are read from the banked flow
    state; ``None`` is "not recorded". They ride here because "the entry state
    was this flat" means one thing at round 1 of a fresh box and another at
    round 1 after a republish reset the count. ``available`` ``False`` carries a
    non-empty ``reason`` and no report, and the reverse.
    """

    available: bool
    reason: str
    program_id: str
    reference_mark: str
    graph_fingerprint: str
    captured_at: str
    artifact_ref: str
    report: FlatSpecReport | None
    round_ordinal: int | None = None
    round_ordinal_epoch: int | None = None

    @classmethod
    def unavailable(
        cls,
        reason: str,
        *,
        round_ordinal: int | None = None,
        round_ordinal_epoch: int | None = None,
    ) -> "EntryStateGrade":
        return cls(
            available=False, reason=reason, program_id="", reference_mark="",
            graph_fingerprint="", captured_at="", artifact_ref="", report=None,
            round_ordinal=round_ordinal, round_ordinal_epoch=round_ordinal_epoch,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "program_id": self.program_id,
            "reference_mark": self.reference_mark,
            # WHICH entry state was graded: a first round's entry graph is the
            # declarations-derived config a fresh box wears, a later round's is
            # whatever the previous round left, told apart by this and nothing
            # else here.
            "graph_fingerprint": self.graph_fingerprint,
            "captured_at": self.captured_at,
            "artifact_ref": self.artifact_ref,
            # WHICH round, and which epoch of the count. Round 1 of a fresh box
            # and round 1 after a reset are the same ordinal, different facts.
            "round_ordinal": self.round_ordinal,
            "round_ordinal_epoch": self.round_ordinal_epoch,
            "report": None if self.report is None else self.report.to_dict(),
        }


def _banked_series_position(state_path: Path | None) -> tuple[int | None, int | None]:
    """``(round_ordinal, round_ordinal_epoch)`` off the round's flow state.

    ``None`` for either field the record does not carry — "not recorded", never
    zero. Read here rather than off the evidence packet because the packet's
    ``round_receipt`` block publishes identities and not the ordinal. ``bool`` is
    rejected before ``int`` for :func:`_row_degrees`' reason.
    """

    def _count(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    if state_path is None or not state_path.is_file():
        return None, None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(state, Mapping):
        return None, None
    receipt = state.get("round_receipt")
    ordinal = _count(receipt.get("round_ordinal")) if isinstance(receipt, Mapping) else None
    return ordinal, _count(state.get("round_ordinal_epoch"))


def _entry_frame(report: FlatSpecReport | None) -> tuple[int, dict[str, Any]]:
    """``(smoothing_fraction, frame_kwargs)`` for an entry baseline — the round's
    own when it graded one, nothing when it did not.

    The frame is the ROUND's so a before and an after are stated over one span. A
    round that banked no cloud group graded no after, so the honest frame is no
    frame: unclamped, on ``0``, this module's spelling for *not attested*. The
    emitted report echoes both clamps as ``None`` on its face, so the grade
    discloses which frame produced it. The room's floor is READ BACK rather than
    re-derived: a floor recomputed here would be a second opinion about one room.
    """
    if report is None:
        return 0, {
            "trusted_floor_hz": None,
            "trusted_ceiling_hz": None,
            "entanglement_floor_hz": None,
            "entanglement_floor_source": ENTANGLEMENT_SOURCE_UNKNOWN,
        }
    return report.smoothing_fraction, report.frame_kwargs


def entry_state_grade(banked: BankedRound) -> EntryStateGrade:
    """Grade the entry state this round measured before it applied anything.

    Reads the banked entry-baseline take out of the round's evidence packet,
    rehydrates it through :meth:`~.round_evidence.EntryBaseline.from_dict` and
    hands the arrays to the shipped
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`. It requires no
    cloud group, which is what makes it reachable: the measure stage is the only
    stage that banks an entry baseline and the only one that banks no cloud
    (#3478). ``packet["entry_baseline"]`` is indexed rather than fetched with a
    default, so a missing key lands in the ``KeyError`` arm the CLI already
    treats as an unreadable round.
    """

    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

    ordinal, epoch = _banked_series_position(banked.inputs.state_path)
    block = banked.packet["entry_baseline"]
    if not block.get("available"):
        return EntryStateGrade.unavailable(
            str(block.get("reason") or ""),
            round_ordinal=ordinal, round_ordinal_epoch=epoch,
        )
    baseline = EntryBaseline.from_dict(block)
    if baseline is None:
        return EntryStateGrade.unavailable(
            ENTRY_STATE_UNREADABLE,
            round_ordinal=ordinal, round_ordinal_epoch=epoch,
        )
    smoothing_fraction, frame_kwargs = _entry_frame(banked.report)
    report = evaluate_flat_spec(
        np.asarray(baseline.curve.hz, dtype=float),
        np.asarray(baseline.curve.db, dtype=float),
        np.asarray(baseline.excluded, dtype=bool),
        smoothing_fraction=smoothing_fraction,
        **frame_kwargs,
    )
    return EntryStateGrade(
        available=True,
        reason="",
        program_id=baseline.program_id,
        reference_mark=baseline.reference_mark,
        graph_fingerprint=baseline.graph_fingerprint,
        captured_at=baseline.captured_at,
        artifact_ref=baseline.artifact_ref,
        report=report,
        round_ordinal=ordinal,
        round_ordinal_epoch=epoch,
    )


# --------------------------------------------------------------------------- #
# View 1 — frozen-reference grading through the shipped grader
# --------------------------------------------------------------------------- #


def _own_reference_db(position: PositionCurve, report: FlatSpecReport) -> float:
    """The reference the SHIPPED path grades this position against — read
    back off the real evaluator, never recomputed here."""
    graded = evaluate_flat_spec(
        np.asarray(position.freqs_hz, dtype=float),
        np.asarray(position.magnitude_db, dtype=float),
        _exclusion_mask(np.asarray(position.freqs_hz, dtype=float), report.excluded_intervals),
        smoothing_fraction=position.smoothing_fraction,
        **report.frame_kwargs,
    )
    return float(graded.reference_db)


def _grade_positions(
    positions: tuple[PositionCurve, ...],
    report: FlatSpecReport,
    frozen_refs: Mapping[str, float] | None,
) -> tuple[dict[str, float], dict[str, float]]:
    """One grading pass — shipped when ``frozen_refs`` is ``None``, frozen to
    the supplied per-position references otherwise.

    Returns ``(per_role_pooled, per_position_rms_db)``.
    """
    per_role: dict[str, list[tuple[float, float]]] = {}
    per_position: dict[str, float] = {}
    for position in positions:
        seat = position.position_id
        override = None if frozen_refs is None else frozen_refs[seat]
        flatness, _octaves = _evaluate_position(position, report, reference_db_override=override)
        if not flatness.evaluable or flatness.rms_db is None:
            raise RoundViewsError(f"{seat}: position not evaluable under this report's frame")
        per_position[seat] = float(flatness.rms_db)
        per_role.setdefault(position.role, []).append((float(flatness.n_bins), float(flatness.rms_db)))
    pooled = {role: value for role, pairs in per_role.items() if (value := _pool(pairs)) is not None}
    return pooled, per_position


@dataclass(frozen=True)
class FrozenReferenceResult:
    """One target round graded twice: as shipped, and frozen to the baseline's
    per-position reference levels.

    ``shipped`` and ``frozen`` are ``{role: pooled_rms_db}``. The freeze removes
    the one degree of freedom §8.9 found compensating a prescribed cut's level
    loss — grading each config against its OWN reference.
    ``target_own_refs`` / ``baseline_refs`` are the per-position levels each half
    actually used, so a caller can audit the freeze rather than trust it.
    """

    baseline_round_dir: str
    target_round_dir: str
    shipped: dict[str, float]
    frozen: dict[str, float]
    shipped_positions: dict[str, float]
    frozen_positions: dict[str, float]
    baseline_refs: dict[str, float]
    target_own_refs: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_round_dir": self.baseline_round_dir,
            "target_round_dir": self.target_round_dir,
            "shipped": self.shipped,
            "frozen": self.frozen,
            "shipped_positions": self.shipped_positions,
            "frozen_positions": self.frozen_positions,
            "baseline_refs": self.baseline_refs,
            "target_own_refs": self.target_own_refs,
        }


def frozen_reference_grade(baseline: BankedRound, target: BankedRound) -> FrozenReferenceResult:
    """Grade ``target`` twice: shipped, and frozen to ``baseline``'s per-position
    reference levels.

    ``target`` may be the same round as ``baseline`` (frozen == shipped by
    construction then). Raises :class:`RoundViewsError` when either round banked
    no cloud group, when a position in ``target`` has no ``position_id``
    counterpart in ``baseline``, or when a position is not evaluable under its
    own report's frame.
    """
    baseline_refs = {
        position.position_id: _own_reference_db(position, baseline.graded_report)
        for position in baseline.graded_positions
    }
    positions = target.graded_positions
    missing = [p.position_id for p in positions if p.position_id not in baseline_refs]
    if missing:
        raise RoundViewsError(
            f"target round has position(s) {missing} with no baseline counterpart "
            f"(baseline has {sorted(baseline_refs)})"
        )
    report = target.graded_report
    target_own_refs = {
        position.position_id: _own_reference_db(position, report)
        for position in positions
    }
    shipped_pooled, shipped_positions = _grade_positions(positions, report, None)
    frozen_pooled, frozen_positions = _grade_positions(positions, report, baseline_refs)
    return FrozenReferenceResult(
        baseline_round_dir=str(baseline.round_dir),
        target_round_dir=str(target.round_dir),
        shipped=shipped_pooled,
        frozen=frozen_pooled,
        shipped_positions=shipped_positions,
        frozen_positions=frozen_positions,
        baseline_refs=baseline_refs,
        target_own_refs=target_own_refs,
    )


# --------------------------------------------------------------------------- #
# View 2 — the VERIFY pose made comparable to the cloud positions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifyPoseResult:
    """The VERIFY-phase capture's MEASURED curve, read off the round's own banked
    state and put on the round's ``curve_grid_hz`` — or the reason it could not
    be.

    ``curve`` is ``None`` exactly when ``reason`` is non-empty. Never raises: a
    round banked before the curve was persisted, or without its ``state.json``,
    is a normal shape.
    """

    curve: PositionCurve | None
    reason: str


def _banked_verify_curve(
    inputs: RoundInputs,
) -> tuple[tuple[np.ndarray, np.ndarray] | None, str]:
    """``((freqs_hz, measured_db), "")`` off the round's flow state, or
    ``(None, reason)``.

    The ONE reader of ``verify_priors.verify_measured`` on this side of the seam,
    because two consumers want the curve differently: :func:`verify_pose_curve`
    puts it on the round's cloud-position grid, and
    :func:`forward_model_verify_delta` takes it VERBATIM, since that comparison
    interpolates onto the prediction's own grid and a round with no cloud group
    has no third grid to detour through (#3482).
    """
    state_path = inputs.state_path
    if state_path is None or not state_path.is_file():
        # The resolver's code when it HAS one: "the speaker's state belongs to
        # another session" is a different answer from "no state was banked",
        # and only it names a round the operator could point at instead.
        return None, (
            inputs.state_reason or "the round names no readable flow state file"
        )
    try:
        state = json.loads(state_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{state_path.name} is unreadable: {type(exc).__name__}"
    if not isinstance(state, Mapping):
        return None, f"{state_path.name} is not a JSON object"
    triple = verify_measured_curve_from_state(state)
    if triple is None:
        return None, "the round's state banked no verify_priors.verify_measured curve"
    freqs_hz, measured_db, _predicted_db = triple
    return (freqs_hz, measured_db), ""


def verify_pose_curve(banked: BankedRound) -> VerifyPoseResult:
    """The VERIFY pose's measured curve, READ rather than re-derived.

    ``verify_priors.verify_measured`` holds the very pair the delta probe graded
    (``(freqs_hz, measured_db, predicted_db)``, #2522); this reads the measured
    half through :func:`_banked_verify_curve` and interpolates it onto the
    round's shared grid.

    The banked curve is block-averaged in dB to
    :data:`~.durable_state.MAX_PERSISTED_SUM_POINTS`, not smoothed at a
    fractional-octave width, so :attr:`PositionCurve.smoothing_fraction` is
    reported as ``0`` — this module's spelling for *not attested*. The resample
    is for the SEATS and only they should pay it, which is why
    :func:`forward_model_verify_delta` reads the same source verbatim.
    """
    banked_curve, reason = _banked_verify_curve(banked.inputs)
    if banked_curve is None:
        return VerifyPoseResult(None, reason)
    freqs_hz, measured_db = banked_curve
    grid = np.asarray(banked.curve_grid_hz, dtype=float)
    curve = PositionCurve(
        position_id=VERIFY_POSITION_ID,
        role=VERIFY_ROLE,
        freqs_hz=grid,
        magnitude_db=np.interp(grid, freqs_hz, measured_db),
        smoothing_fraction=0,
        # The VERIFY phase measures the confirmed on-axis listening position
        # by definition of the phase — this is not an angle recovered from a
        # walk log (none exists for this pose), it is what the phase means.
        degrees=0.0,
        take_id="",
    )
    return VerifyPoseResult(curve, "")


@dataclass(frozen=True)
class ForwardModelDeltaResult:
    """A predicted-vs-measured VERIFY delta, or why there is none.

    ``delta`` is ``None`` exactly when ``reason`` is non-empty. Never raises: a
    round that banked no per-driver solos, none at this pose, or no VERIFY curve,
    is a normal shape.

    ``basis_round_dir`` / ``measured_round_dir`` name the two rounds the halves
    came from, ALWAYS — equal when one round supplied both. Additive evidence: it
    carries no verdict, tolerance or score (invariant 3).
    """

    delta: Mapping[str, Any] | None
    reason: str
    #: Required, not defaulted: an unattributed join is the thing this pair
    #: exists to make impossible.
    basis_round_dir: str
    measured_round_dir: str

    @property
    def acceptance(self) -> dict[str, Any]:
        """Whether a measurement judged this prediction, and which one (#3481).

        Derived rather than passed in: a delta IS the judging. The vocabulary is
        :func:`~.forward_model.acceptance_block`'s, shared with the prediction
        record, so the two cannot spell the same fact differently.
        """
        return forward_model.acceptance_block(
            self.measured_round_dir if self.delta is not None else None
        )


def forward_model_verify_delta(
    basis: BankedRound,
    candidate: "forward_model.SummationCandidate",
    *,
    measured: BankedRound | None = None,
    phase: str = PHASE_MEASURE,
    position_deg: int = DESIGN_AXIS_DEG,
) -> ForwardModelDeltaResult:
    """Predict a summed response from ``basis``'s per-driver solos, and delta it
    against the VERIFY sum ``measured`` banked (ticket 4.5).

    The two halves the question needs: a PREDICTION BASIS (a banked take at
    ``position_deg`` carrying both driver solos, magnitude and phase, per ruling
    R9) and a MEASURED VERIFY SUM. Either absent, and the result says which.

    They come from two different banked rounds because that is where the flow
    puts them (#3482): the measure stage walks the solos and never reaches
    VERIFY; the verify stage measures the sum in a NEW bundle under a new capture
    session id. So the join is disclosed on the result. ``candidate`` is a
    PARAMETER rather than the round's incumbent — the question is usually what
    some candidate WOULD have measured.
    """

    measured = basis if measured is None else measured
    dirs = {
        "basis_round_dir": str(basis.round_dir),
        "measured_round_dir": str(measured.round_dir),
    }
    verify_curve, reason = _banked_verify_curve(measured.inputs)
    if verify_curve is None:
        return ForwardModelDeltaResult(None, reason, **dirs)
    measured_freqs_hz, measured_db = verify_curve
    try:
        pair = forward_model.load_branch_pair(
            basis.session_dir, phase=phase, position_deg=position_deg
        )
    except forward_model.ForwardModelError as exc:
        return ForwardModelDeltaResult(None, str(exc), **dirs)
    if pair is None:
        return ForwardModelDeltaResult(
            None,
            f"no {phase} take at {position_deg} deg banks both driver solos",
            **dirs,
        )
    predicted = forward_model.predict_sum(pair, candidate)
    try:
        delta = forward_model.predicted_minus_measured_db(
            predicted, measured_freqs_hz, measured_db
        )
    except forward_model.ForwardModelError as exc:
        return ForwardModelDeltaResult(None, str(exc), **dirs)
    return ForwardModelDeltaResult(delta, "", **dirs)


# --------------------------------------------------------------------------- #
# View 3 — every seat comparable, including one a round has no position row for
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SeatCurve:
    """One position's (or the VERIFY pose's) curve, normalised against its
    own median level over ``norm_band_hz`` — so a level difference between
    rounds or pipelines cannot masquerade as a shape difference."""

    position_id: str
    role: str
    normalized_db: np.ndarray


def per_seat_curves(
    banked: BankedRound,
    verify: PositionCurve | None = None,
    *,
    norm_band_hz: tuple[float, float] = (400.0, 8000.0),
) -> tuple[SeatCurve, ...]:
    """Every banked position plus, when supplied, the VERIFY pose — all
    normalised onto a comparable basis.

    Each curve is expressed as its own deviation from its own median level over
    ``norm_band_hz``. That is what makes the VERIFY pose — captured through an
    entirely different DSP path — comparable to the banked cloud positions with
    no cross-calibration assumption: only SHAPE is compared, never level.
    """
    # Asked BEFORE the norm band, so a round that banked no cloud group is told
    # what it is missing rather than that its empty grid has no bins in the band.
    positions = banked.graded_positions
    grid = np.asarray(banked.curve_grid_hz, dtype=float)
    sel = (grid >= norm_band_hz[0]) & (grid <= norm_band_hz[1])
    if not np.any(sel):
        raise RoundViewsError(f"norm band {norm_band_hz} has no bins on this round's curve grid")

    def _seat(position_id: str, role: str, curve_db: np.ndarray) -> SeatCurve:
        curve_db = np.asarray(curve_db, dtype=float)
        return SeatCurve(position_id, role, curve_db - float(np.median(curve_db[sel])))

    seats = [_seat(p.position_id, p.role, p.magnitude_db) for p in positions]
    if verify is not None:
        seats.append(_seat(verify.position_id, verify.role, verify.magnitude_db))
    return tuple(seats)


# --------------------------------------------------------------------------- #
# View 4 — session-to-session repeatability of the honest pooled figures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RepeatabilityMetric:
    """One metric's spread across the compared rounds, plus each round's own
    value keyed by the round label the caller supplied.

    ``degrees`` is the BEARING each round banked for this row, empty on a pooled
    role metric. It exists because a position id stopped naming the same bearing
    across the 2026-08-24 geometry ruling, which put the design axis at the front
    of the post-apply pose set: ``cloud_verify_02`` was −7° before it and 0°
    after. A spread taken across that boundary is the difference between two
    different seats.

    It DISCLOSES rather than refuses: this is an interpretation question, and
    comparing a pre-ruling round to a post-ruling one is legitimate.
    :meth:`bearings_agree` names the answer; what to do with ``False`` is the
    reader's call.
    """

    name: str
    values: dict[str, float]
    #: ``{round label: bearing}``, only for rows that HAVE one. A label absent
    #: from this map recorded no bearing for the row, which is why
    #: :meth:`bearings_agree` answers ``None`` rather than ``True`` below two
    #: known bearings: "nothing disagreed" and "nothing was comparable" differ.
    degrees: dict[str, float] = field(default_factory=dict)

    def bearings_agree(self) -> bool | None:
        """Whether every round that recorded a bearing recorded the SAME one.

        ``None`` means unknowable here — fewer than two rounds recorded one, so
        there is no comparison to make. Never ``True`` by default.
        """
        known = list(self.degrees.values())
        if len(known) < 2:
            return None
        return len(set(known)) == 1

    def spread(self) -> dict[str, float] | None:
        vs = list(self.values.values())
        if len(vs) < 2:
            return None
        mean = sum(vs) / len(vs)
        variance = sum((v - mean) ** 2 for v in vs) / (len(vs) - 1)
        return {
            "n": float(len(vs)),
            "mean": mean,
            "range": max(vs) - min(vs),
            "sd": variance**0.5,
            "min": min(vs),
            "max": max(vs),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "values": self.values,
            "spread": self.spread(),
            "degrees": self.degrees,
            "bearings_agree": self.bearings_agree(),
        }


@dataclass(frozen=True)
class RepeatabilityResult:
    """The stop-criterion table: per-round pooled figures, their spread
    (the measured repeat noise), and per-position stability."""

    round_labels: tuple[str, ...]
    metrics: tuple[RepeatabilityMetric, ...]
    per_position: tuple[RepeatabilityMetric, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_labels": list(self.round_labels),
            "metrics": [m.to_dict() for m in self.metrics],
            "per_position": [m.to_dict() for m in self.per_position],
        }


def repeatability_spread(
    rounds: Sequence[tuple[str, BankedRound]], *, primary_role: str = DEFAULT_PRIMARY_ROLE
) -> RepeatabilityResult:
    """Session-to-session spread of the pooled honest figures, plus each round's
    own value.

    ``rounds`` is ``(label, banked_round)`` pairs; the label is whatever the
    caller wants printed. Every round is graded SHIPPED (no frozen substitution)
    through the PUBLIC
    :func:`~jasper.active_speaker.flat_spec_views.role_split_flatness`, which
    reports BOTH poolings per role — ``rms_db``, the shipped per-bin weighting,
    and ``log_rms_db``, the per-octave re-weighting — and this view carries both
    through under those names. ``primary_role`` is a seam requirement of that
    signature, not a repeatability policy: the split is immediately recombined.
    """
    role_pooled: dict[str, dict[str, float]] = {}
    log_role_pooled: dict[str, dict[str, float]] = {}
    linear_pooled: dict[str, float] = {}
    position_values: dict[str, dict[str, float]] = {}
    position_degrees: dict[str, dict[str, float]] = {}
    for label, banked in rounds:
        split = role_split_flatness(
            banked.graded_report, banked.graded_positions, primary_role=primary_role,
        )
        roles = ([split.primary] if split.primary is not None else []) + list(split.others)
        for role_flatness in roles:
            if role_flatness.rms_db is not None:
                role_pooled.setdefault(role_flatness.role, {})[label] = role_flatness.rms_db
            if role_flatness.log_rms_db is not None:
                log_role_pooled.setdefault(role_flatness.role, {})[label] = role_flatness.log_rms_db
            for position_flatness in role_flatness.positions:
                if position_flatness.rms_db is not None:
                    position_values.setdefault(position_flatness.position_id, {})[
                        label
                    ] = position_flatness.rms_db
                    # The seat's banked bearing rides beside its number, so a
                    # comparison spanning the geometry ruling is VISIBLE. Only
                    # recorded bearings are stored, which is what makes
                    # ``bearings_agree()`` answer None rather than invent it.
                    if position_flatness.degrees is not None:
                        position_degrees.setdefault(position_flatness.position_id, {})[
                            label
                        ] = float(position_flatness.degrees)
        # The SHIPPED linear-pooled figure — spec_convergence_residual's own
        # number, lifted from the report rather than recomputed — so a caller can
        # see whether the number the tournament actually reads repeats too.
        residual = flat_spec.spec_convergence_residual(banked.graded_report)
        if residual.evaluable and residual.rms_db is not None:
            linear_pooled[label] = float(residual.rms_db)

    labels = tuple(label for label, _banked in rounds)
    metrics = [RepeatabilityMetric(SHIPPED_POOL_METRIC, dict(linear_pooled))]
    for role in sorted(role_pooled):
        metrics.append(RepeatabilityMetric(f"{role}_linear_pooled_db", dict(role_pooled[role])))
    for role in sorted(log_role_pooled):
        metrics.append(RepeatabilityMetric(f"{role}_log_pooled_db", dict(log_role_pooled[role])))
    per_position_metrics = [
        RepeatabilityMetric(seat, dict(values), dict(position_degrees.get(seat, {})))
        for seat, values in sorted(position_values.items())
    ]
    return RepeatabilityResult(
        round_labels=labels, metrics=tuple(metrics), per_position=tuple(per_position_metrics)
    )


def repeat_floor_provenance(label: str, banked: BankedRound) -> dict[str, Any]:
    """One record row naming what produced a repeat — the packet fields a
    floor cites. Basename only: the record leaves this laptop and a local
    path is nobody's provenance."""
    session = _mapping(banked.packet.get("session"))
    identity = _mapping(banked.packet.get("identity"))
    return {
        "label": Path(label).name,
        "bundle_session_id": session.get("bundle_session_id"),
        "graph_fingerprint": identity.get("graph_fingerprint"),
        "mic_calibration_id": _mapping(identity.get("mic")).get("calibration_id"),
        "started_at": session.get("started_at"),
    }


# --------------------------------------------------------------------------- #
# View 5 — Agreement: per-seat sign/magnitude testimony for every feature
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgreementFeature:
    """One local excursion in the pooled curve, and how every seat testifies
    to it — sign agreement and magnitude agreement, reported separately
    (the campaign's own finding: a feature can agree in sign everywhere and
    still split badly in size, which a single collapsed verdict would hide).
    """

    center_hz: float
    band_hz: tuple[float, float]
    pooled_db: float
    seat_values_db: dict[str, float]
    n_testify: int
    n_dissent: int
    spread_db: float
    ratio: float
    #: ``True``/``False`` when ``len(seats) >= AGREEMENT_TESTIFY_MIN``; ``None``
    #: below it, where that threshold cannot be satisfied by construction — a
    #: NAMED not-evaluable state, never a vacuous boolean. See
    #: :func:`agreement_table`.
    common_mode: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_hz": self.center_hz,
            "band_hz": list(self.band_hz),
            "pooled_db": self.pooled_db,
            "seat_values_db": self.seat_values_db,
            "n_testify": self.n_testify,
            "n_dissent": self.n_dissent,
            "spread_db": self.spread_db,
            "ratio": self.ratio,
            "common_mode": self.common_mode,
        }


def _local_features(
    grid: np.ndarray, pooled: np.ndarray, *, lo_hz: float, hi_hz: float, feature_db: float
) -> list[tuple[int, int, int]]:
    """``(center_idx, lo_idx, hi_idx)`` for every local extremum of
    ``|pooled| >= feature_db`` inside ``[lo_hz, hi_hz]``, half-depth edges."""
    band = (grid >= lo_hz) & (grid <= hi_hz)
    idx = np.where(band)[0]
    found: list[tuple[int, int, int]] = []
    for k in range(1, len(idx) - 1):
        i = idx[k]
        v = pooled[i]
        if abs(v) < feature_db:
            continue
        if v > 0 and not (pooled[i] >= pooled[i - 1] and pooled[i] >= pooled[i + 1]):
            continue
        if v < 0 and not (pooled[i] <= pooled[i - 1] and pooled[i] <= pooled[i + 1]):
            continue
        half = abs(v) / 2.0
        a = i
        while a > idx[0] and abs(pooled[a]) > half and np.sign(pooled[a]) == np.sign(v):
            a -= 1
        b = i
        while b < idx[-1] and abs(pooled[b]) > half and np.sign(pooled[b]) == np.sign(v):
            b += 1
        found.append((i, a, b))
    merged: list[tuple[int, int, int]] = []
    for i, a, b in found:
        if merged and a <= merged[-1][2]:
            prev_i, prev_a, prev_b = merged[-1]
            if abs(pooled[i]) > abs(pooled[prev_i]):
                merged[-1] = (i, min(a, prev_a), max(b, prev_b))
            else:
                merged[-1] = (prev_i, min(a, prev_a), max(b, prev_b))
        else:
            merged.append((i, a, b))
    return merged


def default_agreement_lo_hz(banked: BankedRound) -> float:
    """The trusted sweep's low edge when a caller does not name one.

    The round's OWN trusted floor when it recorded one — sweeping below a
    session's own honesty floor grades bins that session could not vouch for.
    Falls back to :data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ`'s
    edge rather than a campaign-specific literal (the previous default, 357.14
    Hz, was one session's floor at its particular 7 ms gate window).
    """
    floor = banked.graded_report.trusted_floor_hz
    return float(floor) if floor is not None else float(REFERENCE_BAND_HZ[0])


def agreement_table(
    seats: Sequence[SeatCurve],
    grid: np.ndarray,
    *,
    lo_hz: float,
    hi_hz: float,
    feature_db: float = 0.4,
    testify_db: float = 0.4,
    magnitude_ratio_ok: float = 3.0,
) -> tuple[AgreementFeature, ...]:
    """Every feature in ``[lo_hz, hi_hz]``, with per-seat testify/dissent counts
    and a magnitude-agreement ratio.

    ``testify`` = same sign as the pooled curve AND ``|seat| >= testify_db``;
    ``dissent`` = opposite sign AND ``|seat| >= testify_db``. ``common_mode``
    requires BOTH sign agreement (``n_testify >= AGREEMENT_TESTIFY_MIN`` and
    ``n_dissent <= AGREEMENT_DISSENT_MAX``) AND magnitude agreement
    (``ratio <= magnitude_ratio_ok``).

    The sign-agreement counts are the campaign's own LITERAL thresholds, not a
    seat-count-relative generalisation: scaling testify to ``len(seats) - 1``
    demands 4 at the 5-seat default where the measurement-validated frame demands
    3, and returns a vacuous ``True`` at 1-2 seats. Below
    :data:`AGREEMENT_TESTIFY_MIN` seats ``common_mode`` is ``None``, while
    ``n_testify``, ``n_dissent``, ``spread_db`` and ``ratio`` are still reported
    at any seat count — they are measurements, not verdicts.
    """
    grid = np.asarray(grid, dtype=float)
    if not seats:
        raise RoundViewsError("agreement_table: no seats supplied")
    # Power-mean, unlike the campaign's dB mean: compare its published
    # tables by verdict, never cell for cell.
    detrended = np.vstack([detrend(seat.normalized_db, grid) for seat in seats])
    pooled = detrended.mean(axis=0)
    features = []
    n_seats = len(seats)
    for i, a, b in _local_features(grid, pooled, lo_hz=lo_hz, hi_hz=hi_hz, feature_db=feature_db):
        seat_values = detrended[:, a : b + 1].mean(axis=1)
        p = float(pooled[a : b + 1].mean())
        sign = np.sign(p) if p != 0 else 1.0
        testify = int(np.sum((np.sign(seat_values) == sign) & (np.abs(seat_values) >= testify_db)))
        dissent = int(np.sum((np.sign(seat_values) != sign) & (np.abs(seat_values) >= testify_db)))
        spread = float(seat_values.max() - seat_values.min())
        ratio = float(np.abs(seat_values).max() / max(np.abs(seat_values).min(), 0.01))
        common_mode: bool | None
        if n_seats < AGREEMENT_TESTIFY_MIN:
            common_mode = None
        else:
            sign_ok = testify >= AGREEMENT_TESTIFY_MIN and dissent <= AGREEMENT_DISSENT_MAX
            common_mode = bool(sign_ok and ratio <= magnitude_ratio_ok)
        features.append(
            AgreementFeature(
                center_hz=float(grid[i]),
                band_hz=(float(grid[a]), float(grid[b])),
                pooled_db=p,
                seat_values_db={seat.position_id: float(v) for seat, v in zip(seats, seat_values)},
                n_testify=testify,
                n_dissent=dissent,
                spread_db=spread,
                ratio=ratio,
                common_mode=common_mode,
            )
        )
    return tuple(features)


# --------------------------------------------------------------------------- #
# View 6 — audibility-weighted co-metrics: NBD + SM (Olive 2004 /
# US 8,311,232 B2), ADR-0202, ticket 6.13
# --------------------------------------------------------------------------- #

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
    rig does not capture, and the name is deliberate (ADR-0202 / ticket 6.13).
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
    graded round (ADR-0202, ticket 6.13).

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


# --------------------------------------------------------------------------- #
# Measured per-angle directivity (#3865)
# --------------------------------------------------------------------------- #


def directivity_view(banked: BankedRound) -> DirectivityTable:
    """This round's cloud seats as departures from their on-axis reference.

    Per graded band each seat's difference splits into a level offset (the
    band's directivity index, which a trim can remove) and the shape residual
    it cannot — the arithmetic is
    :func:`~jasper.active_speaker.flat_spec_views.directivity_table`'s.
    **Observed only: no grade moves.**

    A round banked before the seat bearings were written still answers, as a
    table with ``angles_recorded`` false and every ``degrees`` ``None``:
    role-labelled directivity is a narrower reading, not an unreadable round.
    """
    return directivity_table(
        banked.graded_report,
        banked.graded_positions,
        reference_role=DEFAULT_PRIMARY_ROLE,
    )


# --------------------------------------------------------------------------- #
# Did the cloud's null evidence BIND the linearization fit?
# --------------------------------------------------------------------------- #


#: The round banked no linearization fit to run a counterfactual against.
CLOUD_BINDING_NO_FIT = "round_banked_no_linearization_fit"

#: The round banked a fit but no cloud exclusion evidence, so there is no
#: input to sever.
CLOUD_BINDING_NO_CLOUD_EVIDENCE = "round_banked_no_cloud_exclusion_evidence"

#: The MEASURE take carries no curve able to rebuild the fit's own inputs —
#: a round banked before the per-occurrence ``validity_floor_hz`` and
#: ``repeat_curves`` rode on it. Nothing here can be reconstructed from what
#: such a round holds; re-run the round to ask this question of it.
CLOUD_BINDING_FIT_INPUTS_NOT_BANKED = "fit_inputs_not_banked"

#: The fit is per-branch and the sibling's occurrence count gates the sigma
#: term, so the counterfactual is stated for a PAIR. A 1-way main is a
#: different question, not a broken round.
CLOUD_BINDING_NOT_A_PAIR = "fit_roles_not_a_pair"

#: The round's ``linearization`` entries are the PRESCRIBED shape, not the
#: fitted one — a candidate may read ``fit_failed`` and still carry prescribed
#: filters, and the entry's ``prescribed_by`` is what tells them apart. There
#: is no fit to run a counterfactual against, which is a different answer from
#: a reconstruction that drifted.
CLOUD_BINDING_NOT_FITTED = "round_linearization_was_prescribed_not_fitted"

#: A fitted entry that names no mic tier the envelope knows, or no driver
#: class. The refit cannot be composed without inventing one of them.
CLOUD_BINDING_ENTRY_INCOMPLETE = "banked_fit_entry_names_no_tier_or_driver_class"

#: The cloud block is present and malformed — a band row that is not a pair,
#: or a missing ``n_positions``. Not the same as a round that banked none.
CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE = "cloud_exclusion_evidence_unreadable"

#: The refit with every input wired did not reproduce the banked fit, so the
#: severed arm cannot be read as the cloud's doing.
CLOUD_BINDING_REFIT_DRIFTED = "refit_does_not_reproduce_the_banked_fit"

#: How far the all-inputs-wired refit may sit from the banked correction curve
#: before this view refuses to report a counterfactual, dB.
#:
#: The residue being allowed for is the BANKED GRID: a take banks its curves at
#: ``spatial.LATERAL_EVIDENCE_POINTS_PER_OCTAVE`` (12/octave, 121 points),
#: while the session fitted the analysis rfft grid. Measured at 0.142 dB on a
#: synthetic two-bump driver with every other input identical; 0.5 dB is that
#: with margin, and is far below the 7.975 dB a missing fit input costs. Not a
#: quality bar — a reconstruction check. Raise it only against a re-measured
#: decimation residue, never to make a drifted refit report.
REFIT_TOLERANCE_DB = 0.5

#: The smallest wired-vs-severed move this view will call BINDING, dB, under
#: the per-role reconstruction error. ``branch_target.SIGNIFICANT_GAIN_DB`` is
#: the gain below which a realized cascade is already treated as putting
#: nothing into a band, so a difference under it cannot correspond to a filter
#: the fit placed or removed. Without a floor, a round whose refit reproduces
#: EXACTLY — the good case — would report a 1e-13 dB cascade difference as
#: bound.
BOUND_FLOOR_DB = SIGNIFICANT_GAIN_DB

#: What the severed arm cuts: the three cloud terms ``compose_envelope`` takes
#: together. Boost permission is deliberately NOT severed — production also
#: withholds it when no cloud reached the envelope, but that is the conductor's
#: policy rather than the cloud's evidence, and cutting both at once would
#: credit the exclusion term with a change the permission flag caused.
SEVERED_CLOUD_INPUTS: tuple[str, ...] = (
    "excluded_bands_hz", "band_spread", "n_positions",
)


@dataclass(frozen=True)
class CloudBindingBand:
    """One octave band's answer to "did severing the cloud move the fit here"."""

    center_hz: float
    f_lo_hz: float
    f_hi_hz: float
    delta_db: float
    cloud_excluded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "center_hz": self.center_hz,
            "f_lo_hz": self.f_lo_hz,
            "f_hi_hz": self.f_hi_hz,
            "delta_db": self.delta_db,
            "cloud_excluded": self.cloud_excluded,
        }


@dataclass(frozen=True)
class CloudBindingRole:
    """One branch's wired-vs-severed comparison.

    ``refit_vs_banked_db`` is this role's own reconstruction check — the max
    absolute difference between the all-inputs-wired refit and the correction
    curve the round banked. ``bound`` is ``max_delta_db`` clearing that same
    number: a severed-arm move smaller than the reconstruction's own error is
    not evidence the cloud did anything.
    """

    role: str
    bound: bool
    max_delta_db: float
    refit_vs_banked_db: float
    n_filters_wired: int
    n_filters_severed: int
    bands: tuple[CloudBindingBand, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "bound": self.bound,
            "max_delta_db": self.max_delta_db,
            "refit_vs_banked_db": self.refit_vs_banked_db,
            "n_filters_wired": self.n_filters_wired,
            "n_filters_severed": self.n_filters_severed,
            "bands": [band.to_dict() for band in self.bands],
        }


@dataclass(frozen=True)
class CloudBindingView:
    """Whether this round's cloud null evidence actually BOUND its fit.

    ``not_evaluated_reason`` is empty exactly when ``evaluable`` is true.
    ``roles`` is empty on every refusal EXCEPT a drifted reconstruction, which
    keeps its per-role numbers because they are what says which branch failed
    to reproduce and by how much. **Observed only:
    no grade moves, and this answers only whether the evidence CHANGED the
    fitted prescription — never whether the wired answer was right.** The
    corpus carries no ground truth for that.
    """

    round_dir: str
    evaluable: bool
    not_evaluated_reason: str
    bound: bool | None
    refit_matches_banked: bool | None
    refit_vs_banked_db: float | None
    tolerance_db: float
    severed_inputs: tuple[str, ...]
    roles: tuple[CloudBindingRole, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_dir": self.round_dir,
            "evaluable": self.evaluable,
            "not_evaluated_reason": self.not_evaluated_reason,
            "bound": self.bound,
            "refit_matches_banked": self.refit_matches_banked,
            "refit_vs_banked_db": self.refit_vs_banked_db,
            "tolerance_db": self.tolerance_db,
            "severed_inputs": list(self.severed_inputs),
            "roles": [role.to_dict() for role in self.roles],
        }


def _not_evaluated(round_dir: Path, reason: str) -> CloudBindingView:
    return CloudBindingView(
        round_dir=str(round_dir),
        evaluable=False,
        not_evaluated_reason=reason,
        bound=None,
        refit_matches_banked=None,
        refit_vs_banked_db=None,
        tolerance_db=REFIT_TOLERANCE_DB,
        severed_inputs=SEVERED_CLOUD_INPUTS,
        roles=(),
    )


def _response_from_banked_curve(curve: Mapping[str, Any]):
    """One banked MEASURE curve as ``(DriverResponse, driven_band_hz)``, or
    ``None`` when the take predates the two inputs the fit needs.

    Both halves come back through the shipped inverse of
    :func:`~.spatial.pose_curve_record`, so "the banked curve" — its transfer
    function AND the band it was driven over — means here what it means to the
    delay landscape and the forward model. ``band_hz`` is optional on a banked
    curve and that parser already falls back to the grid extent, so it is never
    read off the mapping directly.

    The two fit inputs are decided on the KEY, not the value: an absent
    ``validity_floor_hz`` is a take from before the field rode here, while a
    present ``None`` is "no floor was resolved" — which ``compose_envelope``
    handles itself, and which re-running the round would not change.
    """
    from jasper.audio_measurement.program_analysis import DriverResponse

    parsed = position_cycle.parse_curve_complex(curve)
    if parsed is None:
        return None
    if "validity_floor_hz" not in curve or "repeat_curves" not in curve:
        return None
    floor = curve["validity_floor_hz"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float, type(None))):
        return None
    repeats = []
    for occurrence in curve["repeat_curves"] or ():
        repeat = _response_from_banked_curve(occurrence)
        if repeat is None:
            return None
        repeats.append(repeat[0])
    freqs, tf, band = parsed
    return DriverResponse(
        role=str(curve.get("role") or ""),
        freqs_hz=freqs,
        magnitude_db=20.0 * np.log10(np.abs(tf)),
        complex_tf=tf,
        gating={},
        snr=None,
        validity_floor_hz=None if floor is None else float(floor),
        repeat_responses=tuple(repeats),
    ), band


def _correction_db(
    filters: Sequence[Mapping[str, Any]], grid_hz: np.ndarray,
) -> np.ndarray:
    """A filter set's realized cascade magnitude on ``grid_hz``, dB.

    ``filters`` are the plain ``{biquad_type, freq, q, gain}`` records both a
    banked ``linearization[role].filters`` row and ``LinearizationFilter.
    to_dict`` are, so the refit and the fit it is compared against reach the
    one biquad evaluator by the same door.
    """
    from jasper.active_speaker.branch_chain import chain_response

    if not filters:
        return np.zeros_like(grid_hz)
    return 20.0 * np.log10(
        np.maximum(np.abs(chain_response(list(filters), grid_hz)), 1e-12)
    )


def _cloud_inputs(evidence: Mapping[str, Any]):
    """``compose_envelope``'s three cloud arguments off the banked block, or
    ``None`` when the block is present and cannot supply them.

    The three travel together — ``compose_envelope`` requires them as a set —
    so they are validated as a set rather than read one at a time.
    """
    positions = evidence.get("n_positions")
    if isinstance(positions, bool) or not isinstance(positions, (int, float)):
        return None
    bands = []
    for row in evidence.get("excluded_bands_hz") or ():
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return None
        try:
            bands.append((float(row[0]), float(row[1])))
        except (TypeError, ValueError):
            return None
    try:
        spread = tuple(
            BandSpread(**dict(row)) for row in evidence.get("band_spread") or ()
        )
    except TypeError:
        return None
    return tuple(bands), spread, int(positions)


def cloud_binding_view(banked: BankedRound) -> CloudBindingView:
    """Re-fit this round's linearization with the cloud's null evidence cut,
    and report whether that evidence BOUND the fit.

    The counterfactual the corpus can answer: the production ``cloud is None``
    branch of
    :func:`~.intervention.plan_linearization` hands ``compose_envelope`` its
    three cloud arguments as ``None`` together, so re-composing each branch's
    envelope that way and re-fitting shows what the null evidence contributed —
    where, and how much. It cannot show the wired answer was RIGHT: nothing
    banked is ground truth for the prescription.

    **It refuses rather than reporting a drifted reconstruction.** The wired
    arm is refitted from the round's own banked inputs first and compared with
    the correction curve the round banked; past :data:`REFIT_TOLERANCE_DB` the
    view is not evaluable, because a severed arm is only readable against a
    wired arm that reproduces. That check is also what makes the two inputs
    this refit cannot recover from the bank safe to leave at their defaults:
    a session whose ``boost_excluded_bands_hz`` was non-empty, or which
    withheld boost, fails it instead of quietly reporting.

    Observed only; no grade moves and ``round_receipt.json`` is untouched.
    """
    from jasper.active_speaker.branch_chain import radiating_band_hz, sections_by_role
    from jasper.active_speaker.branch_target import branch_target
    from jasper.active_speaker.crossover_v2.intervention import compose_sigma_db
    from jasper.active_speaker.linearization_envelope import compose_envelope
    from jasper.active_speaker.linearization_fit import (
        FitVocabulary,
        core_level_band_hz,
        fit_driver_linearization,
        measurement_hole_bands_hz,
    )
    from jasper.active_speaker.profile import CrossoverRegion

    round_dir = banked.round_dir
    artifact_dir, _why = round_artifact_dir(banked.session_dir)
    candidate = {} if artifact_dir is None else _read_candidate(artifact_dir)
    linearization = _mapping(candidate.get("linearization"))
    if not linearization:
        return _not_evaluated(round_dir, CLOUD_BINDING_NO_FIT)
    evidence = _mapping(candidate.get("exclusion_evidence"))
    if not evidence.get("excluded_bands_hz"):
        return _not_evaluated(round_dir, CLOUD_BINDING_NO_CLOUD_EVIDENCE)
    roles = tuple(sorted(linearization))
    if len(roles) != 2:
        return _not_evaluated(round_dir, CLOUD_BINDING_NOT_A_PAIR)
    entries = {role: _mapping(linearization[role]) for role in roles}
    # A candidate may read ``fit_failed`` and still carry PRESCRIBED filters;
    # the entry's own ``prescribed_by`` is the discriminator, per
    # ``MeasuredCrossoverCandidate``'s two-shape rule.
    if any(entry.get("prescribed_by") for entry in entries.values()):
        return _not_evaluated(round_dir, CLOUD_BINDING_NOT_FITTED)
    tiers = {role: str(entries[role].get("mic_tier") or "") for role in roles}
    classes = {
        role: str(entries[role].get("driver_class") or "") for role in roles
    }
    if any(tiers[role] not in MIC_TIERS or not classes[role] for role in roles):
        return _not_evaluated(round_dir, CLOUD_BINDING_ENTRY_INCOMPLETE)
    cloud = _cloud_inputs(evidence)
    if cloud is None:
        return _not_evaluated(round_dir, CLOUD_BINDING_CLOUD_EVIDENCE_UNREADABLE)
    cloud_bands, band_spread, n_positions = cloud

    pair = position_cycle.read_pose_curve_pair(
        banked.session_dir,
        phase=PHASE_MEASURE,
        position_deg=DESIGN_AXIS_DEG,
        roles=roles,
    )
    if pair is None:
        return _not_evaluated(round_dir, CLOUD_BINDING_FIT_INPUTS_NOT_BANKED)
    read = {
        role: _response_from_banked_curve(curve)
        for role, curve in zip(roles, pair[:2])
    }
    if any(entry is None for entry in read.values()):
        return _not_evaluated(round_dir, CLOUD_BINDING_FIT_INPUTS_NOT_BANKED)
    responses = {role: entry[0] for role, entry in read.items()}
    # The band each role was DRIVEN over, as the curve recorded it — so the
    # envelope is composed over the span the session composed it over.
    excited = {role: entry[1] for role, entry in read.items()}
    sections = sections_by_role(
        CrossoverRegion.from_mapping(region)
        for region in _mapping(candidate.get("source_preset")).get(
            "crossover_regions"
        ) or ()
    )
    role_sections = {role: sections.get(role, ()) for role in roles}
    radiating = {role: radiating_band_hz(role_sections[role]) for role in roles}

    def _fit_pair(*, wired: bool) -> dict[str, Any]:
        """Both branches, composed before either is fitted — the PR-L5 order."""
        envelopes = {
            role: compose_envelope(
                role, responses[role],
                excited_band_hz=excited[role],
                mic_tier=tiers[role],
                driver_class=classes[role],
                sigma_db=compose_sigma_db(
                    responses[role],
                    responses[next(other for other in roles if other != role)],
                    tier=tiers[role],
                    valid_band_hz=excited[role],
                ),
                excluded_bands_hz=cloud_bands if wired else None,
                band_spread=band_spread if wired else None,
                n_positions=n_positions if wired else None,
            )
            for role in roles
        }
        # A hole belongs to the PAIR, so it is derived from both core bands and
        # handed to each fit, exactly as the composer does it.
        blind = measurement_hole_bands_hz([
            core_level_band_hz(envelopes[role], radiating_band_hz=radiating[role])
            for role in roles
        ])
        return {
            role: fit_driver_linearization(
                responses[role], envelopes[role],
                vocabulary=FitVocabulary(allow_boost=True),
                radiating_band_hz=radiating[role],
                blind_bands_hz=blind,
                target=branch_target(role_sections[role], envelopes[role].freqs_hz),
            )
            for role in roles
        }

    wired_fits, severed_fits = _fit_pair(wired=True), _fit_pair(wired=False)

    grid_hz = DEFAULT_ENVELOPE_GRID_HZ
    octaves = octave_bands_hz(float(grid_hz[0]), float(grid_hz[-1]))
    results: list[CloudBindingRole] = []
    worst_reconstruction = 0.0
    for role in roles:
        wired_db = _correction_db(
            [f.to_dict() for f in wired_fits[role].filters], grid_hz
        )
        severed_db = _correction_db(
            [f.to_dict() for f in severed_fits[role].filters], grid_hz
        )
        banked_db = _correction_db(entries[role].get("filters") or (), grid_hz)
        reconstruction = float(np.max(np.abs(wired_db - banked_db)))
        worst_reconstruction = max(worst_reconstruction, reconstruction)
        delta = np.abs(wired_db - severed_db)
        bands = tuple(
            CloudBindingBand(
                center_hz=center,
                f_lo_hz=lo,
                f_hi_hz=hi,
                delta_db=float(np.max(delta[(grid_hz >= lo) & (grid_hz <= hi)])),
                cloud_excluded=any(
                    lo <= hi_null and lo_null <= hi for lo_null, hi_null in cloud_bands
                ),
            )
            for center, lo, hi in octaves
        )
        max_delta = float(np.max(delta))
        results.append(CloudBindingRole(
            role=role,
            # Against this role's OWN reconstruction error, floored: a move
            # smaller than what the refit already mis-states, or than the
            # smallest gain a realized cascade puts anywhere, is not evidence
            # the cloud did anything.
            bound=max_delta > max(reconstruction, BOUND_FLOOR_DB),
            max_delta_db=max_delta,
            refit_vs_banked_db=reconstruction,
            n_filters_wired=len(wired_fits[role].filters),
            n_filters_severed=len(severed_fits[role].filters),
            bands=bands,
        ))

    if worst_reconstruction > REFIT_TOLERANCE_DB:
        return replace(
            _not_evaluated(round_dir, CLOUD_BINDING_REFIT_DRIFTED),
            refit_matches_banked=False,
            refit_vs_banked_db=worst_reconstruction,
            # WHICH branch failed to reproduce, and by how much: this refusal
            # is the view's own self-check, so the numbers behind it travel.
            roles=tuple(results),
        )
    return CloudBindingView(
        round_dir=str(round_dir),
        evaluable=True,
        not_evaluated_reason="",
        bound=any(role.bound for role in results),
        refit_matches_banked=True,
        refit_vs_banked_db=worst_reconstruction,
        tolerance_db=REFIT_TOLERANCE_DB,
        severed_inputs=SEVERED_CLOUD_INPUTS,
        roles=tuple(results),
    )
