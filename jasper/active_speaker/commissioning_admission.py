# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Production admission adapter for one isolated Active driver capture.

The caller owns the existing DSP writer lock and keeps it held across transient
graph load, this adapter, and rollback.  This module owns Active policy: exact
current comparison/profile/target limits, fresh live-graph proof, one-shot WAV
generation, and the handoff that later capture persistence can validate. Shared
owns canonical persistence and guarded playback.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jasper.audio_measurement.admitted_playback import (
    CurrentPlaybackAdmissionInputs,
    GeneratedExcitationWav,
    bind_generated_excitation_wav,
    play_admitted_wav,
)
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import (
    ProtectionEvidence,
    admit_excitation,
)
from jasper.audio_measurement.excitation_artifacts import (
    GenerationAdmissionArtifact,
    persist_generation_admission,
    read_generation_admission,
    read_playback_admission,
)
from jasper.audio_measurement.sweep import (
    SweepMeta,
    synchronized_sweep_metadata,
    synchronized_swept_sine,
    write_sweep_wav,
)
from jasper.camilla_config_contract import DEFAULT_CAPTURE_DEVICE, DEFAULT_SAMPLE_RATE
from jasper.camilla import CamillaUnavailable
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from . import graph_safety as gs
from .bundles import open_bundle_admission_authority
from .camilla_yaml import (
    COMMISSIONING_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    output_commission_mute_name,
)
from .capture_geometry import comparison_set_valid
from .driver_safety import evaluate_driver_safety_profile
from .excitation_safety_plan import (
    DriverSweepGeneratorPlan,
    PreparedDriverExcitationPlan,
    RequestedDriverExcitationPlan,
    prepare_driver_excitation_plan,
)
from .graph_evidence import driver_limiter_name
from .measurement import active_driver_targets
from .staging import running_commission_evidence
from .test_signal_plan import (
    CROSSOVER_AMBIENT_DURATION_S,
    CROSSOVER_CAPTURE_PLAY_DEADLINE_S,
    DRIVER_SWEEP_DURATIONS_S,
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    driver_sweep_duration_s,
)

ADMISSION_HANDOFF_SCHEMA_VERSION = 1
ADMISSION_HANDOFF_KIND = "jts_active_driver_capture_admission_handoff"
ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS = -12.0
ACTIVE_DRIVER_CAPTURE_REPEAT_COUNT = 1
# The relay's armed-to-sweep deadline must also contain the controlled ambient
# interval, longest protected sweep, graph load/readback/restore, and relay
# posts. Keep an explicit nine-second operations budget instead of accepting a
# profile cooldown that can only time out after the phone starts recording.
ACTIVE_DRIVER_CAPTURE_GRAPH_AND_RELAY_BUDGET_S = 9.0
MAX_AUTOMATIC_DRIVER_COOLDOWN_S = max(
    0.0,
    CROSSOVER_CAPTURE_PLAY_DEADLINE_S
    - CROSSOVER_AMBIENT_DURATION_S
    - max(DRIVER_SWEEP_DURATIONS_S.values())
    - ACTIVE_DRIVER_CAPTURE_GRAPH_AND_RELAY_BUDGET_S,
)
ACTIVE_COMMISSIONING_PLAYBACK_FAILURE_REASONS = frozenset({
    "main_volume_drift",
    "post_play_volume_unverified",
    "post_play_volume_verification_cancelled",
})
logger = logging.getLogger(__name__)


class ActiveCommissioningAdmissionError(RuntimeError):
    """Active could not prove or persist one automatic capture attempt."""


class ActiveCommissioningPlaybackDrift(ActiveCommissioningAdmissionError):
    """Playback completed, but mutable runtime state drifted during audio."""

    def __init__(
        self,
        detail: str,
        *,
        reason: str,
        admission_id: str,
        playback_artifact: ArtifactIdentity,
    ) -> None:
        super().__init__(detail)
        if reason not in ACTIVE_COMMISSIONING_PLAYBACK_FAILURE_REASONS:
            raise ValueError("reason must be a known post-playback failure")
        self.reason = reason
        self.admission_id = admission_id
        self.playback_artifact = playback_artifact
        self.audio_may_have_started = True


