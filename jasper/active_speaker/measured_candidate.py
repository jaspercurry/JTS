# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed measured-crossover candidate boundary.

Wave 2 readiness remains a permanent forensic classification. Wave 3 can
refine only attenuation, retained polarity, and delay from exact evidence
reopened by the commissioning evidence store. It owns no playback, search,
persistence, apply transaction, or acoustic-target claim.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, NoReturn, Sequence

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.null_walk import (
    MAX_REPEAT_SPREAD_DB,
    NullWalkError,
    select_scheduled_delay,
)

from .commissioning_evidence import (
    ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID,
    ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION,
    CompleteCommissioningEvidence,
    CompleteIsolatedDriverEvidence,
    active_region_threshold_profile_fingerprint,
    region_evidence_preset_fingerprint,
)
from .commissioning_evidence_store import (
    CommissioningEvidenceStore,
    CommissioningEvidenceStoreError,
)
from .commissioning_receipt import REFERENCE_AXIS_GEOMETRY_ID
from .commissioning_run import CommissioningRunHandle
from .crossover_alignment import PHASE_AWARE, POLARITY_KEEP, propose_crossover_alignment
from .crossover_contract import (
    DRIVER_EXCITATION_MATCH_TOLERANCE_DB,
    verified_driver_excitation,
)
from .driver_acoustics import DRIVER_ACOUSTIC_KIND, SUMMED_ACOUSTIC_KIND
from .level_trim import LevelTrimError, attenuation_from_group_deltas
from .profile import ActiveSpeakerPreset, required_driver_roles

SCHEMA_VERSION = 1
INPUT_CONTRACT_KIND = "jts_active_measured_candidate_input_contract"
READINESS_KIND = "jts_active_measured_candidate_readiness"
CANDIDATE_KIND = "jts_active_measured_electrical_candidate"
CANDIDATE_ALGORITHM_ID = "jts_active_electrical_preset_refinement"
CANDIDATE_ALGORITHM_VERSION = "1"
ISOLATED_ANALYSIS_KIND = "jts_active_isolated_driver_capture_analysis"
ISOLATED_QUALITY_KIND = "jts_active_isolated_driver_capture_quality"
ISOLATED_ANALYZER_ID = "jts_active_isolated_driver_capture"
ISOLATED_ANALYZER_VERSION = "1"
_MAX_ATTENUATION_DB = -60.0
_CANDIDATE_ALGORITHM = {
    "id": CANDIDATE_ALGORITHM_ID,
    "version": CANDIDATE_ALGORITHM_VERSION,
}
_CANDIDATE_FLAGS = {"score_available": False, "acoustic_target_claimed": False}


class MeasuredCandidateError(ValueError):
    """A measured-candidate value is malformed or unsafe."""


