# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Mapping,
    Protocol,
    Sequence,
)

from jasper.active_speaker import baseline_profile
from jasper.active_speaker.branch_chain import CrossoverSection
from jasper.active_speaker.crossover_v2 import admission as _admission
from jasper.active_speaker.crossover_v2 import capture_dispatch as _dispatch
from jasper.active_speaker.crossover_v2 import capture_plan as _plan
from jasper.active_speaker.crossover_v2 import planning as _planning
from jasper.active_speaker.crossover_v2 import priors as _priors
from jasper.active_speaker.crossover_v2 import programs as _programs
from jasper.active_speaker.crossover_v2.admission import (
    ATTEMPT_INITIATOR_SPEAKER,
    MAX_EXTRA_ATTEMPTS_PER_POSITION,
    SlotAttempts,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    CLOUD_GEOMETRY_RETRY_PROMPTS,
    CLOUD_GEOMETRY_RETRY_RISE_CM,
    CLOUD_POSITION_PROMPTS,
    GEOMETRY_RETRY_OFFSET_CM,
    LATERAL_POSE_PROMPTS,
    CloudPositionPrompt,
    _pose,
    position_angle_deg,
    position_elevation_deg,
    verify_pose_table,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginRefused,
)
from jasper.active_speaker.crossover_v2.contracts import (
    CrossoverV2FlowError,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    MAX_ATTEMPT_HISTORY,
    AttemptRecord,
    V2ConductorSnapshot,
)
from jasper.active_speaker.crossover_v2.journey import (
    GROUP_PHASES,
    LATERAL_CONSUMER_FC_SELECTOR,
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_VERIFY,
    CommissionJourney,
    JourneyPlan,
    validated_lateral_consumer,
)
from jasper.active_speaker.crossover_v2.measure_spec import (
    GRAPH_SCOPE_DRIVERS,
    MeasureSpec,
    branch_channels_for,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    NON_RETRIABLE_CODES,
    REASON_CLOUD_GEOMETRY_LOCKED,
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    PhaseVerdict,
    TakeVerdict,
    reason_diagnosis,
    reason_message,
)
from jasper.active_speaker.crossover_v2.spatial import (
    POSITION_ROLE_OFFAX,
    LateralPose,
)
from jasper.active_speaker.crossover_v2.summed_alignment import _unreadable
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.program import (
    ExcitationProgram,
    RoleBand,
)
from jasper.audio_measurement.program_analysis import (
    AppliedAlignment,
    GainPlan,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
)
from jasper.log_event import log_event

from .crossover_v2.alignment_prescription import (
    alignment_delay_search_bounds_us,
)
from .measurement_programs import gate_exemption, resolved_measurement_purpose

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.active_speaker.crossover_v2.round_evidence import (
        EntryBaseline,
    )

logger = logging.getLogger(__name__)

# dB of pooled spec residual; the model's measured tracking error (ADR-0227).
PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5


MEASUREMENT_DISTANCE_M = 1.0


class AnalyzeCapture(Protocol):
    """analyze(program, capture_result, priors, geometry, *, phase) → ProgramAnalysis.

    ``phase`` is the SESSION's flow phase, never ``program.phase``, which is always
    "verify" for every cloud position (#1855). Required and keyword-only.
    """

    def __call__(
        self,
        program: ExcitationProgram,
        result: Any,
        priors: MeasurementPriors,
        geometry: MeasurementGeometry,
        *,
        phase: str,
    ) -> ProgramAnalysis: ...


PublishCheck = Callable[[GainPlan, Mapping[str, Any]], None]


@dataclass(frozen=True)
class V2RecordPublishers:
    """The durable-write seam, funnelled through
    :class:`~.crossover_v2.record_store.BankedRecordStore` (ADR-0227 §12)."""

    check: PublishCheck


@dataclass(frozen=True)
class V2FlowSeams:
    """The session's injected I/O boundary (all side effects)."""

    analyze: AnalyzeCapture
    records: V2RecordPublishers
    summed_alignment_reference: Callable[[Any, Any], Any] | None = None