@dataclass(frozen=True, slots=True)
class ActiveCaptureAdmissionHandoff:
    """Strict server-owned join from admitted playback to captured evidence."""

    session_id: str
    comparison_set_id: str
    comparison_set_fingerprint: str
    admission_id: str
    target_id: str
    target_fingerprint: str
    authority_fingerprint: str
    generation_artifact: ArtifactIdentity
    playback_artifact: ArtifactIdentity
    stimulus: GeneratedExcitationWav
    admission: Mapping[str, Any]
    graph_evidence_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "comparison_set_id",
            "admission_id",
            "target_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty trimmed text")
        for name in (
            "comparison_set_fingerprint",
            "target_fingerprint",
            "authority_fingerprint",
            "graph_evidence_fingerprint",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if not isinstance(self.generation_artifact, ArtifactIdentity):
            raise ValueError("generation_artifact must be an ArtifactIdentity")
        if not isinstance(self.playback_artifact, ArtifactIdentity):
            raise ValueError("playback_artifact must be an ArtifactIdentity")
        if not isinstance(self.stimulus, GeneratedExcitationWav):
            raise ValueError("stimulus must be a GeneratedExcitationWav")
        if not isinstance(self.admission, Mapping):
            raise ValueError("admission must be a mapping")
        object.__setattr__(self, "admission", dict(self.admission))
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": ADMISSION_HANDOFF_SCHEMA_VERSION,
            "kind": ADMISSION_HANDOFF_KIND,
            "session_id": self.session_id,
            "comparison_set_id": self.comparison_set_id,
            "comparison_set_fingerprint": self.comparison_set_fingerprint,
            "admission_id": self.admission_id,
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "authority_fingerprint": self.authority_fingerprint,
            "generation_artifact": self.generation_artifact.to_dict(),
            "playback_artifact": self.playback_artifact.to_dict(),
            "stimulus": self.stimulus.to_dict(),
            "admission": dict(self.admission),
            "graph_evidence_fingerprint": self.graph_evidence_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: object) -> "ActiveCaptureAdmissionHandoff":
        if not isinstance(raw, Mapping):
            raise ValueError("capture admission handoff must be an object")
        expected = {
            "schema_version",
            "kind",
            "session_id",
            "comparison_set_id",
            "comparison_set_fingerprint",
            "admission_id",
            "target_id",
            "target_fingerprint",
            "authority_fingerprint",
            "generation_artifact",
            "playback_artifact",
            "stimulus",
            "admission",
            "graph_evidence_fingerprint",
            "fingerprint",
        }
        if set(raw) != expected:
            raise ValueError("capture admission handoff fields are invalid")
        if (
            raw["schema_version"] != ADMISSION_HANDOFF_SCHEMA_VERSION
            or raw["kind"] != ADMISSION_HANDOFF_KIND
        ):
            raise ValueError("capture admission handoff schema is unsupported")
        result = cls(
            session_id=raw["session_id"],
            comparison_set_id=raw["comparison_set_id"],
            comparison_set_fingerprint=raw["comparison_set_fingerprint"],
            admission_id=raw["admission_id"],
            target_id=raw["target_id"],
            target_fingerprint=raw["target_fingerprint"],
            authority_fingerprint=raw["authority_fingerprint"],
            generation_artifact=ArtifactIdentity.from_mapping(
                raw["generation_artifact"]
            ),
            playback_artifact=ArtifactIdentity.from_mapping(raw["playback_artifact"]),
            stimulus=GeneratedExcitationWav.from_mapping(raw["stimulus"]),
            admission=raw["admission"],
            graph_evidence_fingerprint=raw["graph_evidence_fingerprint"],
        )
        if raw["fingerprint"] != result.fingerprint:
            raise ValueError("capture admission handoff fingerprint is invalid")
        return result


@dataclass(frozen=True, slots=True)
class ActiveDriverCapturePlayback:
    sweep_meta: SweepMeta
    handoff: ActiveCaptureAdmissionHandoff


