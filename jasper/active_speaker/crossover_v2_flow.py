# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import (
    Any,
    Callable,
    Mapping,
    Protocol,
    Sequence,
)

from jasper.active_speaker import baseline_profile
from jasper.active_speaker.crossover_section import CrossoverSection
from jasper.active_speaker.crossover_v2 import admission as _admission
from jasper.active_speaker.crossover_v2 import capture_dispatch as _dispatch
from jasper.active_speaker.crossover_v2 import planning as _planning
from jasper.active_speaker.crossover_v2 import priors as _priors
from jasper.active_speaker.crossover_v2 import programs as _programs
from jasper.active_speaker.crossover_v2.admission import (
    SlotAttempts,
)
from jasper.active_speaker.crossover_v2.capture_plan import (
    CloudPositionPrompt,
    position_angle_deg,
    position_elevation_deg,
)
from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureBeginRefused,
)
from jasper.active_speaker.crossover_v2.contracts import (
    CrossoverV2FlowError,
)
from jasper.active_speaker.crossover_v2.durable_state import (
    V2ConductorSnapshot,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_LATERAL,
)
from jasper.active_speaker.crossover_v2.measure_spec import (
    GRAPH_SCOPE_DRIVERS,
    MeasureSpec,
    branch_channels_for,
)
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    REASON_RETRIES_SPENT,
    PhaseVerdict,
    TakeVerdict,
    reason_message,
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
from jasper.platform.log_event import log_event

from .crossover_v2.alignment_prescription import (
    alignment_delay_search_bounds_us,
)

logger = logging.getLogger(__name__)

MEASUREMENT_DISTANCE_M = 1.0