class MeasuredCandidateEvaluationError(MeasuredCandidateError):
    """Exact evidence cannot authorize an electrical candidate."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class MeasuredCandidateRefusal(str, Enum):
    CAPTURE_NOT_ADMITTED = "measured_candidate_capture_not_admitted"
    SHARED_PERSISTED_ADMISSION_UNAVAILABLE = (
        "measured_candidate_shared_persisted_admission_unavailable"
    )
    CURRENT_PROTECTION_PROOF_MISSING = (
        "measured_candidate_current_protection_proof_missing"
    )
    DRIVER_CAPTURES_MISSING = "measured_candidate_driver_captures_missing"
    MEASURED_VALIDITY_BAND_MISSING = "measured_candidate_measured_validity_band_missing"
    DELAY_WALK_MISSING = "measured_candidate_delay_walk_missing"
    NORMAL_EVIDENCE_MISSING = "measured_candidate_normal_evidence_missing"
    REVERSE_EVIDENCE_MISSING = "measured_candidate_reverse_evidence_missing"
    NULL_EVIDENCE_MISSING = "measured_candidate_null_evidence_missing"
    TOPOLOGY_GRAPH_PROOF_MISSING = "measured_candidate_topology_graph_proof_missing"
    CANDIDATE_PUBLICATION_DISABLED = "measured_candidate_publication_disabled_in_wave2"


@dataclass(frozen=True, init=False)
class MeasuredCandidateInputContract:
    fixed_axis_geometry_id: str
    stationary_evidence_roles: tuple[str, ...]
    stationary_capture_count_per_target: int
    null_capture_count_per_delay: int
    capture_distinctness: str
    delay_step_range_us: tuple[int, int]
    delay_bound: str
    measured_search_band_rule: str
    placement_scope: str
    graph_scope: str
    admission_scope: str
    candidate_output_enabled: bool
    fingerprint: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use measured_candidate_input_contract")

    @classmethod
    def _wave2(cls) -> MeasuredCandidateInputContract:
        self = object.__new__(cls)
        values = {
            "fixed_axis_geometry_id": REFERENCE_AXIS_GEOMETRY_ID,
            "stationary_evidence_roles": (
                "isolated_driver",
                "combined_normal",
                "combined_reverse",
            ),
            "stationary_capture_count_per_target": 3,
            "null_capture_count_per_delay": 5,
            "capture_distinctness": "unique_capture_and_artifact_fingerprints_within_run",
            "delay_step_range_us": (50, 100),
            "delay_bound": "declared_geometry_plus_minus_half_period",
            "measured_search_band_rule": "profile_intersection_tightened_by_per_band_validity_and_snr",
            "placement_scope": "one_exact_fixed_axis_placement_per_topology_derived_group",
            "graph_scope": "exact_topology_wide_routing_filters_gain_protection_and_nonpositive_volume",
            "admission_scope": "fresh_persisted_planner_and_playback_recheck_against_current_active_safety_plan",
            "candidate_output_enabled": False,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))
        return self

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": INPUT_CONTRACT_KIND,
            **{
                name: list(value) if isinstance(value, tuple) else value
                for name, value in self.__dict__.items()
                if name != "fingerprint"
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


def measured_candidate_input_contract() -> MeasuredCandidateInputContract:
    return MeasuredCandidateInputContract._wave2()


@dataclass(frozen=True, init=False)
class MeasuredCandidateReadiness:
    source_classification: str
    refusals: tuple[MeasuredCandidateRefusal, ...]
    input_contract: MeasuredCandidateInputContract
    fingerprint: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use a measured candidate readiness factory")

    @classmethod
    def _not_ready(
        cls,
        source_classification: str,
        refusals: tuple[MeasuredCandidateRefusal, ...],
    ) -> MeasuredCandidateReadiness:
        if not source_classification.strip() or len(set(refusals)) != len(refusals):
            raise MeasuredCandidateError("non-ready classification is malformed")
        if MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED not in refusals:
            raise MeasuredCandidateError("non-ready state must block publication")
        self = object.__new__(cls)
        object.__setattr__(self, "source_classification", source_classification)
        object.__setattr__(self, "refusals", refusals)
        object.__setattr__(self, "input_contract", measured_candidate_input_contract())
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))
        return self

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": READINESS_KIND,
            "source_classification": self.source_classification,
            "input_contract": self.input_contract.to_dict(),
            "ready": False,
            "score_available": False,
            "candidate_authority": False,
            "persistable_candidate": False,
            "apply_authority": False,
            "receipt_authority": False,
            "refusals": [reason.value for reason in self.refusals],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


def legacy_measured_candidate_readiness() -> MeasuredCandidateReadiness:
    return MeasuredCandidateReadiness._not_ready(
        "historical_legacy_non_admitted",
        (
            MeasuredCandidateRefusal.CAPTURE_NOT_ADMITTED,
            MeasuredCandidateRefusal.DRIVER_CAPTURES_MISSING,
            MeasuredCandidateRefusal.MEASURED_VALIDITY_BAND_MISSING,
            MeasuredCandidateRefusal.DELAY_WALK_MISSING,
            MeasuredCandidateRefusal.NORMAL_EVIDENCE_MISSING,
            MeasuredCandidateRefusal.REVERSE_EVIDENCE_MISSING,
            MeasuredCandidateRefusal.NULL_EVIDENCE_MISSING,
            MeasuredCandidateRefusal.TOPOLOGY_GRAPH_PROOF_MISSING,
            MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED,
        ),
    )


def wave2_measured_candidate_readiness() -> MeasuredCandidateReadiness:
    return MeasuredCandidateReadiness._not_ready(
        "wave2_shared_boundary_pending",
        (
            MeasuredCandidateRefusal.SHARED_PERSISTED_ADMISSION_UNAVAILABLE,
            MeasuredCandidateRefusal.CURRENT_PROTECTION_PROOF_MISSING,
            MeasuredCandidateRefusal.TOPOLOGY_GRAPH_PROOF_MISSING,
            MeasuredCandidateRefusal.CANDIDATE_PUBLICATION_DISABLED,
        ),
    )


def _refuse(code: str, detail: str) -> NoReturn:
    raise MeasuredCandidateEvaluationError(code, detail)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("artifact_semantics_invalid", f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        _refuse("artifact_semantics_invalid", f"{name} must be finite")
    return result


def _sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MeasuredCandidateError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True, init=False)
class MeasuredElectricalCandidate:
    """Compact attenuation, retained-polarity, and delay refinement."""

    run: CommissioningRunHandle
    plan_fingerprint: str
    source_preset_fingerprint: str
    isolated_evidence_artifact: ArtifactIdentity
    summed_evidence_artifact: ArtifactIdentity
    source_preset: ActiveSpeakerPreset
    role_attenuations_db: tuple[tuple[str, float], ...]
    role_delays_ms: tuple[tuple[str, float], ...]
    fingerprint: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use evaluate_measured_candidate")

    @classmethod
    def _create(
        cls,
        *,
        run: CommissioningRunHandle,
        plan_fingerprint: str,
        source_preset_fingerprint: str,
        isolated_evidence_artifact: ArtifactIdentity,
        summed_evidence_artifact: ArtifactIdentity,
        source_preset: ActiveSpeakerPreset,
        role_attenuations_db: tuple[tuple[str, float], ...],
        role_delays_ms: tuple[tuple[str, float], ...],
    ) -> MeasuredElectricalCandidate:
        if not isinstance(run, CommissioningRunHandle):
            raise MeasuredCandidateError("candidate run is invalid")
        if not isinstance(
            isolated_evidence_artifact, ArtifactIdentity
        ) or not isinstance(summed_evidence_artifact, ArtifactIdentity):
            raise MeasuredCandidateError("candidate evidence identity is invalid")
        if (
            isolated_evidence_artifact.bundle_id != run.session_id
            or summed_evidence_artifact.bundle_id != run.session_id
        ):
            raise MeasuredCandidateError("candidate evidence belongs to another run")
        plan_fingerprint = _sha256(plan_fingerprint, "plan_fingerprint")
        source_preset_fingerprint = _sha256(
            source_preset_fingerprint, "source_preset_fingerprint"
        )
        roles = required_driver_roles(source_preset.way_count)
        if (
            tuple(role for role, _ in role_attenuations_db) != roles
            or tuple(role for role, _ in role_delays_ms) != roles
        ):
            raise MeasuredCandidateError("candidate role values are incomplete")
        for _, value in role_attenuations_db:
            if (
                not math.isfinite(value)
                or value > 0.0
                or value < _MAX_ATTENUATION_DB
                or not math.isclose(value, round(value, 1), abs_tol=1e-9)
            ):
                raise MeasuredCandidateError("candidate attenuation is invalid")
        if any(
            not math.isfinite(value) or not 0.0 <= value <= 20.0
            for _, value in role_delays_ms
        ) or not any(value == 0.0 for _, value in role_delays_ms):
            raise MeasuredCandidateError("candidate delay is invalid")
        source_preset.validate()
        if source_preset_fingerprint != region_evidence_preset_fingerprint(
            source_preset
        ):
            raise MeasuredCandidateError("candidate source preset fingerprint is stale")
        self = object.__new__(cls)
        attributes: tuple[tuple[str, Any], ...] = (
            ("run", run),
            ("plan_fingerprint", plan_fingerprint),
            ("source_preset_fingerprint", source_preset_fingerprint),
            ("isolated_evidence_artifact", isolated_evidence_artifact),
            ("summed_evidence_artifact", summed_evidence_artifact),
            ("source_preset", source_preset),
            ("role_attenuations_db", role_attenuations_db),
            ("role_delays_ms", role_delays_ms),
        )
        for name, attribute in attributes:
            object.__setattr__(self, name, attribute)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))
        return self

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": CANDIDATE_KIND,
            "algorithm": _CANDIDATE_ALGORITHM,
            "run": asdict(self.run),
            "plan_fingerprint": self.plan_fingerprint,
            "source_preset_fingerprint": self.source_preset_fingerprint,
            "isolated_evidence_artifact": self.isolated_evidence_artifact.to_dict(),
            "summed_evidence_artifact": self.summed_evidence_artifact.to_dict(),
            "source_preset": self.source_preset.to_dict(),
            "role_attenuations_db": dict(self.role_attenuations_db),
            "role_delays_ms": dict(self.role_delays_ms),
            "classification": "electrical_preset_refinement",
            "source": "reviewed_preset",
            "flags": _CANDIDATE_FLAGS,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: Any) -> MeasuredElectricalCandidate:
        """Strictly reopen one persisted evaluator result without re-scoring WAVs."""

        expected = {
            "schema_version",
            "kind",
            "algorithm",
            "run",
            "plan_fingerprint",
            "source_preset_fingerprint",
            "isolated_evidence_artifact",
            "summed_evidence_artifact",
            "source_preset",
            "role_attenuations_db",
            "role_delays_ms",
            "classification",
            "source",
            "flags",
            "fingerprint",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise MeasuredCandidateError(
                "measured candidate has unknown or missing fields"
            )
        run_raw = raw["run"]
        run_fields = {
            "session_id",
            "session_fingerprint",
            "run_id",
            "owner_id",
            "owner_generation",
        }
        if not isinstance(run_raw, Mapping) or set(run_raw) != run_fields:
            raise MeasuredCandidateError("candidate run fields are invalid")
        preset = ActiveSpeakerPreset.from_mapping(raw["source_preset"])
        roles = required_driver_roles(preset.way_count)

        def role_values(name: str) -> tuple[tuple[str, float], ...]:
            values = raw[name]
            if not isinstance(values, Mapping) or set(values) != set(roles):
                raise MeasuredCandidateError(f"candidate {name} is incomplete")
            return tuple(
                (role, _finite(values[role], f"{name}.{role}"))
                for role in roles
            )

        try:
            candidate = cls._create(
                run=CommissioningRunHandle(
                    **{name: run_raw[name] for name in run_fields}
                ),
                plan_fingerprint=str(raw["plan_fingerprint"]),
                source_preset_fingerprint=str(raw["source_preset_fingerprint"]),
                isolated_evidence_artifact=ArtifactIdentity.from_mapping(
                    raw["isolated_evidence_artifact"]
                ),
                summed_evidence_artifact=ArtifactIdentity.from_mapping(
                    raw["summed_evidence_artifact"]
                ),
                source_preset=preset,
                role_attenuations_db=role_values("role_attenuations_db"),
                role_delays_ms=role_values("role_delays_ms"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, MeasuredCandidateError):
                raise
            raise MeasuredCandidateError(str(exc)) from exc
        if candidate.to_dict() != dict(raw):
            raise MeasuredCandidateError(
                "persisted measured candidate does not match its declared result"
            )
        return candidate


def _capture_row(
    store: CommissioningEvidenceStore,
    capture: Any,
    *,
    isolated: bool,
    expect_null: bool = False,
    crossover_fc_hz: float | None = None,
) -> tuple[dict[str, Any], str, float | None]:
    """Stream and semantically join one analysis/quality artifact pair."""

    analysis = store.reopen_json_artifact(capture.capture.analysis_input_artifact)
    quality = store.reopen_json_artifact(capture.capture.quality_artifact)
    algorithm_id = (
        ISOLATED_ANALYZER_ID if isolated else ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID
    )
    algorithm_version = (
        ISOLATED_ANALYZER_VERSION
        if isolated
        else ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION
    )
    expected = {
        "schema_version": SCHEMA_VERSION,
        "kind": ISOLATED_ANALYSIS_KIND
        if isolated
        else "jts_active_summed_capture_analysis",
        "algorithm_id": algorithm_id,
        "algorithm_version": algorithm_version,
        "threshold_profile_fingerprint": active_region_threshold_profile_fingerprint(),
        "target_fingerprint": capture.capture.target_fingerprint,
        "context_fingerprint": capture.context_fingerprint,
        "graph_fingerprint": capture.graph_fingerprint,
        "raw_artifact": capture.capture.raw_artifact.to_dict(),
        "stimulus": capture.stimulus.to_dict(),
        "generation_artifact": capture.generation_artifact.to_dict(),
        "playback_artifact": capture.playback_artifact.to_dict(),
        "capture_geometry": REFERENCE_AXIS_GEOMETRY_ID,
    }
    if isolated:
        expected.update(
            {
                "plan_fingerprint": capture.plan_fingerprint,
                "evidence_target_fingerprint": capture.evidence_target_fingerprint,
                "driver_target_id": capture.driver_target_id,
                "driver_target_fingerprint": capture.driver_target_fingerprint,
            }
        )
        expected.pop("target_fingerprint")
    if any(analysis.get(name) != value for name, value in expected.items()):
        _refuse("capture_analysis_mismatch", "analysis is stale from its typed capture")
    issuance = analysis.get("issuance_id")
    operation = analysis.get("operation_fingerprint")
    quality_expected = {
        "schema_version": SCHEMA_VERSION,
        "kind": ISOLATED_QUALITY_KIND
        if isolated
        else "jts_active_summed_capture_quality",
        "algorithm_id": algorithm_id,
        "algorithm_version": algorithm_version,
        "threshold_profile_fingerprint": expected["threshold_profile_fingerprint"],
        "operation_fingerprint": operation,
        "issuance_id": issuance,
        "raw_artifact_fingerprint": capture.capture.raw_artifact.fingerprint,
        "analysis_artifact_fingerprint": capture.capture.analysis_input_artifact.fingerprint,
        "accepted": True,
        "issues": [],
    }
    if (
        not isinstance(issuance, str)
        or capture.capture.capture_id != f"capture-{issuance}"
        or not isinstance(operation, str)
        or any(quality.get(name) != value for name, value in quality_expected.items())
    ):
        _refuse("capture_quality_refused", "quality is stale, mismatched, or refused")
    calibration = analysis.get("calibration")
    if not isinstance(calibration, Mapping) or set(calibration) != {
        "fingerprint",
        "curve",
    }:
        _refuse("calibration_invalid", "capture calibration is malformed")
    calibration_fp = calibration["fingerprint"]
    if calibration_fp != json_fingerprint(
        {"schema_version": 1, "curve": calibration["curve"]}
    ):
        _refuse("calibration_invalid", "capture calibration fingerprint is stale")
    acoustic = analysis.get("acoustic")
    if not isinstance(acoustic, dict):
        _refuse("capture_acoustic_invalid", "capture acoustic evidence is malformed")
    common = {
        "calibrated": True,
        "capture_geometry": REFERENCE_AXIS_GEOMETRY_ID,
        "mic_clipping": False,
    }
    gating, snr = acoustic.get("gating"), acoustic.get("snr")
    if (
        any(acoustic.get(name) != value for name, value in common.items())
        or not isinstance(gating, Mapping)
        or gating.get("applied") is not True
        or not isinstance(snr, Mapping)
        or snr.get("verdict") != "ok"
    ):
        _refuse(
            "capture_acoustic_unsafe",
            "capture lacks calibrated, gated, unclipped SNR evidence",
        )
    if isolated:
        excitation = verified_driver_excitation(analysis.get("excitation"))
        admitted_effective = capture.generation_admission.request.effective_peak_dbfs
        overlaps = acoustic.get("overlap_levels")
        if (
            excitation is None
            or excitation.get("role") != capture.role
            or not math.isclose(
                float(excitation["effective_peak_dbfs"]),
                admitted_effective,
                rel_tol=0.0,
                abs_tol=DRIVER_EXCITATION_MATCH_TOLERANCE_DB,
            )
            or acoustic.get("kind") != DRIVER_ACOUSTIC_KIND
            or acoustic.get("present") is not True
            or snr.get("decision_class") != "magnitude"
            or not isinstance(overlaps, list)
            or not overlaps
        ):
            _refuse("isolated_capture_unsafe", "isolated evidence is unusable")
        return acoustic, str(calibration_fp), admitted_effective
    if (
        acoustic.get("kind") != SUMMED_ACOUSTIC_KIND
        or any(
            acoustic.get(name) != value
            for name, value in {
                "expect_null": expect_null,
                "above_validity_floor": True,
                "near_validity_floor": False,
                "null_depth_capped": False,
            }.items()
        )
        or snr.get("decision_class") != "alignment"
        or crossover_fc_hz is None
        or not math.isclose(
            _finite(acoustic.get("crossover_fc_hz"), "crossover Fc"),
            crossover_fc_hz,
            abs_tol=1e-3,
        )
    ):
        _refuse("summed_capture_unsafe", "summed evidence is unusable or stale")
    _finite(acoustic.get("null_depth_db"), "null depth")
    return acoustic, str(calibration_fp), None


def _spread(values: Sequence[float], code: str) -> float:
    if not values or max(values) - min(values) >= MAX_REPEAT_SPREAD_DB:
        _refuse(code, "repeat spread exceeds the existing 2 dB bound")
    return statistics.median(values)


def _isolated_levels(
    store: CommissioningEvidenceStore,
    evidence: CompleteIsolatedDriverEvidence,
) -> tuple[dict[tuple[str, str], dict[float, float]], str]:
    result: dict[tuple[str, str], dict[float, float]] = {}
    calibration_fp: str | None = None
    for driver in evidence.drivers:
        rows: list[dict[float, float]] = []
        for capture in driver.captures:
            acoustic, observed_calibration, excitation = _capture_row(
                store, capture, isolated=True
            )
            if calibration_fp not in {None, observed_calibration}:
                _refuse(
                    "calibration_mismatch",
                    "candidate evidence uses multiple calibrations",
                )
            calibration_fp = observed_calibration
            assert excitation is not None
            levels: dict[float, float] = {}
            for overlap in acoustic["overlap_levels"]:
                if (
                    not isinstance(overlap, Mapping)
                    or overlap.get("usable") is not True
                    or overlap.get("above_validity_floor") is not True
                    or overlap.get("near_validity_floor") is not False
                    or overlap.get("snr_verdict") != "ok"
                ):
                    _refuse("isolated_overlap_unsafe", "isolated overlap is unusable")
                fc = _finite(overlap.get("fc_hz"), "overlap Fc")
                if fc in levels:
                    _refuse(
                        "isolated_overlap_duplicate",
                        "isolated capture repeats an overlap Fc",
                    )
                levels[fc] = (
                    _finite(overlap.get("level_db"), "overlap level") - excitation
                )
            rows.append(levels)
        if any(set(row) != set(rows[0]) for row in rows):
            _refuse(
                "isolated_overlap_mismatch", "isolated repeats cover different bands"
            )
        medians = {
            fc: _spread([row[fc] for row in rows], "isolated_repeat_spread")
            for fc in rows[0]
        }
        result[driver.canonical_key] = medians
    assert calibration_fp is not None
    return result, calibration_fp


def _level(levels: Mapping[float, float], fc_hz: float) -> float:
    matches = [
        value
        for fc, value in levels.items()
        if math.isclose(fc, fc_hz, rel_tol=0.0, abs_tol=1.0)
    ]
    if len(matches) != 1:
        _refuse("isolated_overlap_missing", "one exact crossover overlap is required")
    return matches[0]


def _attenuations(
    preset: ActiveSpeakerPreset,
    evidence: CompleteIsolatedDriverEvidence,
    levels: Mapping[tuple[str, str], Mapping[float, float]],
) -> tuple[tuple[str, float], ...]:
    roles = required_driver_roles(preset.way_count)
    groups = sorted({driver.speaker_group_id for driver in evidence.drivers})
    group_deltas = []
    for group in groups:
        deltas = [
            (
                region.lower_driver,
                region.upper_driver,
                _level(levels[(group, region.upper_driver)], region.fc_hz)
                - _level(levels[(group, region.lower_driver)], region.fc_hz),
            )
            for region in sorted(preset.crossover_regions, key=lambda item: item.fc_hz)
        ]
        group_deltas.append(deltas)
    try:
        trims = attenuation_from_group_deltas(
            roles, group_deltas, reject_below_db=_MAX_ATTENUATION_DB
        )
    except LevelTrimError:
        _refuse(
            "candidate_attenuation_out_of_range", "required attenuation is below -60 dB"
        )
    return tuple((role, trims[role]) for role in roles)


def _stationary(
    store: CommissioningEvidenceStore,
    captures: Sequence[Any],
    *,
    expect_null: bool,
    fc_hz: float,
) -> tuple[float, str]:
    depths: list[float] = []
    calibration: str | None = None
    for capture in captures:
        acoustic, observed, _ = _capture_row(
            store,
            capture,
            isolated=False,
            expect_null=expect_null,
            crossover_fc_hz=fc_hz,
        )
        if calibration not in {None, observed}:
            _refuse(
                "calibration_mismatch", "stationary repeats use multiple calibrations"
            )
        calibration = observed
        depths.append(_finite(acoustic["null_depth_db"], "null depth"))
    assert calibration is not None
    return _spread(depths, "stationary_repeat_spread"), calibration


def _candidate_delays(
    store: CommissioningEvidenceStore,
    preset: ActiveSpeakerPreset,
    evidence: CompleteCommissioningEvidence,
    calibration_fp: str,
) -> tuple[tuple[str, float], ...]:
    by_id: dict[str, list[tuple[str | None, float | None]]] = {}
    preset_regions = {region.id: region for region in preset.crossover_regions}
    for item in evidence.regions:
        target = item.target
        source = preset_regions.get(target.region_id)
        if source is None:
            _refuse(
                "preset_plan_mismatch", "evidence attempts to change crossover design"
            )
        normal, normal_cal = _stationary(
            store, item.normal.captures, expect_null=False, fc_hz=source.fc_hz
        )
        reverse_depth, reverse_cal = _stationary(
            store, item.reverse.captures, expect_null=True, fc_hz=source.fc_hz
        )
        if {normal_cal, reverse_cal} != {calibration_fp}:
            _refuse("calibration_mismatch", "summed and isolated calibration differs")
        proposal = propose_crossover_alignment(
            mode=PHASE_AWARE,
            crossover_fc_hz=source.fc_hz,
            lower_role=source.lower_driver,
            upper_role=source.upper_driver,
            in_phase_null_depth_db=normal,
            reverse_null_depth_db=reverse_depth,
            alignment_snr_ok=True,
            null_depth_capped=False,
        )
        if proposal.polarity_action == "invert":
            _refuse(
                "candidate_polarity_change_requires_oriented_delay_walk",
                "delay walk is oriented to reviewed polarity",
            )
        if not proposal.authorized or proposal.polarity_action != POLARITY_KEEP:
            _refuse(
                "candidate_polarity_inconclusive", "polarity keep is not authorized"
            )
        walk = item.delay_walk
        rows: dict[float, list[dict[str, Any]]] = {}
        for point in walk.points:
            rows[point.relative_delay_us] = []
            for capture in point.captures:
                acoustic, observed, _ = _capture_row(
                    store,
                    capture,
                    isolated=False,
                    expect_null=True,
                    crossover_fc_hz=source.fc_hz,
                )
                if observed != calibration_fp:
                    _refuse(
                        "calibration_mismatch", "delay evidence calibration differs"
                    )
                rows[point.relative_delay_us].append(acoustic)
        try:
            selection = select_scheduled_delay(walk.spec, walk.schedule, rows)
        except NullWalkError as exc:
            _refuse("delay_selection_refused", str(exc))
        if selection.get("status") != "selected":
            _refuse(
                "delay_selection_refused",
                str(selection.get("reason") or "delay selection was refused"),
            )
        if (
            _finite(selection.get("selected_null_depth_db"), "selected null")
            < source.null_depth_threshold_db
        ):
            _refuse(
                "selected_null_too_shallow", "selected delay misses reviewed threshold"
            )
        target_role = selection.get("selected_delay_target")
        delay = selection.get("selected_delay_us")
        by_id.setdefault(source.id, []).append(
            (
                target_role,
                None if target_role is None else _finite(delay, "selected delay"),
            )
        )

    roles = required_driver_roles(preset.way_count)
    regions_by_pair = {
        (region.lower_driver, region.upper_driver): region
        for region in preset.crossover_regions
    }
    relative_by_pair: dict[tuple[str, str], float] = {}
    for lower_role, upper_role in zip(roles, roles[1:]):
        source = regions_by_pair[(lower_role, upper_role)]
        choices = set(by_id.get(source.id, []))
        if len(choices) != 1:
            _refuse(
                "stereo_delay_consensus_missing", "groups lack one exact delay choice"
            )
        target_role, delay_us = choices.pop()
        if target_role is None:
            relative_by_pair[(lower_role, upper_role)] = 0.0
        elif (
            target_role in {lower_role, upper_role}
            and delay_us is not None
            and delay_us > 0.0
        ):
            relative_by_pair[(lower_role, upper_role)] = (
                delay_us if target_role == upper_role else -delay_us
            )
        else:
            _refuse("delay_selection_invalid", "selected delay is outside the region")

    absolute_us = {roles[0]: 0.0}
    for lower_role, upper_role in zip(roles, roles[1:]):
        absolute_us[upper_role] = (
            absolute_us[lower_role]
            + relative_by_pair[(lower_role, upper_role)]
        )
    offset = min(absolute_us.values())
    delays = tuple(
        (role, round((absolute_us[role] - offset) / 1000.0, 6)) for role in roles
    )
    if any(delay_ms > 20.0 for _, delay_ms in delays):
        _refuse(
            "candidate_delay_out_of_range",
            "composed driver delay exceeds the 20 ms DSP bound",
        )
    return delays


def evaluate_measured_candidate(
    *,
    store: CommissioningEvidenceStore,
    run: CommissioningRunHandle,
    reviewed_preset: ActiveSpeakerPreset,
    isolated_evidence_artifact: ArtifactIdentity,
    summed_evidence_artifact: ArtifactIdentity,
) -> MeasuredElectricalCandidate:
    """Reopen exact run evidence and derive one deterministic candidate."""

    if store.session_id != run.session_id:
        _refuse("evidence_run_mismatch", "store does not belong to the current run")
    try:
        isolated = store.reopen_complete_isolated_driver_evidence(
            run_id=run.run_id, artifact=isolated_evidence_artifact
        )
        summed = store.reopen_complete_commissioning_evidence(
            run_id=run.run_id, artifact=summed_evidence_artifact
        )
    except CommissioningEvidenceStoreError as exc:
        _refuse("complete_evidence_reopen_failed", str(exc))
    if isolated.plan.authority.run != run or summed.plan.authority.run != run:
        _refuse(
            "evidence_run_mismatch",
            "reopened authority is not the exact current owner generation",
        )
    if isolated.plan != summed.plan:
        _refuse(
            "evidence_plan_mismatch", "complete evidence sets do not share one plan"
        )
    source_fingerprint = region_evidence_preset_fingerprint(reviewed_preset)
    if (
        isolated.plan.preset_id != reviewed_preset.preset_id
        or isolated.plan.preset_fingerprint != source_fingerprint
    ):
        _refuse("source_preset_stale", "reviewed preset differs from the evidence plan")
    placements = {
        driver.speaker_group_id: driver.placement_fingerprint
        for driver in isolated.drivers
    }
    if any(
        placements.get(item.target.speaker_group_id)
        != item.normal.placement_fingerprint
        for item in summed.regions
    ):
        _refuse(
            "placement_mismatch", "isolated and summed evidence use different placement"
        )
    levels, calibration = _isolated_levels(store, isolated)
    attenuations = _attenuations(reviewed_preset, isolated, levels)
    delays = _candidate_delays(store, reviewed_preset, summed, calibration)
    return MeasuredElectricalCandidate._create(
        run=run,
        plan_fingerprint=isolated.plan.fingerprint,
        source_preset_fingerprint=source_fingerprint,
        isolated_evidence_artifact=isolated_evidence_artifact,
        summed_evidence_artifact=summed_evidence_artifact,
        source_preset=reviewed_preset,
        role_attenuations_db=attenuations,
        role_delays_ms=delays,
    )