def validate_capture_admission_handoff(
    raw: object,
    *,
    topology: OutputTopology,
    comparison_set: Mapping[str, Any],
    speaker_group_id: str,
    role: str,
) -> dict[str, Any]:
    """Re-verify a server-owned playback handoff before capture persistence."""

    handoff = ActiveCaptureAdmissionHandoff.from_mapping(raw)
    target = _target(topology, speaker_group_id=speaker_group_id, role=role)
    expected_session = comparison_set.get("bundle_session_id")
    if (
        not comparison_set_valid(comparison_set)
        or handoff.session_id != expected_session
        or handoff.comparison_set_id != comparison_set.get("comparison_set_id")
        or handoff.comparison_set_fingerprint != comparison_set.get("fingerprint")
        or handoff.target_id != target.get("target_id")
        or handoff.target_fingerprint != target.get("target_fingerprint")
    ):
        raise ActiveCommissioningAdmissionError(
            "capture admission handoff does not match the current target context"
        )
    from .bundles import sessions_dir
    authority = open_bundle_admission_authority(
        sessions_dir() / handoff.session_id,
        expected_session_id=handoff.session_id,
    )
    if authority.fingerprint != handoff.authority_fingerprint:
        raise ActiveCommissioningAdmissionError(
            "capture admission authority changed before persistence"
        )
    generation = read_generation_admission(authority, handoff.generation_artifact)
    playback = read_playback_admission(
        authority,
        generation,
        handoff.playback_artifact,
    )
    if (
        generation.admission_id != handoff.admission_id
        or playback.admission.to_dict() != dict(handoff.admission)
        or playback.admission.protection_evidence is None
        or playback.admission.protection_evidence.evidence_fingerprint
        != handoff.graph_evidence_fingerprint
        or handoff.stimulus.generation_artifact_fingerprint
        != generation.artifact.fingerprint
        or handoff.stimulus.artifact.bundle_kind != authority.bundle_kind
        or handoff.stimulus.artifact.bundle_id != authority.bundle_id
    ):
        raise ActiveCommissioningAdmissionError(
            "capture admission handoff artifacts are inconsistent"
        )
    stimulus_path = authority.directory.joinpath(
        *handoff.stimulus.artifact.relative_path.split("/")
    )
    if stimulus_path.is_symlink() or stimulus_path.parent.is_symlink():
        raise ActiveCommissioningAdmissionError(
            "admitted capture stimulus path is not an immutable file"
        )
    try:
        stat = stimulus_path.stat()
        digest = hashlib.sha256()
        with stimulus_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ActiveCommissioningAdmissionError(
            "admitted capture stimulus is no longer readable"
        ) from exc
    if (
        not stimulus_path.is_file()
        or stat.st_size != handoff.stimulus.artifact.byte_size
        or digest.hexdigest() != handoff.stimulus.artifact.sha256
    ):
        raise ActiveCommissioningAdmissionError(
            "admitted capture stimulus identity changed before persistence"
        )
    return handoff.to_dict()


def _target(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
) -> Mapping[str, Any]:
    matches = [
        target
        for target in active_driver_targets(topology)
        if target.get("speaker_group_id") == speaker_group_id
        and target.get("role") == role
    ]
    if len(matches) != 1:
        raise ActiveCommissioningAdmissionError(
            "the requested driver target is not current"
        )
    return matches[0]


def _target_by_id(
    topology: OutputTopology,
    *,
    target_id: str,
) -> Mapping[str, Any]:
    matches = [
        target
        for target in active_driver_targets(topology)
        if target.get("target_id") == target_id
    ]
    if len(matches) != 1:
        raise ActiveCommissioningAdmissionError(
            "the prepared driver target is not current"
        )
    return matches[0]


def _profile_target(
    safety_profile: Mapping[str, Any], target_fingerprint: str
) -> Mapping[str, Any]:
    targets = safety_profile.get("targets")
    matches = [
        target
        for target in (targets if isinstance(targets, list) else [])
        if isinstance(target, Mapping)
        and target.get("target_fingerprint") == target_fingerprint
    ]
    if len(matches) != 1:
        raise ActiveCommissioningAdmissionError(
            "the confirmed driver safety profile does not contain this target"
        )
    return matches[0]


