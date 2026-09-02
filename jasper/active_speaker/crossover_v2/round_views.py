# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The round-grading comparison views a laptop campaign had re-derived by hand.

Product promotion of four tools that lived under
``captures/linearization-night-2026-08-19/tools/`` — ``frozen_reference.py``,
``per_seat.py``, ``repeatability.py``, and ``agreement.py`` — which computed
the numbers the crossover-v2 tournament's "dominance decides" rule was
actually judged on (issue #2769). This module is the *reading and grading*
half of that campaign; every DSP and grading primitive it uses is imported
from the seam that already owns it:

* :mod:`~jasper.active_speaker.crossover_v2.evidence_packet` reads a banked
  round's bundle (this module never globs ``cloud_verify.json`` by hand) and
  its persisted ``spec`` block rehydrates through
  :meth:`~jasper.active_speaker.flat_spec.FlatSpecReport.from_dict` — the
  product's own inverse of ``to_dict()``, not a private per-caller copy.
* :mod:`~jasper.active_speaker.flat_spec` grades a curve
  (:func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`); this module
  never re-derives a reference level or a band tolerance.
* :mod:`~jasper.active_speaker.flat_spec_views` re-reads an already-graded
  report per position/role. :func:`repeatability_spread` — no per-position
  reference to substitute — uses the PUBLIC
  :func:`~jasper.active_speaker.flat_spec_views.role_split_flatness`.
  :func:`frozen_reference_grade` reaches for the private
  :func:`~jasper.active_speaker.flat_spec_views._evaluate_position` /
  :func:`~jasper.active_speaker.flat_spec_views._pool` building blocks
  instead — the same ones the campaign's own ``frozen_reference.py`` used —
  because grading a position against a SUBSTITUTED per-position reference is
  exactly the one thing the public function has no parameter for; that
  private coupling is load-bearing there and nowhere else in this module.
* :mod:`~jasper.active_speaker.crossover_v2.durable_state` reads the round's
  banked VERIFY curve (this module never parses ``verify_priors`` by hand).

* :mod:`~jasper.active_speaker.crossover_v2.gate_sweep` reads one bin through
  a ladder of gate windows over the same round's captures
  (:func:`~jasper.active_speaker.crossover_v2.round_captures.discover_captures`);
  :func:`spec_with_gate_sensitivity` stamps that answer onto the round's own
  spec bands and computes none of it.

**This module performs no DSP of its own.** It did, in one place: the VERIFY
pose was the one curve a round did not carry pre-computed, so
:func:`verify_pose_curve` deconvolved, gated, smoothed and resampled its raw
dump-ring bytes. Ruling S3 banked the curve, so the re-derivation is gone and
the only transform left anywhere here is the ``fraction=1`` residual
:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave` call
:func:`agreement_table` takes through the product's own seam.

**Input shape**: a *banked round directory*, the tree
``scripts/bank-crossover-round.sh <dest-dir>`` produces —

.. code-block:: text

    <round-dir>/
      bundle/<session-id>/...        one active-speaker session bundle
      state.json                     crossover-v2 flow state (optional)
      design-draft.json              active-speaker design draft (optional)
      applied-profile.json           applied baseline profile SSOT (optional)
      repeat-floor.json              banked repeat floor (optional)

It comes in TWO shapes, because the two-stage flow banks one bundle per stage:
a MEASURE round (per-driver solos, the entry baseline, the lateral walk; no
cloud group and so no cloud seats and no graded spec) and a VERIFY round (the
verify capture and the cloud group). :func:`load_banked_round` reads either;
:class:`BankedRound` says which views need which.

**A seat's BEARING is read, and the position id alone is no longer enough.**
Every view here keys a position by its stable ``position_id``
(``f"{phase}_{index:02d}"``, assigned once by the walk driver), because that is
what lines the SAME prompted spot up across rounds — but the 2026-08-24
geometry ruling put the design axis at the front of the post-apply pose set,
so an id stopped naming a fixed bearing across that boundary:
``cloud_verify_02`` was −7° before it and 0° after; ``cloud_verify_04`` was
−22° and is now +7°. A spread taken across the ruling is the difference between
two different seats.

So :func:`load_banked_round` reads each row's own ``position_deg`` into
``PositionCurve.degrees`` (``None`` for a round banked before that writer, for
a vertical seat, and for a retake that declares no side — "not recorded", never
zero), and :func:`repeatability_spread` carries the bearings beside the numbers
so a mixed comparison is VISIBLE
(:meth:`RepeatabilityMetric.bearings_agree`). It discloses rather than refuses:
the doctrine's hard stops are component damage and hearing safety, and
comparing a pre-ruling round to a post-ruling one is a legitimate question a
guard would have blocked. The synthesized VERIFY pose keeps ``0.0``, which is
not a recovered angle but what that phase MEANS.

The campaign's ``frozen_reference.py`` carried a hardcoded
``index -> degrees`` table because the cloud record had no bearing of its own
to read. It is not ported, and now for a better reason: a second
hand-maintained copy of a fact the record already carries is only a place for
the two to drift.

**What this module still deliberately does NOT do.** It never joins a lateral
walk pose to a cloud seat. They are DIFFERENT captures — a per-driver
measurement and a summed sweep — not the same one with more detail, and both
count positions from the front of their own table, so a matching index between
them is a coincidence rather than a correspondence.
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
    PositionCurve,
    role_split_flatness,
    _evaluate_position,
    _exclusion_mask,
    _pool,
)
from jasper.active_speaker.repeat_floor import SHIPPED_POOL_METRIC
from jasper.active_speaker.crossover_v2 import forward_model
from jasper.active_speaker.crossover_v2.contracts import DESIGN_AXIS_DEG
from jasper.active_speaker.crossover_v2.durable_state import (
    verify_measured_curve_from_state,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    _mapping,
    CrossoverEvidencePacketError,
    build_crossover_evidence_packet,
)
from jasper.active_speaker.crossover_v2.feature_classifier import (
    load_round_pose_curves,
)
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
from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.audio_measurement.olive_metrics import nbd_and_sm

__all__ = [
    "AGREEMENT_DISSENT_MAX",
    "AGREEMENT_TESTIFY_MIN",
    "NOT_SWEPT_BAND_NOT_EVALUABLE",
    "NOT_SWEPT_BIN_OFF_ANALYSIS_GRID",
    "NOT_SWEPT_CAPTURES_UNREADABLE",
    "NOT_SWEPT_SINGLE_POSE",
    "AgreementFeature",
    "AudibilityCoMetrics",
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
    "default_agreement_lo_hz",
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

#: Mirrors ``.spatial.POSITION_ROLE_ONAX`` as a local literal rather
#: than importing that (large, orchestration-heavy) module for one string.
#: :mod:`.flat_spec_views` follows the same policy for the same reason — see
#: its ``PositionCurve.role`` docstring: this package never owns that
#: constant, it only takes it as a caller-supplied value.
DEFAULT_PRIMARY_ROLE = "onax"

#: The synthetic role/position-id this module mints for a VERIFY-phase
#: capture, which a round's bundle never carries a ``positions`` row for.
VERIFY_ROLE = "verify"
VERIFY_POSITION_ID = "verify"

#: The campaign's own literal agreement thresholds (``agreement.py``:
#: ``test >= 3 and diss <= 1``), NOT a seat-count-relative generalisation —
#: see :func:`agreement_table` for why a generalisation is the wrong port.
AGREEMENT_TESTIFY_MIN = 3
AGREEMENT_DISSENT_MAX = 1

# --------------------------------------------------------------------------- #
# Loading one round
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BankedRound:
    """One round — banked or still live on the box — read once, ready for
    every view below. :attr:`inputs` says which of the two it was and where
    its three non-bundle inputs came from.

    ``report`` carries the round's own grading frame (trusted floor,
    published exclusion intervals, and the full per-band evaluation, needed
    for :func:`repeatability_spread`'s pooled figures). ``positions`` is built
    directly from the evidence packet's ``positions`` block; nothing here
    re-parses ``cloud_verify.json``.

    **Both are ABSENT on a round that banked no cloud group, and that is a
    round SHAPE rather than a defect** (#3478). Only the verify stage banks a
    cloud group (``capture_plan.STAGE1_INCLUDES_CLOUD_MEASURE`` is ``False``),
    so a measure-stage round — the only shape that produces an entry baseline
    — has an empty ``positions`` and a ``None`` ``report``. The two accessors
    below are where a view that NEEDS them says so; a view that does not
    (:func:`entry_state_grade`, the frequency projector over :attr:`packet`)
    reads the round it was given.
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

        TWO refusals, because two different things are missing. A round that
        banked no cloud group is the measure-stage SHAPE above; a block that
        says ``available`` with nothing left after the ``magnitude_db`` filter
        is a TRUNCATED packet from a round that did walk one
        (``evidence_packet._positions_block`` sets ``available`` to
        ``bool(rows)``, so the two cannot be confused). The shape sentence over
        a corrupt packet reads as "this round is fine, you asked it the wrong
        question".
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
    told apart HERE — a round banked before the 2026-08-24 geometry writer, a
    vertical seat that commanded no bearing, a geometry-locked retake that
    declares no side. The packet's own ``angle_deg`` block is where that
    distinction is published; a view only needs to know it has no number.

    ``bool`` is rejected before ``int`` because it subclasses it, so a
    hand-edited ``true`` would otherwise be published as a 1° bearing.
    """
    value = row.get("position_deg")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def load_banked_round(round_dir: Path) -> BankedRound:
    """Read one round — banked tree or LIVE session bundle — into a
    :class:`BankedRound`.

    Which of the two it is, and therefore where the flow state, design draft
    and applied profile come from, is :func:`~.round_inputs.round_inputs`'
    answer; it rides on :attr:`BankedRound.inputs` so the views that reach for
    the flow state read the same file this packet was built from rather than
    re-deriving a location from :attr:`BankedRound.round_dir`.

    Raises :class:`RoundViewsError` when the directory is neither shape, when
    a banked tree holds more than one session, or when the bundle carries no
    readable evidence packet.

    **It does NOT judge what the round banked** — a loader-level "positions
    and a graded spec, or nothing" refusal blocked the two views that need
    neither on the only shape that reaches them (#3478, #3482). What a view
    needs, the view says: :attr:`BankedRound.graded_report` and
    :attr:`BankedRound.graded_positions`.
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
            # The seat's OWN banked bearing, read rather than defaulted, since
            # the 2026-08-24 geometry ruling made the packet carry it. Absent
            # stays ``None`` — "not recorded", never zero, which is
            # ``PositionFlatness.degrees``' own documented contract — and that
            # is what a round banked before the writer reads as. ``bool`` is
            # excluded because it subclasses ``int`` and a stray ``true`` would
            # otherwise publish 1 as a bearing, the same guard the packet's own
            # angle block applies.
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


#: Why a band carries no gate sensitivity when the LADDER NEVER RAN on it.
#: The ``not_swept_`` prefix is what tells these apart from the sweep's own
#: refusals (``gate_sweep.NULL_*``), which mean the ladder ran and then
#: declined to publish; both land in the one
#: :attr:`~jasper.active_speaker.flat_spec.BandResult.gate_sensitivity_note`
#: field, and conflating "not measured" with "measured, inconclusive" is the
#: read this vocabulary exists to prevent.
NOT_SWEPT_SINGLE_POSE = "not_swept_single_pose"
NOT_SWEPT_BAND_NOT_EVALUABLE = "not_swept_band_not_evaluable"
#: Every ``RoundCapturesRefused`` the ladder can raise EXCEPT the single-pose
#: one: no captures, no programs, a capture that will not bind to its bytes, a
#: missing radiated band, and a radiated band that does not reach the sweep's
#: reference band. They are one word here because the answer is the same —
#: this round's captures did not become curves — and the refusal itself
#: carries the detail.
NOT_SWEPT_CAPTURES_UNREADABLE = "not_swept_captures_unreadable"
NOT_SWEPT_BIN_OFF_ANALYSIS_GRID = "not_swept_bin_outside_analysis_grid"


def _stamped_band(
    band: flat_spec.BandResult,
    feature: Mapping[str, Any] | None,
    note: str | None,
    detail: dict[str, Any] | None,
) -> flat_spec.BandResult:
    """One band plus the ladder's read at its own worst bin, or the reason why not.

    ``n_valid_rungs`` is stamped whenever the ladder RAN, including on a null:
    it is the denominator behind the two numbers beside it, and a reader
    weighing ``insufficient_valid_rungs`` needs to see how few.

    ``detail`` carries only beside a capture-refusal note -- the two
    ``RoundCapturesRefused`` buckets -- and is ``None`` for every other note,
    swept or not.
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
    )


def spec_with_gate_sensitivity(
    banked: BankedRound, *, rungs_ms: Sequence[float] = DEFAULT_RUNGS_MS,
) -> FlatSpecReport:
    """The round's graded spec, with "room or speaker" answered at every band's
    own worst bin.

    The spec verdict names the bin; :mod:`.gate_sweep` says whether that bin
    moves with the analysis window. Before this the two lived one
    ``jasper-gate-sweep --at-hz <bin>`` apart and a reader had to remember to
    cross them, so the answer travelled only as far as whoever ran the second
    command. Now each :class:`~jasper.active_speaker.flat_spec.BandResult`
    carries the ladder's headline at its own
    ``max_deviation_hz`` and the report carries the frame those numbers are
    stated in. **Disclosure only: no grade moves** — every field the evaluator
    set comes back untouched.

    **Why this reads a BANKED round rather than the live combine.** The cloud
    pipeline's seam (``spatial.assemble_cloud_group_result``) has the graded
    curve but not the impulse responses: ``CombinedResponse`` keeps each
    position's magnitude and echo diagnostic and drops the ``ir`` that
    ``detect_echo`` consumed. Reaching one there means the IR behind
    ``DriverResponse.complex_tf``, and that IR has ALREADY been through
    ``deconv.direct_arrival_window`` and the adaptive reflection gate, whose
    search stops at ``gating.SEARCH_T_MAX_MS`` — 7 ms. The ladder's 9, 12 and
    20 ms rungs would then read a window that was closed before they got
    there, and ``sigma_growth_ratio`` would come back at ~1.0 by
    construction: "it is the speaker", fabricated, on every band. A banked
    round's ``summed_*.wav`` is the raw capture, deconvolved here against its
    own program (:func:`~.round_captures.discover_captures`), and only that
    IR can answer what a longer window admits.

    Cost is one ladder pass over the round's captures: the round door's own
    ``discover_captures`` decode/deconvolve, then ONE
    :func:`~.gate_sweep.sweep_features` call at up to three bins. A band with
    no ``max_deviation_hz`` to name is skipped rather than swept, and a report
    that names none at all costs nothing — the captures are never read.

    Every way there can be no number is named in
    ``gate_sensitivity_note`` and none of them raises: a round whose
    captures cannot be read is a round with an ungraded window, not an
    ungraded spec. A capture refusal additionally stamps
    ``gate_sensitivity_detail`` with the specific input that was missing —
    the note names the bucket, the detail names the evidence.
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
            # rejects the whole CALL on one off-grid bin, which would cost
            # every other band its answer.
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
            # fewer than two captures, and this names that refusal apart from
            # a round whose captures would not read at all.
            refused = (
                NOT_SWEPT_SINGLE_POSE
                if exc.reason == REFUSE_SINGLE_POSE
                else NOT_SWEPT_CAPTURES_UNREADABLE
            )
            notes.update({index: refused for index, _hz in targets})
            # The bucket slug names only the shape; what was actually missing
            # rides on the exception the engine already raised, so a reader
            # of this ONE band need not go re-run the ladder to see it.
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


#: Why an entry state could not be graded, when the take IS banked but will not
#: rehydrate. ``EntryBaseline.from_dict`` owns that rule and answers ``None``
#: for every member of the set — a curve or mask that is not a list, a mask
#: whose length disagrees with the curve, a curve ``ResponseCurve`` itself
#: refuses (empty, length-mismatched, or non-finite), and a blank
#: ``program_id`` / ``reference_mark`` / ``graph_fingerprint`` /
#: ``captured_at``. This is what that ``None`` MEANS to an operator; that
#: reader does not say which member it was, and neither does this.
ENTRY_STATE_UNREADABLE = (
    "this round banked an entry_baseline take, but it does not rehydrate into "
    "a gradeable baseline — its curve, exclusion mask, or identity fields are "
    "absent, disagree in length, or are not finite"
)


@dataclass(frozen=True)
class EntryStateGrade:
    """The graph a round ENTERED on, graded — or the named reason it was not.

    **The gap this closes.** Every round grades what it PRODUCED and compares
    that against the state it started from, but the entry state itself was
    never graded as a first-class result: a fresh box wearing nothing but its
    declarations-derived config had no way to ask "how flat am I right now"
    short of an operator calling
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec` by hand. The
    measurement was always banked — the entry-baseline take is write-once,
    which is ruling S3's offline promise — so this is a reader, not a new
    capture and not a second grader.

    ``report`` is a real :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`
    from the shipped evaluator, so it carries the same per-band table the
    round's own ``spec`` block does and the two read side by side with no
    translation step.

    **The frame is the ROUND's, the exclusion mask is the TAKE's**, and the
    split is deliberate. The mask belongs to this capture: it is
    :func:`~.round_evidence._validity_clamp`'s output — the bins below THIS
    capture's own reflection gate, where the response is an artifact of a
    truncated gate window — not the cloud's interference screen, which is a
    different mask over a different capture. Grading this curve through the
    round's post-apply exclusions would report deviations at bins nobody
    vouched for, and screen bins this capture's own gate never invalidated.
    The floor and ceiling belong to the round, and :func:`_entry_frame` owns
    both why and what a round that graded no after is stated in instead.

    ``round_ordinal`` / ``round_ordinal_epoch`` are the round this entry state
    belongs to and which epoch of the ordinal sequence that ordinal counts in,
    read from the banked flow state. ``None`` for each is "not recorded" — a
    round banked before the field shipped, or a directory with no state file.
    They ride here because an unattributed table is not a disclosure: "the
    entry state was this flat" means one thing at round 1 of a fresh box and
    another at round 1 after a republish reset the count.

    ``available`` ``False`` carries a non-empty ``reason`` and no report;
    ``True`` carries a report and an empty ``reason``. A round that banked no
    take is a fact this door reports rather than a failure it raises —
    retention is fail-soft and never costs the household a retake.
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
            # WHICH entry state was graded. Without it the table is an
            # unattributed set of numbers: a first round's entry graph is the
            # declarations-derived config a fresh box wears, a later round's is
            # whatever the previous round left on the speaker, and the two are
            # told apart by this fingerprint and nothing else here.
            "graph_fingerprint": self.graph_fingerprint,
            "captured_at": self.captured_at,
            "artifact_ref": self.artifact_ref,
            # WHICH round, and which epoch of the count that round number
            # belongs to. Round 1 of a fresh box and round 1 after a reset are
            # the same ordinal and different facts.
            "round_ordinal": self.round_ordinal,
            "round_ordinal_epoch": self.round_ordinal_epoch,
            "report": None if self.report is None else self.report.to_dict(),
        }