class CrossoverV2Session:
    def __init__(
        self,
        *,
        session_id: str,
        source_preset: Any,
        roles_bands: Sequence[RoleBand],
        fc_hz: float | None,
        driver_caps_dbfs: Mapping[str, float],
        session_volume_db: float,
        seams: V2FlowSeams,
        driver_sweep_duration_limits_s: Mapping[str, float] | None = None,
        driver_spacing_m: float | None = 0.0,
        accepted_phases: Sequence[str] = (),
        applied: bool = False,
        gain_plan_db: Mapping[str, float] | None = None,
        measure_gain_ceiling_db: Mapping[str, float] | None = None,
        index_phase_map: Mapping[int, str] | None = None,
        post_apply_verifies: bool | None = None,
        measure_predicted_sum: Any = None,
        measure_entry_baseline: "EntryBaseline | None" = None,
        measurement_protection_sections_by_role: Mapping[
            str, Sequence[CrossoverSection]
        ]
        | None = None,
        attempt_history: Sequence[AttemptRecord] = (),
        sound_design_revision: int | None = None,
        lateral_consumer: str = LATERAL_CONSUMER_FC_SELECTOR,
        lateral_prompts: Sequence[CloudPositionPrompt] | None = None,
        measure_specs_by_index: Mapping[int, MeasureSpec] | None = None,
        verify_prompts: Sequence[CloudPositionPrompt] | None = None,
    ) -> None:
        roles = tuple(roles_bands)
        if not 1 <= len(roles) <= 2:
            raise CrossoverV2FlowError("a v2 session walks one or two drivers")
        self.session_id = str(session_id)
        self.sound_design_revision = sound_design_revision
        self._preset = source_preset
        self._roles = roles
        # Lowest role first. ``_tweeter`` is ``None`` on a 1-way main, never aliased.
        self._tweeter: RoleBand | None = roles[1] if len(roles) == 2 else None
        self._tweeter_role = None if self._tweeter is None else self._tweeter.role
        self._fc_hz = None if fc_hz is None else float(fc_hz)
        self._caps = dict(driver_caps_dbfs)
        # Per-role longest admissible ONE sweep; an absent role composes at its
        # nominal duration.
        self._sweep_duration_limits_s = dict(driver_sweep_duration_limits_s or {})
        self._session_volume_db = float(session_volume_db)
        self._seams = seams
        self.capture_published_refusal = False
        self._measurement_protection_sections_by_role = None
        if measurement_protection_sections_by_role is not None:
            self._measurement_protection_sections_by_role = {
                str(role): tuple(sections)
                for role, sections in measurement_protection_sections_by_role.items()
            }
        # Attempts belong to the commissioning journey, not to this capture session.
        self._attempt_history = list(attempt_history)[-MAX_ATTEMPT_HISTORY:]
        # ``None`` is undeclared spacing, never a default: disclose it rather than
        # silently folding it into the same 0.0 ``MeasurementGeometry.parallax_us``
        # already treats as "no correction".
        if driver_spacing_m is None:
            log_event(
                logger,
                "crossover_v2.driver_spacing_unknown",
                level=logging.INFO,
                session_id=self.session_id,
            )
        self._geometry = MeasurementGeometry(
            driver_spacing_m=0.0
            if driver_spacing_m is None
            else float(driver_spacing_m),
            mic_distance_m=MEASUREMENT_DISTANCE_M,
        )
        # Where this round is, and the walk it is in. ONE aggregate: six correlated
        # fields here could disagree.
        self._journey = CommissionJourney(
            JourneyPlan.from_index_map(
                index_phase_map
                if index_phase_map is not None
                else _plan.DEFAULT_INDEX_PHASE_MAP,
                post_apply_verifies=post_apply_verifies,
            ),
            accepted_phases=accepted_phases,
            applied=applied,
        )
        self._gain_plan_db = dict(gain_plan_db) if gain_plan_db else None
        self._measure_gain_ceiling_db = dict(measure_gain_ceiling_db or {})
        # CHECK's measured room floor, held for the MEASURE and lateral priors.
        # In-memory only: CHECK/MEASURE evidence does not carry across sessions.
        self._check_ambient_report: dict[str, Any] | None = None
        self._lateral_poses: list[LateralPose] = []
        try:
            validated_lateral_consumer(
                lateral_consumer,
                states_own_poses=lateral_prompts is not None,
            )
        except ValueError as exc:
            raise CrossoverV2FlowError(str(exc)) from exc
        self._lateral_prompts: tuple[CloudPositionPrompt, ...] = (
            tuple(lateral_prompts)
            if lateral_prompts is not None
            else LATERAL_POSE_PROMPTS
        )
        self._measure_specs_by_index = (
            measure_specs_by_index if measure_specs_by_index is not None else {}
        )
        # Resolved through the resolver the plan builder uses, so the session and the
        # plan cannot read different pose tables.
        self._verify_prompts: tuple[CloudPositionPrompt, ...] = verify_pose_table(
            verify_prompts
        )
        # Geometry-locked retakes already spent, per group.
        self._geometry_retries_used: dict[str, int] = {
            phase: 0 for phase in self._journey.plan.group_indexes
        }
        # Frozen together so a subset cannot drift.
        self._excitation = _programs.SessionExcitation(
            roles=self._roles,
            caps_dbfs=self._caps,
            session_volume_db=self._session_volume_db,
            fc_hz=self._fc_hz,
            sweep_duration_limits_s=self._sweep_duration_limits_s,
            summed_sweep_band_hz=_plan.room_sweep_band_hz(
                self._roles, self._lateral_prompts
            ),
        )
        # Composed ONCE and held: ``program_for_phase`` answers by object identity;
        # before→after comparability depends on it.
        self._check_program = self._excitation.check_program()
        self._measure_program: ExcitationProgram | None = (
            self._excitation.measure_program(self._gain_plan_db)
            if self._gain_plan_db is not None
            else None
        )
        self._verify_program = self._excitation.verify_program()
        # The position groups' twin: same sweep, same clamp, no courtesy prelude.
        self._cloud_program = self._excitation.cloud_program()
        branch_spec = next(
            (
                spec
                for spec in self._measure_specs_by_index.values()
                if spec.graph_scope == "candidate_branches"
            ),
            None,
        )
        self._branch_program = (
            build_branch_program(self._cloud_program, branch_channels_for(branch_spec))
            if branch_spec is not None
            else None
        )
        # Per-SLOT attempt bookkeeping: the phase for a single-capture phase,
        # ``phase:index`` inside a group. ONE meter per slot.
        self._slot_attempts: dict[str, SlotAttempts] = {}
        self._last_reason: dict[str, str] = {}
        # The capture evidence paired with each slot's last rejection; exhaustion reads
        # this rather than the global pair, which can belong to a different position.
        self._last_pilot_evidence: dict[str, tuple[str, bool | None, bool | None]] = {}
        # Positions the flow GAVE UP on, so the group closes with what it has instead
        # of the session dying at the mic.
        self._group_unresolved: dict[str, dict[int, str]] = {
            phase: {} for phase in self._journey.plan.group_indexes
        }
        self._armed_capture: tuple[int, int] | None = None
        self._measure_predicted_sum: Any = measure_predicted_sum
        self._measure_entry_baseline: "EntryBaseline | None" = measure_entry_baseline
        self._last_failure_code: str | None = None
        # The pilot evidence belonging to ``_last_failure_code``, ALWAYS written with
        # it. ``None`` is "no pilot evidence for this failure".
        self._last_failure_pilot_heard: bool | None = None

    @property
    def source_preset(self) -> Any:
        return self._preset

    @property
    def roles_bands(self) -> tuple[RoleBand, ...]:
        return self._roles

    @property
    def excitation(self) -> _programs.SessionExcitation:
        return self._excitation

    @property
    def caps_dbfs(self) -> Mapping[str, float]:
        return self._excitation.caps_dbfs

    @property
    def spl_stop_db_spl(self) -> float:
        return self._preset.safety.max_commissioning_level_db_spl

    @property
    def gain_plan_db(self) -> Mapping[str, float] | None:
        return self._gain_plan_db

    @property
    def measure_gain_ceiling_db(self) -> Mapping[str, float]:
        return self._measure_gain_ceiling_db

    @property
    def analyze(self) -> AnalyzeCapture:
        return self._seams.analyze

    def summed_alignment_reference(self) -> Any:
        baseline = self.measure_entry_baseline
        key = baseline.artifact_ref if baseline is not None else None
        cached = getattr(self, "_summed_alignment_reference_cache", None)
        if cached is None or cached[0] != key:
            seam = self._seams.summed_alignment_reference
            reference = (
                _unreadable("no_entry_baseline")
                if baseline is None
                else seam(baseline, self.source_preset)
                if seam
                else None
            )
            cached = self._summed_alignment_reference_cache = (key, reference)
        return cached[1]

    def set_program(self, phase: str, program: ExcitationProgram) -> None:
        if phase == PHASE_CHECK:
            self._check_program = program
        elif phase == PHASE_VERIFY:
            self._verify_program = program
        elif phase == PHASE_CLOUD_VERIFY:
            self._cloud_program = program

    def set_excitation(self, excitation: _programs.SessionExcitation) -> None:
        self._excitation = excitation

    def set_entry_baseline(self, baseline: EntryBaseline | None) -> None:
        self._measure_entry_baseline = baseline

    def _compose_measure_program(
        self,
        gain_plan_db: Mapping[str, float],
        *,
        extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        """MEASURE's program at the solved gains, the one with a LIFECYCLE."""
        return self._excitation.measure_program(
            gain_plan_db,
            extra_backoff_db=extra_backoff_db,
        )

    def check_priors(self) -> MeasurementPriors:
        return _priors.check_priors(fc_hz=self._fc_hz)

    def measure_priors(self) -> MeasurementPriors:
        return _priors.measure_priors(
            fc_hz=self._fc_hz,
            source_preset=self._preset,
            protection_sections_by_role=self._measurement_protection_sections_by_role,
            ambient_report=self._check_ambient_report,
            summed_alignment=self.summed_alignment_reference(),
            alignment_delay_bounds_us=alignment_delay_search_bounds_us(self._preset),
            applied_alignment=self._applied_alignment(),
            explicit_alignment_delay_us=None,
            explicit_alignment_polarity_sign=None,
        )

    def _applied_alignment(self) -> AppliedAlignment | None:
        if self._tweeter_role is None:
            return None
        return _planning.applied_profile_timing(
            baseline_profile.load_applied_baseline_profile_state()
        )

    def lateral_priors(self) -> MeasurementPriors:
        return _priors.lateral_priors(
            fc_hz=self._fc_hz,
            ambient_report=self._check_ambient_report,
        )

    @property
    def post_apply_verifies(self) -> bool:
        """Will this session's correction be MEASURED after it is applied?"""
        return self._journey.plan.post_apply_verifies

    @property
    def accepted_phases(self) -> frozenset[str]:
        return self._journey.accepted_phases

    @property
    def attempt_history(self) -> tuple[AttemptRecord, ...]:
        """Accepted applied-candidate attempts, oldest first and bounded."""
        return tuple(self._attempt_history)

    @property
    def session_phases(self) -> tuple[str, ...]:
        """The ordered phases this session runs (its ``index_phase_map``'s)."""
        return self._journey.plan.phases

    @property
    def applied(self) -> bool:
        return self._journey.applied

    @property
    def measure_predicted_sum(self) -> Any:
        return self._measure_predicted_sum

    @property
    def measure_entry_baseline(self) -> "EntryBaseline | None":
        """#2291's pre-apply side of this round, or ``None``."""
        return self._measure_entry_baseline

    @property
    def last_failure_code(self) -> str | None:
        """The most recent rejection's reason code (host persistence reads it)."""
        return self._last_failure_code

    @property
    def last_failure_pilot_heard(self) -> bool | None:
        """Pilot evidence for :attr:`last_failure_code` — the host persists it.

        This getter checks nothing: the pairing that CAN diverge is with the code a
        caller chooses to persist, and ``persist_conductor_state`` makes that check.
        """
        return self._last_failure_pilot_heard if self._last_failure_code else None

    def _pilot_heard_for(
        self,
        code: str | None,
        *,
        slot: str | None = None,
    ) -> bool | None:
        """The pilot evidence recorded WITH ``code``, else ``None`` (#2085)."""
        if slot is not None:
            paired = self._last_pilot_evidence.get(slot)
        elif self._last_failure_code is None:
            paired = None
        else:
            paired = (
                self._last_failure_code,
                self._last_failure_pilot_heard,
                None,
            )
        return _admission.pilot_heard_for(code, paired)

    def _reflection_measured_for(
        self,
        code: str | None,
        *,
        slot: str,
    ) -> bool | None:
        """The gate discriminator recorded with ``code`` at ``slot``."""
        return _admission.reflection_measured_for(
            code, self._last_pilot_evidence.get(slot)
        )

    @property
    def armed_capture(self) -> tuple[int, int] | None:
        """The last authorized ``(index, attempt)``: the host addresses the terminal
        ``capture_result`` host event at a play-seam failure to it.
        """
        return self._armed_capture

    def phase_of_index(self, index: int) -> str:
        phase = self._journey.plan.phase_for_index(index)
        if phase is None:
            raise CrossoverV2FlowError(f"no v2 phase for capture index {index}")
        return phase

    def _slot_of_index(self, index: int) -> str:
        """The retry-budget key for one capture index."""
        phase = self.phase_of_index(index)
        return f"{phase}:{index}" if phase in GROUP_PHASES else phase

    def _cloud_prompt(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt for one group index — the SAME table the plan emitted."""
        offsets = self._journey.plan.group_offsets(phase)
        try:
            position = offsets.index(index)
        except ValueError:
            position = 0
        table = (
            self._lateral_prompts
            if phase == PHASE_LATERAL
            else self._verify_prompts
            if phase == PHASE_CLOUD_VERIFY
            else CLOUD_POSITION_PROMPTS
        )
        if position < len(table):
            return table[position]
        return _pose(_plan._LATERAL_POSE, 45.0, POSITION_ROLE_OFFAX, side="RIGHT")

    def _prompt_shown_for(self, phase: str, index: int) -> CloudPositionPrompt:
        """The prompt the operator ACTUALLY followed for the take in hand.

        Not always the table entry: after a geometry-locked rejection the phone showed a
        wider retry rung, and the sidecar's prompt is the durable statement of where.
        """
        slot = self._slot_of_index(index)
        if self._last_reason.get(slot) == REASON_CLOUD_GEOMETRY_LOCKED:
            used = max(self._geometry_retries_used.get(phase, 1), 1)
            index_ = min(used - 1, len(CLOUD_GEOMETRY_RETRY_PROMPTS) - 1)
            rung = CLOUD_GEOMETRY_RETRY_PROMPTS[index_]
            rise_cm = CLOUD_GEOMETRY_RETRY_RISE_CM[index_]
            return CloudPositionPrompt(
                rung,
                offset_cm=GEOMETRY_RETRY_OFFSET_CM,
                role=POSITION_ROLE_OFFAX,
                vertical_sign=1 if rise_cm else 0,
                vertical_offset_cm=rise_cm,
            )
        return self._cloud_prompt(phase, index)

    def note_restore_observed(self) -> None:
        """The restore-observed host event — disarms the VERIFY hold (#2616)."""
        self._journey.mark_restored()
        log_event(
            logger,
            "correction.crossover_v2_restore_observed",
            session_id=self.session_id,
        )

    def snapshot(self) -> V2ConductorSnapshot:
        return V2ConductorSnapshot(
            session_id=self.session_id,
            accepted_phases=self._journey.accepted_capture_phases(),
            session_phases=self._journey.plan.phases,
            applied=self._journey.applied,
            gain_plan_db=dict(self._gain_plan_db) if self._gain_plan_db else None,
            measure_gain_ceiling_db=dict(self._measure_gain_ceiling_db),
            measure_sweep_durations_s=_priors.measure_sweep_durations_s(
                self._measure_program
            ),
            candidate_fingerprint=None,
            attempt_history=tuple(self._attempt_history),
        )

    @classmethod
    def hydrate(
        cls,
        snapshot: V2ConductorSnapshot | None,
        *,
        session_id: str,
        **kwargs: Any,
    ) -> "CrossoverV2Session":
        """Rebuild a session, applying the §5.6 session-binding rule.

        Same session ⇒ resume with its accepted phases and gain plan; a different
        or absent one ⇒ fresh start at CHECK, mic position being unverifiable
        across sessions.
        """
        journey: dict[str, Any] = {}
        if snapshot is not None:
            journey = {
                "attempt_history": snapshot.attempt_history,
            }
        journey.update(
            {key: kwargs.pop(key) for key in tuple(journey) if key in kwargs}
        )
        if snapshot is not None and snapshot.session_id == session_id:
            return cls(
                session_id=session_id,
                accepted_phases=snapshot.accepted_phases,
                applied=snapshot.applied,
                gain_plan_db=snapshot.gain_plan_db,
                measure_gain_ceiling_db=snapshot.measure_gain_ceiling_db,
                **journey,
                **kwargs,
            )
        if snapshot is not None:
            log_event(
                logger,
                "correction.crossover_v2_session_rebound",
                level=logging.INFO,
                prior_session=snapshot.session_id,
                session_id=session_id,
            )
        return cls(session_id=session_id, **journey, **kwargs)

    def authorize_begin(
        self,
        index: int,
        attempt: int,
        entry: Any = None,
        *,
        executor_ledger: SlotAttempts | None = None,
    ) -> None:
        """Admit (or defer / refuse) one phone ``begin_capture`` (§5.7)."""
        phase = self.phase_of_index(index)
        slot = self._slot_of_index(index)
        ledger = (
            executor_ledger
            if executor_ledger is not None
            else self._slot_attempts.get(slot)
        )

        decision = _admission.assess_begin(
            ledger=None if executor_ledger is not None and attempt == 1 else ledger,
            last_reason=self._last_reason.get(slot),
            non_retriable=NON_RETRIABLE_CODES,
            default_code=REASON_LOCATE_FAILED,
            retry_charge=executor_ledger.charge
            if executor_ledger is not None
            else "operator",
        )
        if decision.kind == _admission.REFUSE_NON_RETRIABLE:
            spec = REASON_REGISTRY[decision.code]
            self.capture_published_refusal = True
            raise CaptureBeginRefused(
                spec.code,
                reason_message(
                    spec.code,
                    spec,
                    pilot_heard=self._pilot_heard_for(decision.code, slot=slot),
                ),
            )
        if decision.kind == _admission.REFUSE_EXTRAS_SPENT:
            assert ledger is not None
            code = decision.code
            spec = REASON_REGISTRY[code]
            diagnosis = reason_diagnosis(
                code,
                spec,
                pilot_heard=self._pilot_heard_for(code, slot=slot),
                reflection_measured=self._reflection_measured_for(
                    code,
                    slot=slot,
                ),
            )
            self.capture_published_refusal = True
            raise CaptureBeginRefused(
                code,
                self._extras_spent_message(
                    ledger,
                    diagnosis=diagnosis,
                    outcome=self._spent_slot_outcome(phase, index),
                ),
            )
        if decision.kind != _admission.ADMIT:
            log_event(
                logger,
                "correction.crossover_v2_begin_decision_kind_unmapped",
                level=logging.ERROR,
                session_id=self.session_id,
                phase=phase,
                index=index,
                kind=str(decision.kind),
            )
            self.capture_published_refusal = True
            raise CaptureBeginRefused(
                REASON_LOCATE_FAILED,
                reason_message(
                    REASON_LOCATE_FAILED,
                    REASON_REGISTRY[REASON_LOCATE_FAILED],
                ),
            )
        ledger = (
            executor_ledger
            if executor_ledger is not None
            else self._slot_attempts.setdefault(slot, SlotAttempts())
        )
        if decision.spends_extra and executor_ledger is None:
            try:
                ledger.spend(
                    "speaker"
                    if decision.initiator == ATTEMPT_INITIATOR_SPEAKER
                    else "operator"
                )
            except _admission.AttemptOverspendError as exc:
                raise CrossoverV2FlowError(str(exc)) from exc
        if executor_ledger is not None and attempt > 1:
            ledger.spend(executor_ledger.charge)
        ledger.admitted += 1
        self._armed_capture = (index, attempt)
        log_event(
            logger,
            "correction.crossover_v2_authorized",
            session_id=self.session_id,
            phase=phase,
            index=index,
            attempt=attempt,
            extra_used=ledger.extras_used,
            extra_allowed=MAX_EXTRA_ATTEMPTS_PER_POSITION,
            extra_by_speaker=ledger.by_speaker,
        )

    @staticmethod
    def _extras_spent_message(
        ledger: SlotAttempts,
        *,
        diagnosis: str,
        outcome: str,
    ) -> str:
        """The household sentence for a position whose extras are gone."""
        return _admission.extras_spent_message(
            ledger,
            diagnosis=diagnosis,
            outcome=outcome,
        )

    def _spent_slot_outcome(self, phase: str, index: int) -> str:
        """The state after an exhausted slot, derived from session state."""
        is_group = self._journey.plan.is_group(phase)
        return _admission.spent_slot_outcome(
            is_group=is_group,
            index=index,
            unresolved=self._group_unresolved[phase] if is_group else (),
            retained=self._retained_group_indexes(phase) if is_group else (),
        )

    def program_for_phase(self, phase: str) -> ExcitationProgram:
        """The composed program this session plays for ``phase``."""
        if phase == PHASE_LATERAL and self._branch_program is not None:
            return self._branch_program
        if phase == PHASE_LATERAL and any(
            index in self._journey.plan.group_offsets(phase)
            and spec.graph_scope != GRAPH_SCOPE_DRIVERS
            for index, spec in self._measure_specs_by_index.items()
        ):
            return self._cloud_program
        try:
            return _programs.program_for_phase(
                phase,
                check=self._check_program,
                measure=self._measure_program,
                verify=self._verify_program,
                cloud=self._cloud_program,
            )
        except _programs.NoProgramForPhaseError as exc:
            raise CrossoverV2FlowError(str(exc)) from exc

    def _capture_purpose(self, phase: str, index: int) -> str | None:
        prompt = (
            self._prompt_shown_for(phase, index)
            if phase in GROUP_PHASES
            else self._lateral_prompts[0]
            if phase == PHASE_ENTRY_BASELINE and self._lateral_prompts
            else None
        )
        return (
            resolved_measurement_purpose(prompt.purpose, prompt.kind)
            if prompt
            else None
        )

    def capture_geometry(self, phase: str, index: int) -> MeasurementGeometry:
        """Apply the plan's analysis purpose to this capture."""
        spec = self._measure_specs_by_index.get(index)
        position, vertical, exemption = (
            (spec.positions or (0,))[0] if spec else 0,
            spec.vertical_deg if spec else 0,
            None,
        )
        if phase in GROUP_PHASES:
            prompt = self._prompt_shown_for(phase, index)
            position, vertical = (
                position_angle_deg(prompt),
                position_elevation_deg(prompt),
            )
            exemption = gate_exemption(self._capture_purpose(phase, index))
        return replace(
            self._geometry,
            gate_exempt_reason=exemption,
            position_deg=position,
            vertical_deg=vertical,
        )

    def note_accepted(self, phase: str, index: int) -> None:
        self._journey.accept(phase, index)

    def check_verdict(self, analysis: ProgramAnalysis) -> PhaseVerdict:
        gain_plan = analysis.gain_plan
        verdict = PhaseVerdict.from_take(
            _dispatch.assess(analysis, phase=PHASE_CHECK, program=self._check_program)
        )
        if not verdict.accepted:
            return verdict
        assert gain_plan is not None
        self._gain_plan_db = dict(gain_plan.gain_db)
        self._measure_gain_ceiling_db.clear()
        self._measure_gain_ceiling_db.update(
            {
                role: solve.flat_target_gain_db
                for role, solve in gain_plan.role_solves.items()
            }
        )
        # HOLD the ambient report, don't just publish it: without it MEASURE's
        # per-driver SNR verdict has no noise floor to grade against.
        self._check_ambient_report = (
            dict(analysis.ambient_report) if analysis.ambient_report else None
        )
        self._measure_program = self._compose_measure_program(self._gain_plan_db)
        self._seams.records.check(gain_plan, analysis.ambient_report or {})
        return replace(verdict, payload={"measurement_phase": PHASE_CHECK})

    def _retained_group_indexes(self, phase: str) -> set[int]:
        """Which indexes of one group already hold evidence."""
        if phase == PHASE_LATERAL:
            return {pose.index for pose in self._lateral_poses}
        return set()

    def rearm_measure_after_transient(
        self, verdict: PhaseVerdict | TakeVerdict
    ) -> None:
        if self._gain_plan_db is not None:
            self._gain_plan_db.update(
                {
                    key.removeprefix("next_gain_db."): float(value)
                    for key, value in verdict.evidence.items()
                    if key.startswith("next_gain_db.")
                }
            )
            self._measure_program = self._compose_measure_program(self._gain_plan_db)
            if verdict.next == "retake_quieter":
                self._measure_gain_ceiling_db.update(
                    {
                        role: min(ceiling, self._gain_plan_db[role])
                        for role, ceiling in self._measure_gain_ceiling_db.items()
                    }
                )