def _context_fingerprint(
    *,
    topology: OutputTopology,
    comparison_set: Mapping[str, Any],
    applied_profile: Mapping[str, Any],
    target: Mapping[str, Any],
    expected_main_volume_db: float,
    expected_graph_fingerprint: str,
) -> str:
    return json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_driver_capture_context",
            "topology_id": topology.topology_id,
            "comparison_set_id": comparison_set.get("comparison_set_id"),
            "comparison_set_fingerprint": comparison_set.get("fingerprint"),
            "commissioning_session_id": comparison_set.get("bundle_session_id"),
            "applied_profile_fingerprint": json_fingerprint(dict(applied_profile)),
            "target_id": target.get("target_id"),
            "target_fingerprint": target.get("target_fingerprint"),
            "expected_main_volume_db": expected_main_volume_db,
            "expected_graph_fingerprint": expected_graph_fingerprint,
        }
    )


def running_graph_fingerprint(running_config_raw: str | None) -> str:
    """Fingerprint one parseable fresh CamillaDSP readback without repairing it."""

    try:
        parsed = yaml.safe_load(running_config_raw or "")
    except yaml.YAMLError as exc:
        raise ActiveCommissioningAdmissionError(
            "running CamillaDSP graph is not parseable"
        ) from exc
    if not isinstance(parsed, dict):
        raise ActiveCommissioningAdmissionError(
            "running CamillaDSP graph is not an object"
        )
    return json_fingerprint(parsed)


def prepare_capture_plan(
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    comparison_set: Mapping[str, Any],
    applied_profile: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
    commissioning_gain_db: float,
    expected_main_volume_db: float,
    expected_graph_fingerprint: str,
) -> tuple[PreparedDriverExcitationPlan, SweepMeta]:
    """Build one role-bounded, realized one-shot sweep plan."""

    if not comparison_set_valid(comparison_set):
        raise ActiveCommissioningAdmissionError("comparison set is missing or stale")
    session_id = comparison_set.get("bundle_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ActiveCommissioningAdmissionError(
            "comparison set predates production excitation admission"
        )
    if (
        not isinstance(expected_graph_fingerprint, str)
        or len(expected_graph_fingerprint) != 64
    ):
        raise ActiveCommissioningAdmissionError(
            "expected graph fingerprint is missing"
        )
    for name, value in (
        ("commissioning_gain_db", commissioning_gain_db),
        ("expected_main_volume_db", expected_main_volume_db),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) > 0.0
        ):
            raise ActiveCommissioningAdmissionError(
                f"{name} must be finite and non-positive"
            )
    target = _target(
        topology, speaker_group_id=speaker_group_id, role=role
    )
    profile_target = _profile_target(
        safety_profile, str(target.get("target_fingerprint") or "")
    )
    hard_band = profile_target.get("hard_excitation_band_hz")
    measurement_band = profile_target.get("measurement_band_hz")
    level_limits = profile_target.get("level_duration_limits")
    if (
        not isinstance(hard_band, list)
        or len(hard_band) != 2
        or not isinstance(measurement_band, list)
        or len(measurement_band) != 2
        or not isinstance(level_limits, Mapping)
    ):
        raise ActiveCommissioningAdmissionError(
            "confirmed driver safety limits are incomplete"
        )
    f1 = max(
        MIN_DRIVER_TEST_FREQUENCY_HZ,
        float(hard_band[0]),
        float(measurement_band[0]),
    )
    f2 = min(
        MAX_DRIVER_TEST_FREQUENCY_HZ,
        float(hard_band[1]),
        float(measurement_band[1]),
        DEFAULT_SAMPLE_RATE / 2.0 - 1.0,
    )
    duration_limit = min(
        float(level_limits["max_sweep_duration_s"]),
        driver_sweep_duration_s(role),
    )
    duration_approx = duration_limit
    while True:
        meta = synchronized_sweep_metadata(
            f1=f1,
            f2=f2,
            duration_approx_s=duration_approx,
            sample_rate=DEFAULT_SAMPLE_RATE,
            amplitude_dbfs=ACTIVE_DRIVER_CAPTURE_SOURCE_DBFS,
        )
        if meta.duration_s <= duration_limit + 1e-9:
            break
        duration_approx -= 1.0 / f1
        if duration_approx <= 0.0:
            raise ActiveCommissioningAdmissionError(
                "driver sweep cannot fit the confirmed duration limit"
            )
    requested = RequestedDriverExcitationPlan(
        target_fingerprint=str(target["target_fingerprint"]),
        commissioning_context_fingerprint=_context_fingerprint(
            topology=topology,
            comparison_set=comparison_set,
            applied_profile=applied_profile,
            target=target,
            expected_main_volume_db=float(expected_main_volume_db),
            expected_graph_fingerprint=expected_graph_fingerprint,
        ),
        generator=DriverSweepGeneratorPlan(
            f1_hz=meta.f1,
            f2_hz=meta.f2,
            amplitude=10.0 ** (meta.amplitude_dbfs / 20.0),
            duration_s=meta.duration_s,
            repeat_count=ACTIVE_DRIVER_CAPTURE_REPEAT_COUNT,
            commissioning_gain_db=float(commissioning_gain_db),
            main_volume_db=float(expected_main_volume_db),
        ),
    )
    prepared = prepare_driver_excitation_plan(topology, safety_profile, requested)
    if prepared.minimum_cooldown_s > MAX_AUTOMATIC_DRIVER_COOLDOWN_S:
        raise ActiveCommissioningAdmissionError(
            "minimum driver cooldown exceeds the bounded automatic wait"
        )
    return prepared, meta