class AnalyzeCapture(Protocol):
    """analyze(program, capture_result, priors, geometry, *, phase) → ProgramAnalysis.

    ``phase`` is the SESSION's flow phase, never ``program.phase``, which is
    "verify" for a timing take (#1855). Required and keyword-only.
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
        index_phase_map: Mapping[int, str],
        driver_sweep_duration_limits_s: Mapping[str, float] | None = None,
        target_bands: Mapping[str, Any] | None = None,
        driver_spacing_m: float | None = 0.0,
        gain_plan_db: Mapping[str, float] | None = None,
        measure_gain_ceiling_db: Mapping[str, float] | None = None,
        timing_prior: str | None = None,
        measurement_protection_sections_by_role: Mapping[
            str, Sequence[CrossoverSection]
        ]
        | None = None,
        sound_design_revision: int | None = None,
        lateral_prompts: Sequence[CloudPositionPrompt] = (),
        measure_specs_by_index: Mapping[int, MeasureSpec] | None = None,
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
        self._measurement_protection_sections_by_role = None
        if measurement_protection_sections_by_role is not None:
            self._measurement_protection_sections_by_role = {
                str(role): tuple(sections)
                for role, sections in measurement_protection_sections_by_role.items()
            }
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
        self._index_phase_map = dict(index_phase_map)
        self._lateral_indexes = tuple(sorted(
            index for index, phase in self._index_phase_map.items() if phase == PHASE_LATERAL
        ))
        self._gain_plan_db = dict(gain_plan_db) if gain_plan_db else None
        self._measure_gain_ceiling_db = dict(measure_gain_ceiling_db or {})
        # CHECK's measured room floor, held for the MEASURE and lateral priors.
        # In-memory only: CHECK/MEASURE evidence does not carry across sessions.
        self._check_ambient_report: dict[str, Any] | None = None
        self._lateral_prompts = tuple(lateral_prompts)
        self._measure_specs_by_index = (
            measure_specs_by_index if measure_specs_by_index is not None else {}
        )
        # Frozen together so a subset cannot drift.
        self._excitation = _programs.SessionExcitation(
            roles=self._roles,
            caps_dbfs=self._caps,
            session_volume_db=self._session_volume_db,
            fc_hz=self._fc_hz,
            sweep_duration_limits_s=self._sweep_duration_limits_s,
            target_bands=target_bands or {},
        )
        # Composed ONCE and held: ``program_for_phase`` answers by object identity.
        self._check_program = self._excitation.check_program()
        self._measure_program: ExcitationProgram | None = (
            self._excitation.measure_program(self._gain_plan_db)
            if self._gain_plan_db is not None
            else None
        )
        self._verify_program = self._excitation.verify_program()
        # VERIFY's sweep without the courtesy prelude, for ``program_for_phase``
        # alone. Only ``bind_production_play``'s fallback asks that, and only tests
        # take it: a run composes each take by spec (``programs.program_for_spec``).
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
        # Per-SLOT attempt bookkeeping: the phase, or ``phase:index`` for a
        # lateral pose. ONE meter per slot.
        self._slot_attempts: dict[str, SlotAttempts] = {}
        self._timing_prior = timing_prior

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
        take_id = self._timing_prior
        cached = getattr(self, "_summed_alignment_reference_cache", None)
        if cached is None or cached[0] != take_id:
            seam = self._seams.summed_alignment_reference
            reference = (
                _unreadable("no_timing_prior")
                if take_id is None
                else seam(take_id, self.source_preset)
                if seam
                else None
            )
            cached = self._summed_alignment_reference_cache = (take_id, reference)
        return cached[1]

    def set_program(self, phase: str, program: ExcitationProgram) -> None:
        if phase == PHASE_CHECK:
            self._check_program = program

    def set_excitation(self, excitation: _programs.SessionExcitation) -> None:
        self._excitation = excitation

    def set_timing_prior(self, take_id: str | None) -> None:
        self._timing_prior = take_id

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
    def timing_prior(self) -> str | None:
        """The id of the session's timing take, the prior MEASURE reads (ADR-0319), or ``None``."""
        return self._timing_prior

    def phase_of_index(self, index: int) -> str:
        phase = self._index_phase_map.get(index)
        if phase is None:
            raise CrossoverV2FlowError(f"no v2 phase for capture index {index}")
        return phase

    def _slot_of_index(self, index: int) -> str:
        """The retry-budget key for one capture index."""
        phase = self.phase_of_index(index)
        return f"{phase}:{index}" if phase == PHASE_LATERAL else phase

    def snapshot(self) -> V2ConductorSnapshot:
        return V2ConductorSnapshot(
            session_id=self.session_id,
            gain_plan_db=dict(self._gain_plan_db) if self._gain_plan_db else None,
            measure_gain_ceiling_db=dict(self._measure_gain_ceiling_db),
        )

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
            ledger=ledger,
            default_code=REASON_RETRIES_SPENT,
            retry_charge=executor_ledger.charge
            if executor_ledger is not None
            else "operator",
        )
        if decision.kind == _admission.REFUSE_EXTRAS_SPENT:
            raise CaptureBeginRefused(
                decision.code,
                reason_message(decision.code, REASON_REGISTRY[decision.code]),
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
        try:
            ledger.admit()
        except _admission.AttemptOverspendError as exc:
            raise CrossoverV2FlowError(str(exc)) from exc
        log_event(
            logger,
            "correction.crossover_v2_authorized",
            session_id=self.session_id,
            phase=phase,
            index=index,
            attempt=attempt,
            charge=ledger.charge,
            extra_used=ledger.extras_used,
            extra_allowed=ledger.retries_per_pose,
            extra_by_speaker=ledger.by_speaker,
        )

    def program_for_phase(self, phase: str) -> ExcitationProgram:
        """The composed program this session plays for ``phase``."""
        if phase == PHASE_LATERAL and self._branch_program is not None:
            return self._branch_program
        if phase == PHASE_LATERAL and any(
            index in self._lateral_indexes
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
            )
        except _programs.NoProgramForPhaseError as exc:
            raise CrossoverV2FlowError(str(exc)) from exc

    def capture_geometry(self, phase: str, index: int) -> MeasurementGeometry:
        """The session's geometry at this capture's angles; the host adds the
        window its pose picks (ADR-0400)."""
        spec = self._measure_specs_by_index.get(index)
        position, vertical = (
            (spec.positions or (0,))[0] if spec else 0,
            spec.vertical_deg if spec else 0,
        )
        if phase == PHASE_LATERAL:
            # The plan's own prompt for this pose: one per lateral index, in order.
            prompt = self._lateral_prompts[self._lateral_indexes.index(index)]
            position, vertical = (
                position_angle_deg(prompt),
                position_elevation_deg(prompt),
            )
        return replace(
            self._geometry,
            position_deg=position,
            vertical_deg=vertical,
        )

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