def _banked_series_position(state_path: Path | None) -> tuple[int | None, int | None]:
    """``(round_ordinal, round_ordinal_epoch)`` off the round's flow state.

    ``(None, None)`` when the round names no readable flow state, and
    ``None`` for either field the record does not carry — "not recorded",
    never zero, on :attr:`PositionCurve.degrees`' rule. Read here rather than
    off the evidence packet because the packet's ``round_receipt`` block
    publishes identities and does not carry the ordinal.

    ``bool`` is rejected before ``int`` because it subclasses it, the same
    guard :func:`_row_degrees` applies for the same reason.
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
    """``(smoothing_fraction, frame_kwargs)`` for an entry baseline — the
    round's own when it graded one, nothing when it did not.

    The frame is the ROUND's so a before and an after are stated over one span
    (:class:`EntryStateGrade`). A round that banked no cloud group graded no
    after, so there is no span for this before to be made comparable with, and
    the honest frame is no frame: unclamped, on ``0`` — this module's own
    spelling for *not attested* (:func:`verify_pose_curve` reads the same
    ``0``). Nothing is inferred and nothing is defaulted to a literal; the
    emitted report echoes both clamps as ``None`` on its face, which is
    :class:`~jasper.active_speaker.flat_spec.FlatSpecReport`'s own "not
    stated", so the grade discloses which of the two frames produced it. The
    room's floor rides with them under the same rule and is READ BACK rather
    than re-derived: this grades an entry baseline in the ROUND's frame, and
    a floor recomputed here would be a second opinion about one room.
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
    rehydrates it through
    :meth:`~.round_evidence.EntryBaseline.from_dict` — which already owns the
    "length-agreeing curve and mask, or nothing" rule, so this door does not
    re-spell it — and hands the arrays to the shipped
    :func:`~jasper.active_speaker.flat_spec.evaluate_flat_spec`. Every number
    returned comes from that evaluator; see :class:`EntryStateGrade` for why
    the frame is the round's and the mask is the take's, and
    :func:`_entry_frame` for what a round that graded no after is stated in.

    **It requires no cloud group**, which is what makes it reachable at all:
    the measure stage is the only stage that banks an entry baseline and the
    only one that banks no cloud (#3478).

    ``packet["entry_baseline"]`` is indexed rather than fetched with a default:
    the packet is DERIVED fresh on every read by a builder that always emits
    this block (present-and-``available: False`` is how it reports a round that
    banked no take), so a missing key is a corrupt packet and belongs in the
    ``KeyError`` arm the CLI already treats as an unreadable round — not in a
    fallback sentence that can never be produced.
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
    """One target round graded twice: as shipped, and frozen to the
    baseline's per-position reference levels.

    ``shipped`` and ``frozen`` are ``{role: pooled_rms_db}``. The freeze
    removes exactly the one degree of freedom §8.9 found compensating a
    prescribed cut's level loss (grading each config against its OWN
    reference) — see the module docstring's linked campaign tool for the
    full derivation. ``target_own_refs`` / ``baseline_refs`` are the
    per-position reference levels each half actually used, so a caller can
    audit the freeze rather than trust it.
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
    """Grade ``target`` twice: shipped, and frozen to ``baseline``'s
    per-position reference levels.

    ``target`` may be the same round as ``baseline`` (frozen == shipped by
    construction in that case — the freeze changes nothing when a round is
    compared against itself). Raises :class:`RoundViewsError` when either
    round banked no cloud group (:attr:`BankedRound.graded_positions` /
    :attr:`BankedRound.graded_report`), when a position present in ``target``
    has no counterpart (matched by ``position_id``) in ``baseline``, or when
    any position is not evaluable under its own report's frame.
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
    """The VERIFY-phase (on-axis, zero-degree by definition of the phase)
    capture's MEASURED curve, read off the round's own banked state and put on
    the round's ``curve_grid_hz`` — or the reason it could not be.

    ``curve`` is ``None`` exactly when ``reason`` is non-empty. Never raises: a
    round banked before the curve was persisted, or one banked without its
    ``state.json``, is a normal, expected shape, not an error.
    """

    curve: PositionCurve | None
    reason: str


def _banked_verify_curve(
    inputs: RoundInputs,
) -> tuple[tuple[np.ndarray, np.ndarray] | None, str]:
    """``((freqs_hz, measured_db), "")`` off the round's flow state, or
    ``(None, reason)``.

    The ONE reader of ``verify_priors.verify_measured`` on this side of the
    seam, because it has two consumers that want the curve differently:
    :func:`verify_pose_curve` puts it on the round's cloud-position grid so it
    can sit beside the seats, and :func:`forward_model_verify_delta` takes it
    VERBATIM — that comparison interpolates onto the prediction's own grid, so
    a detour through a third grid would only lose bins, and a round that
    banked no cloud group has no such grid to detour through (#3482). The
    parse itself is :func:`~.durable_state.verify_measured_curve_from_state`,
    the product's own reader for the key.
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

    This used to deconvolve every ``verify``-tagged dump-ring capture against
    its banked program, gate it, smooth it at the positions' own fraction and
    resample the result — the one place a *reading* module performed DSP, and
    it did so only because the curve it wanted was not banked anywhere.

    It is banked now. ``verify_priors.verify_measured`` holds the very pair the
    delta probe graded (``(freqs_hz, measured_db, predicted_db)``, #2522), so
    this reads the measured half through :func:`_banked_verify_curve` — the
    module's one reader for that key — and interpolates it onto the round's
    shared grid. That is the whole function: one hop from a banked number to a
    comparable one.

    The banked curve is block-averaged in dB to
    :data:`~.durable_state.MAX_PERSISTED_SUM_POINTS`, not smoothed at a
    fractional-octave width, so :attr:`PositionCurve.smoothing_fraction` is
    reported as ``0`` — this module's own spelling for *not attested*
    (:func:`load_banked_round` reads the same ``0`` for a round that banked no
    fraction). Nothing consumes that attestation for THIS curve:
    :func:`per_seat_curves` and :func:`agreement_table` read only
    ``magnitude_db``, and the graded views run over the cloud seats.

    **The resample is for the SEATS, and only they should pay it.** The grid
    is the round's cloud-position grid, which is what makes this curve sit
    beside them — and which a round that banked no cloud group does not have.
    :func:`forward_model_verify_delta` therefore reads the same source
    verbatim rather than coming through here.
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

    ``delta`` is ``None`` exactly when ``reason`` is non-empty, exactly as
    :class:`VerifyPoseResult` reads. Never raises: a round that banked no
    per-driver solos, or none at this pose, or no VERIFY curve, is a normal
    shape rather than an error.

    ``basis_round_dir`` / ``measured_round_dir`` name the two rounds the
    halves came from, ALWAYS — equal when one round supplied both. They are
    the join's disclosure: a delta whose prediction and whose measurement were
    banked by different sessions is a different claim from one taken inside a
    round, and a reader must not have to remember which it asked for.

    Additive evidence. It carries no verdict, tolerance or score — what a
    given ``max_abs_db`` means is the reader's judgement (invariant 3).
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

        Derived rather than passed in: a delta IS the judging, so a result
        carrying one was judged by ``measured_round_dir`` and a result
        carrying a reason was judged by nothing. The vocabulary is
        :func:`~.forward_model.acceptance_block`'s, shared with the
        prediction record, so ``predict``'s untriaged output and this one
        cannot come to spell the same fact differently.
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
    """Predict a summed response from ``basis``'s per-driver solos, and delta
    it against the VERIFY sum ``measured`` banked (ticket 4.5).

    The two halves the question needs: a PREDICTION BASIS (a banked take at
    ``position_deg`` carrying both driver solos, magnitude and phase, per
    ruling R9) and a MEASURED VERIFY SUM (``verify_priors.verify_measured``,
    read verbatim through :func:`_banked_verify_curve`). Either absent, and the
    result says which — this view never substitutes one for the other.

    **They come from two different banked rounds, because that is where the
    flow puts them** (#3482). The measure stage walks the solos and never
    reaches VERIFY; the verify stage measures the sum and walks no solos, in a
    NEW bundle under a new relay session id. So ``measured`` is a separate
    round and the join is disclosed on the result. It defaults to ``basis``,
    which is the same question asked of one round — the shape a corpus banked
    from a single session carries.

    ``candidate`` is a PARAMETER rather than the round's incumbent, and that is
    the point of a forward model: the question is usually what some candidate
    WOULD have measured, not what the applied one did. Postdicting the flat
    campaign's r8 regression is exactly that shape, and exactly this join —
    r7's banked solos, the inherited EQ held verbatim, the delay r8 applied on
    top, against r8's own measured verify.
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

    Each curve is expressed as its own deviation from its own median level
    over ``norm_band_hz`` (the campaign tool's own normalisation). This is
    what makes the VERIFY pose — captured through an entirely different DSP
    path than the banked cloud positions — comparable to them without any
    cross-calibration assumption: only SHAPE is compared, never absolute
    level.
    """
    # Asked BEFORE the norm band, so a round that banked no cloud group is
    # told what it is missing rather than that its (empty) grid has no bins in
    # the band — the two are one absence and only the first sentence names it.
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

    ``degrees`` is the BEARING each round banked for this row, where the row is
    a seat and the round records one — empty on a pooled role metric, which has
    no single bearing. It exists because a per-seat metric is keyed by
    ``position_id``, and a position id stopped naming the same bearing across
    the 2026-08-24 geometry ruling: the ruling put the design axis at the front
    of the post-apply pose set, so ``cloud_verify_02`` was −7° before it and 0°
    after, ``cloud_verify_04`` was −22° and is now +7°. A "spread" taken across
    that boundary is the difference between two different seats, and nothing in
    the comparison could see it.

    **It DISCLOSES rather than refuses**, deliberately. The
    measurement-loop doctrine's hard stops are for component damage and
    hearing safety; this is an interpretation question, and an operator who
    knowingly compares a pre-ruling round to a post-ruling one — to see what
    the ruling itself did — is asking a legitimate question a guard would
    have blocked. So the bearings ride beside the number and
    :meth:`bearings_agree` names the answer; what to do about a ``False`` is
    the reader's call.
    """

    name: str
    values: dict[str, float]
    #: ``{round label: bearing}``, only for rows that HAVE one. A label absent
    #: from this map recorded no bearing for the row (a pre-ruling round, a
    #: vertical seat, a retake that declares no side) — which is why
    #: :meth:`bearings_agree` answers ``None`` rather than ``True`` when fewer
    #: than two are known: "nothing disagreed" and "nothing was comparable" are
    #: different facts.
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
    """Session-to-session spread of the pooled honest figures, plus each
    round's own value and the pairwise deltas a caller can derive from them.

    ``rounds`` is ``(label, banked_round)`` pairs — the label is whatever
    the caller wants printed (a session id, a timestamp, an attempt name).
    Every round is graded SHIPPED (no frozen substitution) through the
    PUBLIC :func:`~jasper.active_speaker.flat_spec_views.role_split_flatness`
    — unlike :func:`frozen_reference_grade`, this view has no per-position
    reference to substitute, so it has no reason to reach for the private
    ``_evaluate_position``/``_pool`` building blocks the freeze needs.
    ``role_split_flatness`` reports BOTH poolings per role (``rms_db``, the
    shipped per-bin weighting, and ``log_rms_db``, the per-octave
    re-weighting) and this view carries both through, correctly named —
    an earlier version of this function hand-pooled with ``_pool`` and
    labelled the result ``log_pooled`` while actually computing the LINEAR
    (per-bin) pool, because ``_pool`` is the one weighted-mean identity both
    poolings share and only the caller's choice of weights (bin count vs
    octave span) tells them apart.

    ``primary_role`` exists here only because ``role_split_flatness``'s
    signature requires one — this function immediately recombines its
    ``primary``/``others`` split back into one flat role list (below) and
    reports every role the same way, so which role is named "primary"
    changes nothing about this function's own output. It is a seam
    requirement, not a repeatability policy.
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
                    # comparison spanning the geometry ruling is VISIBLE rather
                    # than silently pairing two different seats under one id.
                    # Only recorded bearings are stored — an absent label is
                    # what makes ``bearings_agree()`` answer None instead of
                    # inventing agreement out of a missing number.
                    if position_flatness.degrees is not None:
                        position_degrees.setdefault(position_flatness.position_id, {})[
                            label
                        ] = float(position_flatness.degrees)
        # The SHIPPED linear-pooled figure — spec_convergence_residual's own
        # number, lifted from the report rather than recomputed — carried
        # beside the per-octave/per-role re-poolings above so a caller can
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
# Agreement — per-seat sign/magnitude testimony for every feature
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
    #: ``True``/``False`` when ``len(seats) >= AGREEMENT_TESTIFY_MIN`` (the
    #: campaign's own literal threshold can in principle be met); ``None`` —
    #: a NAMED not-evaluable state, never a vacuous boolean — below it, where
    #: ``n_testify >= AGREEMENT_TESTIFY_MIN`` cannot be satisfied by
    #: construction. See :func:`agreement_table`.
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


def _detrend(grid: np.ndarray, curve_db: np.ndarray) -> np.ndarray:
    """Each seat expressed against its own local ~1-octave background.

    A feature is a local excursion, not the gross tilt no narrow biquad can
    or should chase — the campaign's own ``agreement.py`` made the same call,
    subtracting a per-bin ``[fc*2**-0.5, fc*2**0.5)`` window average from
    each curve before hunting for features.

    **This is the same QUESTION, not the same ARITHMETIC.** The campaign's
    detrend is a plain arithmetic mean of dB values over a half-open window;
    ``smooth_fractional_octave(..., fraction=1)`` is the product seam's
    POWER-mean (linear-energy average) over a very slightly different,
    inclusive-topped window. The two track each other closely for the small
    ripple this function is built to isolate, but they are NOT byte-
    identical — this module's numbers will not reproduce the campaign's own
    published detrend tables bin-for-bin, and a caller comparing the two
    should compare verdicts (which feature, which sign, roughly what size),
    never subtract one table's cell from the other's. Reusing the product
    seam here — rather than porting the campaign's exact arithmetic — is the
    deliberate choice: taking "1-octave window average" from the seam that
    owns it beats a second, bespoke implementation of the same idea that
    could drift from it. This is now the only call to it in the module.
    """
    return curve_db - smooth_fractional_octave(grid, curve_db, fraction=1)


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

    The round's OWN trusted floor (:attr:`FlatSpecReport.trusted_floor_hz`)
    when it recorded one — a caller sweeping below a session's own honesty
    floor would be grading bins that session itself could not vouch for.
    Falls back to the spec's nominal reference-band edge
    (:data:`~jasper.active_speaker.flat_spec.REFERENCE_BAND_HZ`) for a round
    that recorded no floor, rather than a campaign-specific literal (the
    previous default, ``357.14`` Hz, was one session's ``f_trusted_floor_hz``
    at its particular 7 ms gate window — a coincidence of that campaign, not
    a general default).
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
    """Every feature in ``[lo_hz, hi_hz]``, with per-seat testify/dissent
    counts and a magnitude-agreement ratio.

    ``testify`` = same sign as the pooled curve AND ``|seat| >= testify_db``.
    ``dissent`` = opposite sign AND ``|seat| >= testify_db``. ``common_mode``
    requires BOTH sign agreement (``n_testify >= AGREEMENT_TESTIFY_MIN`` and
    ``n_dissent <= AGREEMENT_DISSENT_MAX``) AND magnitude agreement
    (``ratio <= magnitude_ratio_ok``).

    **The sign-agreement counts are the campaign's own LITERAL thresholds**
    (``test >= 3 and diss <= 1``), not a seat-count-relative generalisation.
    An earlier version of this function scaled the testify requirement to
    ``len(seats) - 1``, which is not the same rule: at the real 5-seat
    default it demands testify >= 4 where the campaign's own measurement-
    validated frame demands only >= 3, flipping the verdict on any feature
    exactly 3 seats testify to (sign-agreement fails under the generalised
    rule, holds under the literal one) — and at 1-2 seats it returns a
    vacuous ``True`` (``max(n_seats - 1, 0)`` floors at 0, so an
    unconstrained "at least 0 testify" trivially passes). Below
    ``AGREEMENT_TESTIFY_MIN`` seats the literal threshold can never be
    satisfied by construction, so ``common_mode`` is ``None`` there — a
    named NOT-EVALUABLE state, distinct from both a real pass and a real
    fail, matching the same "absence is not a zero" rule
    :class:`~jasper.active_speaker.flat_spec.BandResult` follows for an
    unevaluable band. ``n_testify``, ``n_dissent``, ``spread_db`` and
    ``ratio`` are still reported at any seat count — they are measurements,
    not verdicts, and remain informative even where the verdict is not.
    """
    grid = np.asarray(grid, dtype=float)
    if not seats:
        raise RoundViewsError("agreement_table: no seats supplied")
    detrended = np.vstack([_detrend(grid, seat.normalized_db) for seat in seats])
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
# View 4 — audibility-weighted co-metrics: NBD + SM (Olive 2004 /
# US 8,311,232 B2), ADR-0202, ticket 6.13
# --------------------------------------------------------------------------- #

#: The lateral-walk curve role this view pools onto the on-axis curve: the
#: composed acoustic response, not one driver's isolated branch. A local
#: literal on :data:`DEFAULT_PRIMARY_ROLE`'s own precedent — importing
#: ``crossover_v2.spatial`` (large, orchestration-heavy) for one string is
#: not worth it, and this module defines no role constant of its own beyond
#: what it needs to select.
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
    """:func:`pooled_window_horizontal`'s output curve, plus its own
    provenance.

    **Not** CTA-2034's "listening window": that average includes vertical
    poses this rig does not capture — the name is deliberate (ADR-0202 /
    ticket 6.13). This is the power average of whatever horizontal
    bearings — 0/±7/±22° or fewer — the round's lateral walk banked a
    :data:`_SUMMED_CURVE_ROLE` curve for.

    Args:
      freqs_hz: the grid the curve was resampled onto (the caller's own).
      magnitude_db: the power-averaged curve.
      bearings_deg: which banked bearings actually contributed, sorted —
        the round's own coverage, disclosed rather than assumed complete.
      n_curves: how many banked lateral-walk stops contributed in total —
        never a superseded retake (:func:`pooled_window_horizontal` has
        the pooling and supersession rules).
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
    is the same banked-curve reader (:func:`~.record_index.bundle_measurements`
    + :func:`~.position_cycle.read_take_curves` +
    :func:`~.position_cycle.parse_curve_magnitude`) every other lateral-pose
    consumer in this tree already uses — never a second reader of the take
    files. This function only adds the ``role == "summed"`` filter and the
    power-average.

    **Power-averaged, never dB-averaged** — the house convention
    :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave` uses
    for the same reason (a dB mean over-emphasises deep nulls), restated
    here rather than delegated to it because this reduction is across
    CURVES at one frequency, not across frequencies within one curve, so it
    is not literally a call to that function. Per curve: resampled onto
    ``grid_hz`` and masked to the curve's OWN driven ``band_hz`` (a point
    outside it was never measured, so it is excluded rather than read as
    silence). Distinct stops sharing a bearing (a drift re-visit) are
    power-averaged together FIRST, so a bearing visited three times cannot
    outweigh one visited once; the per-bearing curves are then
    power-averaged across bearings. A RETAKE is not such a repeat:
    :func:`~.feature_classifier.load_round_pose_curves` has already
    superseded the older attempts, so rejected noise never reaches this
    pool.

    Returns ``None`` when the round's lateral walk banked no
    :data:`_SUMMED_CURVE_ROLE` curve at ANY bearing — not every round has
    banked the composed post-apply response yet
    (``docs/active-speaker-tuning-layers-design.md``'s "S at every angle"
    is a landing capability, not a shipped one). The absence is disclosed
    by the caller (:func:`audibility_co_metrics`'s ``pooled_window_reason``),
    never fabricated.
    """

    grid = np.asarray(grid_hz, dtype=float)
    by_bearing: dict[float, list[np.ndarray]] = {}
    for curve in load_round_pose_curves(Path(bundle_dir)):
        # A raised pose is SKIPPED rather than given its own bucket: this
        # pool is the lateral walk's horizontal window, and an elevated seat
        # sharing a bearing with a mark-height one is a different measurement,
        # not a repeat visit to the same stop.
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

    # A grid point outside every contributing curve's own band is legitimate
    # (not a bug): np.nanmean of an all-NaN slice is the correct "no bearing
    # covered this frequency" answer, and both it and the log of its NaN
    # propagate that honestly. Only the WARNINGS are silenced here — numpy's
    # "Mean of empty slice" is raised through the stdlib `warnings` module
    # (not the FPE machinery `errstate` governs), and "invalid value
    # encountered in log10" needs `errstate` for the same NaN.
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
    ``*_reason`` is non-empty — the same "available iff no reason" contract
    :class:`VerifyPoseResult` follows. A round missing one lens is not an
    unreadable round: co-metrics inform, they never gate (ADR-0202 rule 2),
    and an absent one is disclosed rather than fabricated.

    Args:
      round_dir: the round this describes.
      on_axis: NBD/SM on ``banked.positions``' own
        :data:`DEFAULT_PRIMARY_ROLE` curve(s), power-averaged together when
        the round banked more than one.
      pooled_window: NBD/SM on :func:`pooled_window_horizontal`.
      pooled_window_bearings_deg: which bearings the pooled window actually
        drew on — ``()`` when ``pooled_window`` is ``None``.
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
    """NBD + SM on the on-axis curve and the pooled horizontal window, for
    one graded round (ADR-0202, ticket 6.13).

    A co-metric surface, additive beside the round's grade: nothing here
    reads or writes ``banked.report`` — it is read once, for its own
    ``graded_band_hz`` default, and never touched again.

    ``band_hz`` defaults to ``banked.report.graded_band_hz`` — the SAME
    span ``flat_spec.SPEC_BANDS`` itself graded, so a co-metric and the
    grade it sits beside describe the same stretch of spectrum unless a
    caller deliberately asks for a different one.
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
        # module (never dB-mean) — a no-op when there is exactly one, which
        # is the ordinary case.
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