def _filter_requirement_passed(
    view: gs.GraphView,
    *,
    output_index: int,
    requirement: Mapping[str, Any],
) -> bool:
    kind = str(requirement.get("kind") or "")
    expected_type = {
        "highpass": "LinkwitzRileyHighpass",
        "lowpass": "LinkwitzRileyLowpass",
    }.get(kind)
    if expected_type is None:
        return False
    cutoff = requirement.get("cutoff_hz")
    slope = requirement.get("minimum_slope_db_per_octave")
    if (
        isinstance(cutoff, bool)
        or not isinstance(cutoff, (int, float))
        or isinstance(slope, bool)
        or not isinstance(slope, (int, float))
        or requirement.get("family_or_equivalent") != "equivalent_or_steeper"
    ):
        return False
    for step in view.pipeline_steps:
        if step.channels != frozenset({output_index}):
            continue
        for name in step.names:
            definition = view.filters.get(name)
            if definition is None or definition.type != "BiquadCombo":
                continue
            if definition.params.get("type") != expected_type:
                continue
            actual_cutoff = gs.float_value(definition.params.get("freq"))
            actual_order = gs.float_value(definition.params.get("order"))
            if actual_cutoff is None or actual_order is None:
                continue
            cutoff_ok = (
                actual_cutoff >= float(cutoff)
                if kind == "highpass"
                else actual_cutoff <= float(cutoff)
            )
            if cutoff_ok and actual_order * 6.0 >= float(slope):
                return True
    return False


def _main_volume_matches(observed: float | None, expected: float) -> bool:
    return bool(
        observed is not None
        and math.isfinite(float(observed))
        and abs(float(observed) - expected) < 0.0001
    )


def issue_protection_evidence(
    *,
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    prepared: PreparedDriverExcitationPlan,
    load_payload: Mapping[str, Any],
    running_config_raw: str | None,
    observed_main_volume_db: float | None,
    expected_main_volume_db: float,
) -> tuple[ProtectionEvidence, Mapping[str, Any]]:
    """Reduce one fresh live readback to exact Active protection evidence."""

    try:
        parsed = yaml.safe_load(running_config_raw or "")
    except yaml.YAMLError:
        parsed = None
    parsed_graph = parsed if isinstance(parsed, dict) else {}
    view = gs.view_from_camilla_dict(parsed if isinstance(parsed, dict) else None)
    target = _target_by_id(topology, target_id=prepared.target_id)
    output_index_value = target.get("output_index")
    output_index = (
        output_index_value
        if isinstance(output_index_value, int) and not isinstance(output_index_value, bool)
        else None
    )
    profile_target = _profile_target(
        safety_profile, prepared.requested_plan.target_fingerprint
    )
    intent = load_payload.get("preflight")
    intent = intent.get("audible_evidence") if isinstance(intent, Mapping) else None
    intent = intent if isinstance(intent, Mapping) else {}
    live = running_commission_evidence(
        running_config_raw,
        audible_outputs=intent.get("audible_outputs", []),
        muted_outputs=intent.get("muted_outputs", []),
        tweeter_outputs=intent.get("tweeter_outputs", []),
        protective_hp_hz=intent.get("protective_highpass_hz"),
        tweeter_highpass_name=str(intent.get("tweeter_highpass_name") or ""),
        tweeter_highpass_order=int(intent.get("tweeter_highpass_order") or 4),
        expected_headroom_db=COMMISSIONING_HEADROOM_DB,
    )
    required_filters = profile_target.get("required_protection_filters")
    filter_requirements = [
        requirement
        for requirement in (
            required_filters if isinstance(required_filters, list) else []
        )
        if isinstance(requirement, Mapping)
    ]
    filter_checks = (
        [
            _filter_requirement_passed(
                view,
                output_index=output_index,
                requirement=requirement,
            )
            for requirement in filter_requirements
        ]
        if output_index is not None
        else []
    )
    limiter = view.filters.get(driver_limiter_name(prepared.target_role))
    limiter_clip = (
        gs.float_value(limiter.params.get("clip_limit"))
        if limiter is not None and limiter.type == "Limiter"
        else None
    )
    limiter_ok = bool(
        isinstance(output_index, int)
        and limiter_clip is not None
        and limiter_clip <= STARTUP_LIMITER_CLIP_LIMIT_DB
        and gs.pipeline_contains_chain(
            view,
            channels={output_index},
            required_names=(driver_limiter_name(prepared.target_role),),
        )
    )
    devices = parsed_graph.get("devices")
    devices = devices if isinstance(devices, Mapping) else {}
    capture = devices.get("capture")
    capture = capture if isinstance(capture, Mapping) else {}
    volume_limit = gs.float_value(devices.get("volume_limit"))
    volume_ok = _main_volume_matches(
        observed_main_volume_db, expected_main_volume_db
    )
    target_gain_name = (
        output_commission_mute_name(output_index)
        if output_index is not None
        else ""
    )
    target_gain_filter = view.filters.get(target_gain_name)
    observed_target_gain_db = (
        gs.float_value(target_gain_filter.params.get("gain"))
        if target_gain_filter is not None and target_gain_filter.type == "Gain"
        else None
    )
    target_gain_ok = bool(
        output_index is not None
        and gs.filter_param_matches(
            view,
            target_gain_name,
            filter_type="Gain",
            params={
                "gain": prepared.requested_plan.generator.commissioning_gain_db,
                "mute": False,
            },
        )
    )
    load_state = load_payload.get("load")
    load_target = load_state.get("target") if isinstance(load_state, Mapping) else None
    load_target = load_target if isinstance(load_target, Mapping) else {}
    checks = {
        "live_commission_graph": bool(live.get("passed")),
        "target_output_current": isinstance(output_index, int),
        "load_target_current": (
            load_target.get("speaker_group_id") == target.get("speaker_group_id")
            and load_target.get("role") == target.get("role")
        ),
        "required_filters_present": bool(filter_checks) and all(filter_checks),
        "target_limiter_present": limiter_ok,
        "capture_route_current": (
            devices.get("samplerate") == DEFAULT_SAMPLE_RATE
            and capture.get("device") == DEFAULT_CAPTURE_DEVICE
        ),
        "graph_volume_ceiling": (
            volume_limit is not None
            and volume_limit <= expected_main_volume_db + 0.0001
        ),
        "main_volume_current": volume_ok,
        "target_commissioning_gain_current": target_gain_ok,
        "profile_current": evaluate_driver_safety_profile(
            safety_profile, topology
        ).confirmed_and_current,
        "plan_executable": prepared.execution_allowed,
    }
    report = {
        "schema_version": 1,
        "kind": "jts_active_driver_live_protection_report",
        "target_id": prepared.target_id,
        "target_fingerprint": prepared.requested_plan.target_fingerprint,
        "observed_main_volume_db": observed_main_volume_db,
        "expected_main_volume_db": expected_main_volume_db,
        "observed_target_commissioning_gain_db": observed_target_gain_db,
        "expected_target_commissioning_gain_db": (
            prepared.requested_plan.generator.commissioning_gain_db
        ),
        "graph_fingerprint": json_fingerprint(parsed_graph),
        "filter_checks": filter_checks,
        "live": live,
        "checks": checks,
        "passed": all(checks.values()),
    }
    proof_fingerprint = json_fingerprint(report)
    limits = prepared.limits
    return (
        ProtectionEvidence(
            target_fingerprint=limits.target_fingerprint,
            safety_profile_fingerprint=limits.safety_profile_fingerprint,
            protection_requirement_fingerprint=(
                limits.protection_requirement_fingerprint
            ),
            authority_fingerprint=limits.fingerprint,
            excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
            evidence_fingerprint=proof_fingerprint,
            current=bool(report["passed"]),
        ),
        report,
    )


def _write_stimulus_once(
    authority_dir: Path,
    *,
    generation: GenerationAdmissionArtifact,
    meta: SweepMeta,
) -> GeneratedExcitationWav:
    relative_path = f"stimuli/{generation.admission_id}.wav"
    target = authority_dir / relative_path
    target.parent.mkdir(mode=0o750, exist_ok=True)
    signal, generated_meta = synchronized_swept_sine(
        f1=meta.f1,
        f2=meta.f2,
        duration_approx_s=meta.duration_s,
        sample_rate=meta.sample_rate,
        amplitude_dbfs=meta.amplitude_dbfs,
    )
    if generated_meta != meta:
        raise ActiveCommissioningAdmissionError(
            "generated sweep metadata changed after admission"
        )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".stimulus-", suffix=".wav", dir=target.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        write_sweep_wav(temporary, signal, meta.sample_rate)
        os.chmod(temporary, 0o640)
        os.link(temporary, target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise ActiveCommissioningAdmissionError(
            "one-shot stimulus path already exists"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    byte_size = 0
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
            byte_size += len(chunk)
    artifact = ArtifactIdentity(
        bundle_kind=generation.authority.bundle_kind,
        bundle_id=generation.authority.bundle_id,
        relative_path=relative_path,
        sha256=digest.hexdigest(),
        byte_size=byte_size,
    )
    return bind_generated_excitation_wav(generation, artifact)


async def play_admitted_driver_capture(
    *,
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    comparison_set: Mapping[str, Any],
    applied_profile: Mapping[str, Any],
    speaker_group_id: str,
    role: str,
    commissioning_gain_db: float,
    expected_main_volume_db: float,
    load_payload: Mapping[str, Any],
    read_running_config: Callable[[], Awaitable[str | None]],
    read_main_volume_db: Callable[[], Awaitable[float | None]],
    load_current_context: Callable[
        [],
        tuple[
            OutputTopology,
            Mapping[str, Any],
            Mapping[str, Any],
            Mapping[str, Any],
        ],
    ],
    alsa_device: str,
    timeout_s: float,
) -> ActiveDriverCapturePlayback:
    """Persist, independently re-admit, and play one exact driver WAV.

    The caller must hold the shared DSP writer lock for this entire await.
    Every call mints a new server-side identity; retries cannot reuse a consumed
    generation or playback admission.
    """

    initial_raw = await read_running_config()
    initial_graph_fingerprint = running_graph_fingerprint(initial_raw)
    prepared, meta = prepare_capture_plan(
        topology,
        safety_profile,
        comparison_set,
        applied_profile,
        speaker_group_id=speaker_group_id,
        role=role,
        commissioning_gain_db=commissioning_gain_db,
        expected_main_volume_db=expected_main_volume_db,
        expected_graph_fingerprint=initial_graph_fingerprint,
    )
    session_id = str(comparison_set.get("bundle_session_id") or "")
    from .bundles import sessions_dir

    authority = open_bundle_admission_authority(
        sessions_dir() / session_id,
        expected_session_id=session_id,
    )
    initial_volume = await read_main_volume_db()
    initial_evidence, _initial_report = issue_protection_evidence(
        topology=topology,
        safety_profile=safety_profile,
        prepared=prepared,
        load_payload=load_payload,
        running_config_raw=initial_raw,
        observed_main_volume_db=initial_volume,
        expected_main_volume_db=expected_main_volume_db,
    )
    decision = admit_excitation(
        prepared.request,
        prepared.limits,
        protection_evidence=initial_evidence,
    )
    if not decision.allowed:
        reasons = ",".join(reason.value for reason in decision.refusal_reasons)
        raise ActiveCommissioningAdmissionError(
            f"driver excitation generation refused: {reasons}"
        )
    admission_id = uuid.uuid4().hex
    generation = persist_generation_admission(
        authority,
        admission_id=admission_id,
        admission=decision,
    )
    stimulus = _write_stimulus_once(
        authority.directory,
        generation=generation,
        meta=meta,
    )

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        current_topology, current_profile, current_comparison, current_applied = (
            load_current_context()
        )
        current_raw = await read_running_config()
        current_prepared, current_meta = prepare_capture_plan(
            current_topology,
            current_profile,
            current_comparison,
            current_applied,
            speaker_group_id=speaker_group_id,
            role=role,
            commissioning_gain_db=commissioning_gain_db,
            expected_main_volume_db=expected_main_volume_db,
            expected_graph_fingerprint=running_graph_fingerprint(current_raw),
        )
        if current_meta != meta:
            raise ActiveCommissioningAdmissionError(
                "driver sweep plan changed before playback"
            )
        evidence, _report = issue_protection_evidence(
            topology=current_topology,
            safety_profile=current_profile,
            prepared=current_prepared,
            load_payload=load_payload,
            running_config_raw=current_raw,
            observed_main_volume_db=await read_main_volume_db(),
            expected_main_volume_db=expected_main_volume_db,
        )
        return CurrentPlaybackAdmissionInputs(
            limits=current_prepared.limits,
            protection_evidence=evidence,
        )

    if prepared.minimum_cooldown_s > 0.0:
        log_event(
            logger,
            "active_speaker.driver_capture_cooldown_started",
            admission_id=admission_id,
            target_id=prepared.target_id,
            cooldown_s=prepared.minimum_cooldown_s,
        )
        await asyncio.sleep(prepared.minimum_cooldown_s)

    admitted = await play_admitted_wav(
        authority.directory,
        stimulus=stimulus,
        authority=authority,
        generation=generation,
        issue_current_inputs=issue_current_inputs,
        alsa_device=alsa_device,
        timeout_s=timeout_s,
    )
    try:
        post_playback_volume = await read_main_volume_db()
    except asyncio.CancelledError as exc:
        raise ActiveCommissioningPlaybackDrift(
            "post-play volume verification was cancelled after admitted driver playback",
            reason="post_play_volume_verification_cancelled",
            admission_id=admission_id,
            playback_artifact=admitted.admission.artifact,
        ) from exc
    except (CamillaUnavailable, TypeError, ValueError) as exc:
        raise ActiveCommissioningPlaybackDrift(
            "main volume could not be verified after admitted driver playback",
            reason="post_play_volume_unverified",
            admission_id=admission_id,
            playback_artifact=admitted.admission.artifact,
        ) from exc
    if not _main_volume_matches(post_playback_volume, expected_main_volume_db):
        raise ActiveCommissioningPlaybackDrift(
            "main volume changed during admitted driver playback",
            reason="main_volume_drift",
            admission_id=admission_id,
            playback_artifact=admitted.admission.artifact,
        )
    playback_evidence = admitted.admission.admission.protection_evidence
    if playback_evidence is None or playback_evidence.evidence_fingerprint is None:
        raise ActiveCommissioningAdmissionError(
            "playback admission omitted its fresh protection evidence"
        )
    handoff = ActiveCaptureAdmissionHandoff(
        session_id=session_id,
        comparison_set_id=str(comparison_set["comparison_set_id"]),
        comparison_set_fingerprint=str(comparison_set["fingerprint"]),
        admission_id=admission_id,
        target_id=prepared.target_id,
        target_fingerprint=prepared.requested_plan.target_fingerprint,
        authority_fingerprint=authority.fingerprint,
        generation_artifact=generation.artifact,
        playback_artifact=admitted.admission.artifact,
        stimulus=stimulus,
        admission=admitted.admission.admission.to_dict(),
        graph_evidence_fingerprint=playback_evidence.evidence_fingerprint,
    )
    return ActiveDriverCapturePlayback(sweep_meta=meta, handoff=handoff)
