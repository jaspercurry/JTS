# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Concrete admitted producer for one typed summed-region capture.

The host owns the operation, authorities, graph, and safety policy.  The only
injected edge is a raw microphone transport which receives one bounded play
closure and returns WAV bytes plus non-authoritative diagnostics.  Admission,
playback re-admission, analysis, quality, and evidence identities are all built
inside this module; a transport cannot manufacture any of them.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol, TypeAlias, cast

from jasper.audio_measurement.admitted_playback import (
    AdmittedPlaybackResult,
    CurrentPlaybackAdmissionInputs,
    play_admitted_wav,
)
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.evidence_identity import (
    CaptureIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.excitation_admission import (
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.audio_measurement.excitation_artifacts import (
    persist_generation_admission,
)
from jasper.audio_measurement.null_walk import NullWalkSpec
from jasper.audio_measurement.playback import PlaybackResult
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.sweep import SweepMeta, synchronized_sweep_metadata
from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE
from jasper.output_topology import OutputTopology

from . import graph_safety as gs
from .baseline_profile import topology_config_fingerprint
from .commissioning_admission import (
    MAX_AUTOMATIC_DRIVER_COOLDOWN_S,
    persist_synchronized_stimulus_once,
)
from .commissioning_evidence import (
    ACTIVE_REGION_EVIDENCE_CONSUMER_ID,
    ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID,
    ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION,
    REFERENCE_AXIS_GEOMETRY_ID,
    AdmittedRegionCapture,
    CommissioningAttemptHandle,
    CommissioningEvidenceAuthority,
    EvidenceKind,
    RegionEvidenceTarget,
    capture_attempt_context_fingerprint,
    active_region_threshold_profile_fingerprint,
    delay_point_context_base_fingerprint,
    measurement_kind_for_evidence,
)
from .commissioning_evidence_store import CommissioningEvidenceStore
from .commissioning_runtime import (
    AdmittedCaptureCallbackResult,
    CommissioningFreshReadback,
    CommissioningLiveContext,
    PreparedSummedExcitation,
    prepare_summed_excitation,
)
from .commissioning_receipt import (
    POST_APPLY_CONSUMER_ID,
    POST_APPLY_MEASUREMENT_KIND,
    AdmittedCaptureProof,
)
from .driver_acoustics import (
    SUMMED_BLEND_OK,
    SUMMED_POLARITY_OR_DELAY_PROBLEM,
    SummedAcousticResult,
    analyze_summed_crossover,
)
from .driver_safety import evaluate_driver_safety_profile
from .graph_evidence import driver_baseline_gain_name
from .measurement import active_driver_targets
from .runtime_contract import classify_camilla_graph
from .runtime_contract import GRAPH_GUARDED_COMMISSIONING
from .test_signal_plan import (
    CROSSOVER_AMBIENT_DURATION_S,
    CROSSOVER_CAPTURE_PLAY_DEADLINE_S,
    CROSSOVER_CAPTURE_MAX_WAV_BYTES,
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    SUMMED_SWEEP_DURATION_S,
)

MAX_TRANSPORT_METADATA_BYTES = 16 * 1024
MAX_PLAYBACK_TIMEOUT_S = 120.0
_UUID_HEX_RE = re.compile(r"[0-9a-f]{32}")


class SummedCaptureProducerError(RuntimeError):
    """One typed operation could not produce authoritative capture evidence."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class RegionCaptureOperation(Protocol):
    """Structural view of the server-owned host operation."""

    @property
    def plan_fingerprint(self) -> str: ...

    @property
    def fingerprint(self) -> str: ...

    @property
    def issuance_id(self) -> str | None: ...

    @property
    def target(self) -> RegionEvidenceTarget: ...

    @property
    def attempt(self) -> CommissioningAttemptHandle: ...

    @property
    def evidence_kind(self) -> EvidenceKind: ...

    @property
    def placement_fingerprint(self) -> str: ...

    @property
    def driver_target_fingerprints(self) -> tuple[str, str]: ...

    @property
    def lower_channels(self) -> tuple[int, ...]: ...

    @property
    def upper_channels(self) -> tuple[int, ...]: ...

    @property
    def capture_ordinal(self) -> int: ...

    @property
    def relative_delay_us(self) -> float | None: ...

    @property
    def null_walk_spec(self) -> NullWalkSpec | None: ...

    @property
    def target_fingerprint(self) -> str: ...


class PostApplyCaptureOperation(RegionCaptureOperation, Protocol):
    @property
    def commissioning_context_fingerprint(self) -> str: ...


class RawCaptureResult:
    """Bounded WAV bytes and diagnostics which never grant authority."""

    __slots__ = ("wav_bytes", "_metadata_json")

    def __init__(self, wav_bytes: bytes, metadata: Mapping[str, Any]) -> None:
        if type(wav_bytes) is not bytes or not wav_bytes:
            raise SummedCaptureProducerError(
                "raw_capture_invalid", "raw capture WAV must be non-empty bytes"
            )
        if len(wav_bytes) > CROSSOVER_CAPTURE_MAX_WAV_BYTES:
            raise SummedCaptureProducerError(
                "raw_capture_too_large", "raw capture WAV exceeds the bounded limit"
            )
        if not isinstance(metadata, Mapping):
            raise SummedCaptureProducerError(
                "transport_metadata_invalid", "transport metadata must be an object"
            )
        try:
            raw = json.dumps(
                dict(metadata),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise SummedCaptureProducerError(
                "transport_metadata_invalid",
                "transport metadata must be finite canonical JSON",
            ) from exc
        if len(raw.encode("utf-8")) > MAX_TRANSPORT_METADATA_BYTES:
            raise SummedCaptureProducerError(
                "transport_metadata_too_large",
                "transport metadata exceeds the bounded limit",
            )
        self.wav_bytes = wav_bytes
        self._metadata_json = raw

    @property
    def metadata(self) -> dict[str, Any]:
        value = json.loads(self._metadata_json)
        assert isinstance(value, dict)
        return value


PlayOnce: TypeAlias = Callable[[], Awaitable[PlaybackResult]]
RawCaptureTransport: TypeAlias = Callable[
    [PlayOnce], Awaitable[RawCaptureResult]
]


@dataclass(frozen=True, slots=True)
class CurrentCaptureAuthority:
    """Fresh host-owned policy and calibration read at guarded boundaries."""

    safety_profile: Mapping[str, Any]
    calibration: CalibrationCurve


CurrentCaptureAuthorityLoader: TypeAlias = Callable[[], CurrentCaptureAuthority]


class _PlaybackGate:
    """Own exactly one admitted playback task even if transport misbehaves."""

    def __init__(self, play: Callable[[], Awaitable[AdmittedPlaybackResult]]) -> None:
        self._play = play
        self._expired = False
        self.task: asyncio.Task[PlaybackResult] | None = None
        self.admitted: AdmittedPlaybackResult | None = None

    def begin(self) -> Awaitable[PlaybackResult]:
        if self._expired:
            raise SummedCaptureProducerError(
                "transport_play_expired",
                "raw capture transport retained an expired play closure",
            )
        if self.task is not None:
            raise SummedCaptureProducerError(
                "transport_play_reused",
                "raw capture transport may invoke the play closure exactly once",
            )

        async def run() -> PlaybackResult:
            admitted = await self._play()
            self.admitted = admitted
            return admitted.playback

        self.task = asyncio.create_task(run())
        return self.task

    def expire(self) -> None:
        """Refuse future playback starts without cancelling an owned task."""

        self._expired = True


async def _cancel_and_drain(task: asyncio.Task[Any]) -> None:
    """Cancel one owned child and settle it despite repeated caller cancellation."""

    if not task.done():
        task.cancel()
    waiter = asyncio.create_task(asyncio.wait({task}))
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            continue
    waiter.result()
    if not task.cancelled():
        task.exception()


def _uuid_hex(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        raise SummedCaptureProducerError(
            "operation_invalid", f"{field_name} must be a lowercase UUID hex"
        )
    return value


def _canonical_mapping(value: Mapping[str, Any], *, field_name: str) -> str:
    try:
        raw = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise SummedCaptureProducerError(
            "authority_invalid", f"{field_name} must be finite canonical JSON"
        ) from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise SummedCaptureProducerError(
            "authority_invalid", f"{field_name} must be an object"
        )
    return raw


def _number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SummedCaptureProducerError(
            "graph_protection_invalid", f"{field_name} must be finite numeric"
        )
    result = float(value)
    if not math.isfinite(result):
        raise SummedCaptureProducerError(
            "graph_protection_invalid", f"{field_name} must be finite numeric"
        )
    return result


def _finite_json_evidence(value: Any) -> Any:
    """Represent analyzer non-finite sentinels without invalid JSON numbers."""

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _finite_json_evidence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json_evidence(item) for item in value]
    return value


def _profile_target(
    safety_profile: Mapping[str, Any], target_fingerprint: str
) -> Mapping[str, Any] | None:
    values = safety_profile.get("targets")
    if not isinstance(values, list):
        return None
    return next(
        (
            value
            for value in values
            if isinstance(value, Mapping)
            and value.get("target_fingerprint") == target_fingerprint
        ),
        None,
    )


def _role_gain_db(graph: Mapping[str, Any], role: str) -> float:
    view = gs.view_from_camilla_dict(dict(graph))
    definition = view.filters.get(driver_baseline_gain_name(role))
    gain = (
        gs.float_value(definition.params.get("gain"))
        if definition is not None and definition.type == "Gain"
        else None
    )
    if gain is None or gain > 0.0:
        raise SummedCaptureProducerError(
            "graph_protection_invalid",
            f"current {role} baseline gain is missing or positive",
        )
    return gain


def _bounded_sweep_meta(
    *, f1: float, f2: float, maximum_duration_s: float, amplitude_dbfs: float
) -> SweepMeta:
    duration_approx = min(SUMMED_SWEEP_DURATION_S, maximum_duration_s)
    while duration_approx > 0.0:
        meta = synchronized_sweep_metadata(
            f1=f1,
            f2=f2,
            duration_approx_s=duration_approx,
            sample_rate=DEFAULT_SAMPLE_RATE,
            amplitude_dbfs=amplitude_dbfs,
        )
        if meta.duration_s <= maximum_duration_s + 1e-9:
            return meta
        duration_approx -= 1.0 / f1
    raise SummedCaptureProducerError(
        "excitation_plan_invalid", "summed sweep cannot fit the confirmed duration"
    )


class SummedCaptureProducer:
    """Host-bound producer whose only injected behavior is raw capture transport."""

    __slots__ = (
        "authority",
        "plan_fingerprint",
        "topology",
        "evidence_store",
        "raw_transport",
        "alsa_device",
        "playback_timeout_s",
        "load_current_authority",
        "_safety_profile_json",
        "_calibration_fingerprint",
    )

    def __init__(
        self,
        *,
        authority: CommissioningEvidenceAuthority,
        plan_fingerprint: str,
        topology: OutputTopology,
        evidence_store: CommissioningEvidenceStore,
        load_current_authority: CurrentCaptureAuthorityLoader,
        raw_transport: RawCaptureTransport,
        alsa_device: str,
        playback_timeout_s: float,
    ) -> None:
        if not isinstance(authority, CommissioningEvidenceAuthority):
            raise SummedCaptureProducerError(
                "authority_invalid", "authority must be commissioning evidence"
            )
        if not isinstance(topology, OutputTopology):
            raise SummedCaptureProducerError(
                "authority_invalid", "topology must be OutputTopology"
            )
        if not isinstance(evidence_store, CommissioningEvidenceStore):
            raise SummedCaptureProducerError(
                "authority_invalid", "evidence_store must be strict"
            )
        if (
            evidence_store.session_id != authority.commissioning_session_id
            or authority.topology_id != topology.topology_id
            or authority.topology_fingerprint != topology_config_fingerprint(topology)
        ):
            raise SummedCaptureProducerError(
                "authority_invalid", "producer authorities do not describe one session"
            )
        if (
            not isinstance(plan_fingerprint, str)
            or len(plan_fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in plan_fingerprint)
        ):
            raise SummedCaptureProducerError(
                "authority_invalid", "plan_fingerprint must be a lowercase SHA-256"
            )
        if not callable(load_current_authority):
            raise SummedCaptureProducerError(
                "authority_invalid", "current capture authority loader is required"
            )
        initial_authority = load_current_authority()
        if not isinstance(initial_authority, CurrentCaptureAuthority):
            raise SummedCaptureProducerError(
                "authority_invalid", "current capture authority has an invalid shape"
            )
        safety_json = _canonical_mapping(
            initial_authority.safety_profile, field_name="safety_profile"
        )
        frozen_safety = json.loads(safety_json)
        assert isinstance(frozen_safety, dict)
        evaluation = evaluate_driver_safety_profile(frozen_safety, topology)
        if (
            not evaluation.confirmed_and_current
            or evaluation.profile_fingerprint
            != authority.protected_safety_profile_fingerprint
        ):
            raise SummedCaptureProducerError(
                "authority_invalid", "safety profile is not the exact run authority"
            )
        if (
            authority.threshold_profile_fingerprint
            != active_region_threshold_profile_fingerprint()
        ):
            raise SummedCaptureProducerError(
                "threshold_profile_stale",
                "run threshold profile is not the current code-owned model",
            )
        if not isinstance(initial_authority.calibration, CalibrationCurve):
            raise SummedCaptureProducerError(
                "authority_invalid", "calibration must be a server-owned curve"
            )
        calibration_snapshot = CalibrationCurve.from_dict(
            initial_authority.calibration.to_dict()
        )
        if not callable(raw_transport):
            raise SummedCaptureProducerError(
                "transport_invalid", "raw capture transport must be callable"
            )
        device = alsa_device.strip() if isinstance(alsa_device, str) else ""
        timeout = _number(playback_timeout_s, field_name="playback_timeout_s")
        if not device or timeout <= 0.0 or timeout > MAX_PLAYBACK_TIMEOUT_S:
            raise SummedCaptureProducerError(
                "playback_control_invalid", "playback control is outside its bound"
            )
        self.authority = authority
        self.plan_fingerprint = plan_fingerprint
        self.topology = topology
        self.evidence_store = evidence_store
        self.raw_transport = raw_transport
        self.alsa_device = device
        self.playback_timeout_s = timeout
        self.load_current_authority = load_current_authority
        self._safety_profile_json = safety_json
        self._calibration_fingerprint = json_fingerprint(
            {"schema_version": 1, "curve": calibration_snapshot.to_dict()}
        )

    @property
    def safety_profile(self) -> dict[str, Any]:
        value = json.loads(self._safety_profile_json)
        assert isinstance(value, dict)
        return value

    def _fresh_authority(self) -> CurrentCaptureAuthority:
        snapshot = self.load_current_authority()
        if not isinstance(snapshot, CurrentCaptureAuthority):
            raise SummedCaptureProducerError(
                "authority_stale", "current capture authority has an invalid shape"
            )
        safety_json = _canonical_mapping(
            snapshot.safety_profile, field_name="safety_profile"
        )
        if safety_json != self._safety_profile_json:
            raise SummedCaptureProducerError(
                "authority_stale", "driver safety profile changed during the run"
            )
        evaluation = evaluate_driver_safety_profile(snapshot.safety_profile, self.topology)
        if (
            not evaluation.confirmed_and_current
            or evaluation.profile_fingerprint
            != self.authority.protected_safety_profile_fingerprint
        ):
            raise SummedCaptureProducerError(
                "authority_stale", "driver safety profile is no longer current"
            )
        if not isinstance(snapshot.calibration, CalibrationCurve):
            raise SummedCaptureProducerError(
                "authority_stale", "calibration is no longer available"
            )
        calibration = CalibrationCurve.from_dict(snapshot.calibration.to_dict())
        fingerprint = json_fingerprint(
            {"schema_version": 1, "curve": calibration.to_dict()}
        )
        if fingerprint != self._calibration_fingerprint:
            raise SummedCaptureProducerError(
                "authority_stale", "microphone calibration changed during the run"
            )
        return CurrentCaptureAuthority(
            safety_profile=self.safety_profile,
            calibration=calibration,
        )

    def callback_for(
        self, operation: RegionCaptureOperation
    ) -> Callable[
        [CommissioningLiveContext],
        Awaitable[AdmittedCaptureCallbackResult[AdmittedRegionCapture]],
    ]:
        """Return the sole admitted callback for one server-issued operation."""

        async def callback(
            context: CommissioningLiveContext,
        ) -> AdmittedCaptureCallbackResult[AdmittedRegionCapture]:
            return await self.capture(operation, context)

        return callback

    def _prepare_sweep(
        self,
        operation: RegionCaptureOperation,
        context: CommissioningFreshReadback | CommissioningLiveContext,
    ) -> tuple[PreparedSummedExcitation, SweepMeta]:
        fc = float(operation.target.electrical_fc_hz)
        f1 = max(MIN_DRIVER_TEST_FREQUENCY_HZ, fc / 2.0)
        f2 = min(MAX_DRIVER_TEST_FREQUENCY_HZ, fc * 2.0)
        if not 0.0 < f1 < f2 < DEFAULT_SAMPLE_RATE / 2.0:
            raise SummedCaptureProducerError(
                "excitation_plan_invalid", "crossover shoulder band is not measurable"
            )
        gains = {
            operation.target.lower_role: _role_gain_db(
                context.graph.normalized_active_raw, operation.target.lower_role
            ),
            operation.target.upper_role: _role_gain_db(
                context.graph.normalized_active_raw, operation.target.upper_role
            ),
        }
        listening_volume_db = _number(
            context.listening_volume_db, field_name="listening_volume_db"
        )
        path_gain = max(gains.values()) + listening_volume_db
        discovery_fingerprint = json_fingerprint(
            {
                "schema_version": 1,
                "kind": "jts_active_summed_excitation_discovery",
                "operation_fingerprint": operation.fingerprint,
                "graph_fingerprint": context.graph.active_raw_fingerprint,
            }
        )
        discovery = prepare_summed_excitation(
            self.topology,
            self.safety_profile,
            target_fingerprints=operation.driver_target_fingerprints,
            evidence_target_fingerprint=operation.attempt.target_fingerprint,
            band=FrequencyBand(f1, f2),
            effective_peak_dbfs=-120.0,
            duration_s=0.001,
            excitation_plan_fingerprint=discovery_fingerprint,
        )
        source_peak = min(
            AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
            discovery.limits.maximum_effective_peak_dbfs - path_gain,
        )
        if not math.isfinite(source_peak) or source_peak > 0.0:
            raise SummedCaptureProducerError(
                "excitation_plan_invalid", "safe summed source level is unavailable"
            )
        meta = _bounded_sweep_meta(
            f1=f1,
            f2=f2,
            maximum_duration_s=discovery.limits.maximum_duration_s,
            amplitude_dbfs=source_peak,
        )
        effective_peak = source_peak + path_gain
        plan_fingerprint = json_fingerprint(
            {
                "schema_version": 1,
                "kind": "jts_active_summed_excitation_plan",
                "operation_fingerprint": operation.fingerprint,
                "issuance_id": operation.issuance_id,
                "target_fingerprints": list(operation.driver_target_fingerprints),
                "evidence_target_fingerprint": operation.attempt.target_fingerprint,
                "graph_fingerprint": context.graph.active_raw_fingerprint,
                "listening_volume_db": listening_volume_db,
                "role_gains_db": gains,
                "source_peak_dbfs": source_peak,
                "effective_peak_dbfs": effective_peak,
                "sweep_meta": meta.to_dict(),
            }
        )
        prepared = prepare_summed_excitation(
            self.topology,
            self.safety_profile,
            target_fingerprints=operation.driver_target_fingerprints,
            evidence_target_fingerprint=operation.attempt.target_fingerprint,
            band=FrequencyBand(meta.f1, meta.f2),
            effective_peak_dbfs=effective_peak,
            duration_s=meta.duration_s,
            excitation_plan_fingerprint=plan_fingerprint,
        )
        if prepared.minimum_cooldown_s > MAX_AUTOMATIC_DRIVER_COOLDOWN_S:
            raise SummedCaptureProducerError(
                "cooldown_unbounded", "summed cooldown exceeds the automatic bound"
            )
        return prepared, meta

    def _protection_evidence(
        self,
        operation: RegionCaptureOperation,
        prepared: PreparedSummedExcitation,
        readback: CommissioningFreshReadback | CommissioningLiveContext,
        *,
        expected_graph_fingerprint: str,
        expected_volume_db: float,
        boundary: str,
        post_apply: bool = False,
    ) -> ProtectionEvidence:
        current_targets = {
            value["target_fingerprint"]: value
            for value in active_driver_targets(self.topology)
        }
        channels_by_role: dict[str, set[int]] = {}
        for value in current_targets.values():
            role = value.get("role")
            output_index = value.get("output_index")
            if isinstance(role, str) and isinstance(output_index, int) and not isinstance(
                output_index, bool
            ):
                channels_by_role.setdefault(role, set()).add(output_index)
        view = gs.view_from_camilla_dict(readback.graph.normalized_active_raw)
        requirement_checks: list[dict[str, Any]] = []
        for target_fingerprint in operation.driver_target_fingerprints:
            target = current_targets.get(target_fingerprint)
            profile_target = _profile_target(self.safety_profile, target_fingerprint)
            output_index = target.get("output_index") if target is not None else None
            role = target.get("role") if target is not None else None
            requirements = (
                profile_target.get("required_protection_filters")
                if profile_target is not None
                else None
            )
            requirement_values = (
                requirements if isinstance(requirements, list) else []
            )
            filter_results = [
                gs.protection_requirement_present(
                    view,
                    output_index=output_index,
                    allowed_channels=channels_by_role.get(
                        role if isinstance(role, str) else "", set()
                    ),
                    requirement=dict(requirement),
                )
                for requirement in requirement_values
                if isinstance(output_index, int)
                and not isinstance(output_index, bool)
                and isinstance(requirement, Mapping)
            ]
            requirement_checks.append(
                {
                    "target_fingerprint": target_fingerprint,
                    "output_index": output_index,
                    "checks": filter_results,
                    "passed": bool(filter_results) and all(filter_results),
                }
            )
        graph_safety = classify_camilla_graph(
            topology=self.topology, text=readback.active_raw
        )
        devices = readback.graph.normalized_active_raw.get("devices")
        volume_limit = (
            gs.float_value(devices.get("volume_limit"))
            if isinstance(devices, Mapping)
            else None
        )
        evaluation = evaluate_driver_safety_profile(
            self.safety_profile, self.topology
        )
        delay_current = (
            readback.delay_confirmation is not None
            if operation.evidence_kind == "delay_null"
            else readback.delay_confirmation is None
        )
        graph_details = graph_safety.details
        graph_target_current = (
            graph_safety.allowed
            if post_apply
            else bool(
                graph_details.get("baseline_commissioning_candidate") is True
                and graph_details.get("baseline_commissioning_group")
                == operation.target.speaker_group_id
                and set(graph_details.get("baseline_commissioning_roles") or ())
                == {operation.target.lower_role, operation.target.upper_role}
                and graph_details.get("unmuted_outputs")
                == sorted(operation.lower_channels + operation.upper_channels)
            )
        )
        current_checks = {
            "graph_exact": (
                readback.graph.active_raw_fingerprint == expected_graph_fingerprint
            ),
            "graph_guarded_commissioning": (
                graph_safety.allowed
                and (
                    post_apply
                    or graph_safety.classification == GRAPH_GUARDED_COMMISSIONING
                )
            ),
            "graph_target_current": graph_target_current,
            "listening_volume_exact": math.isclose(
                readback.listening_volume_db,
                expected_volume_db,
                rel_tol=0.0,
                abs_tol=1e-6,
            ),
            "graph_volume_ceiling": (
                volume_limit is not None
                and (
                    (post_apply and volume_limit <= 0.0)
                    or volume_limit <= expected_volume_db + 0.0001
                )
            ),
            "profile_current": (
                evaluation.confirmed_and_current
                and evaluation.profile_fingerprint
                == self.authority.protected_safety_profile_fingerprint
            ),
            "requirements_current": bool(requirement_checks)
            and all(item["passed"] for item in requirement_checks),
            "delay_confirmation_current": delay_current,
        }
        report = {
            "schema_version": 1,
            "kind": "jts_active_summed_live_protection_report",
            "boundary": boundary,
            "operation_fingerprint": operation.fingerprint,
            "issuance_id": operation.issuance_id,
            "target_fingerprint": operation.attempt.target_fingerprint,
            "graph_fingerprint": readback.graph.active_raw_fingerprint,
            "listening_volume_db": readback.listening_volume_db,
            "graph_classification": graph_safety.classification,
            "requirement_checks": requirement_checks,
            "checks": current_checks,
            "passed": all(current_checks.values()),
        }
        limits = prepared.limits
        return ProtectionEvidence(
            target_fingerprint=limits.target_fingerprint,
            safety_profile_fingerprint=limits.safety_profile_fingerprint,
            protection_requirement_fingerprint=(
                limits.protection_requirement_fingerprint
            ),
            authority_fingerprint=limits.fingerprint,
            excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
            evidence_fingerprint=json_fingerprint(report),
            current=bool(report["passed"]),
        )

    def _quality_issues(
        self,
        operation: RegionCaptureOperation,
        result: SummedAcousticResult,
    ) -> list[str]:
        issues: list[str] = []
        quality = result.quality
        gating = result.gating if isinstance(result.gating, Mapping) else {}
        snr = result.snr if isinstance(result.snr, Mapping) else {}
        expected_null = operation.evidence_kind in {"reverse", "delay_null"}
        if quality.get("failed") is not False:
            issues.append("capture_quality_failed")
        # A polarity/delay problem is a usable admitted measurement. The
        # post-apply host classifies three exact repeats; this recorder layer
        # rejects only captures that cannot support that decision.
        accepted_verdicts = {SUMMED_BLEND_OK, SUMMED_POLARITY_OR_DELAY_PROBLEM}
        if result.verdict not in accepted_verdicts:
            issues.append("summed_capture_unusable")
        if result.mic_clipping:
            issues.append("capture_clipped")
        if not result.calibrated:
            issues.append("calibrated_mic_required")
        if result.expect_null is not expected_null:
            issues.append("polarity_context_mismatch")
        if not math.isclose(
            result.crossover_fc_hz,
            operation.target.electrical_fc_hz,
            rel_tol=1e-6,
            abs_tol=1e-3,
        ):
            issues.append("crossover_region_mismatch")
        if gating.get("applied") is not True:
            issues.append("gated_capture_required")
        if result.above_validity_floor is not True:
            issues.append("below_validity_floor")
        if snr.get("decision_class") != "alignment" or snr.get("verdict") != "ok":
            issues.append("alignment_snr_insufficient")
        if result.null_depth_capped:
            issues.append("null_depth_capped")
        if not math.isfinite(result.null_depth_db) or result.null_depth_db < 0.0:
            issues.append("null_depth_invalid")
        return issues

    async def capture(
        self,
        operation: RegionCaptureOperation,
        context: CommissioningLiveContext,
    ) -> AdmittedCaptureCallbackResult[AdmittedRegionCapture]:
        """Admit, play, persist, analyze, and bind one exact operation."""

        result = await self._capture(operation, context, post_apply=False)
        if not isinstance(result.payload, AdmittedRegionCapture):
            raise SummedCaptureProducerError(
                "capture_kind_mismatch", "region capture returned another proof kind"
            )
        return result

    async def capture_post_apply(
        self,
        operation: PostApplyCaptureOperation,
        context: CommissioningLiveContext,
    ) -> AdmittedCaptureCallbackResult[AdmittedCaptureProof]:
        result = await self._capture(operation, context, post_apply=True)
        if not isinstance(result.payload, AdmittedCaptureProof):
            raise SummedCaptureProducerError(
                "capture_kind_mismatch",
                "post-apply capture returned another proof kind",
            )
        return result

    async def _capture(
        self,
        operation: RegionCaptureOperation | PostApplyCaptureOperation,
        context: CommissioningLiveContext,
        *,
        post_apply: bool,
    ) -> AdmittedCaptureCallbackResult[Any]:

        issuance_id = _uuid_hex(operation.issuance_id, field_name="issuance_id")
        if (
            operation.plan_fingerprint != self.plan_fingerprint
            or operation.attempt.run != self.authority.run
            or operation.target_fingerprint != operation.attempt.target_fingerprint
        ):
            raise SummedCaptureProducerError(
                "operation_invalid", "operation does not equal the producer authority"
            )
        self._fresh_authority()
        prepared, sweep_meta = self._prepare_sweep(operation, context)
        generation_proof = self._protection_evidence(
            operation,
            prepared,
            context,
            expected_graph_fingerprint=context.graph.active_raw_fingerprint,
            expected_volume_db=context.listening_volume_db,
            boundary="generation",
            post_apply=post_apply,
        )
        decision = admit_excitation(
            prepared.request,
            prepared.limits,
            protection_evidence=generation_proof,
        )
        if not decision.allowed:
            reasons = ",".join(item.value for item in decision.refusal_reasons)
            raise SummedCaptureProducerError(
                "generation_refused", f"summed generation admission refused: {reasons}"
            )
        admission_id = uuid.uuid4().hex
        generation = persist_generation_admission(
            self.evidence_store.admission_authority,
            admission_id=admission_id,
            admission=decision,
        )
        stimulus = persist_synchronized_stimulus_once(
            self.evidence_store.bundle_dir,
            generation=generation,
            meta=sweep_meta,
        )

        async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
            self._fresh_authority()
            fresh = await context.fresh_readback()
            proof = self._protection_evidence(
                operation,
                prepared,
                fresh,
                expected_graph_fingerprint=context.graph.active_raw_fingerprint,
                expected_volume_db=context.listening_volume_db,
                boundary="playback",
                post_apply=post_apply,
            )
            return CurrentPlaybackAdmissionInputs(
                limits=prepared.limits,
                protection_evidence=proof,
            )

        async def play() -> AdmittedPlaybackResult:
            if prepared.minimum_cooldown_s > 0.0:
                await asyncio.sleep(prepared.minimum_cooldown_s)
            return await play_admitted_wav(
                self.evidence_store.bundle_dir,
                stimulus=stimulus,
                authority=self.evidence_store.admission_authority,
                generation=generation,
                issue_current_inputs=issue_current_inputs,
                alsa_device=self.alsa_device,
                timeout_s=self.playback_timeout_s,
            )

        gate = _PlaybackGate(play)
        capture_completed = False

        async def capture_and_settle_playback() -> RawCaptureResult:
            try:
                raw_capture = await self.raw_transport(gate.begin)
            finally:
                gate.expire()
            if not isinstance(raw_capture, RawCaptureResult):
                raise SummedCaptureProducerError(
                    "raw_capture_invalid",
                    "transport returned an unsupported result",
                )
            if gate.task is None:
                raise SummedCaptureProducerError(
                    "transport_play_missing",
                    "transport did not invoke the play closure",
                )
            await gate.task
            return raw_capture

        capture_task = asyncio.create_task(capture_and_settle_playback())
        try:
            done, _pending = await asyncio.wait(
                {capture_task}, timeout=CROSSOVER_CAPTURE_PLAY_DEADLINE_S
            )
            if not done:
                await _cancel_and_drain(capture_task)
                raise SummedCaptureProducerError(
                    "capture_deadline_exceeded",
                    "summed raw capture exceeded the code-owned deadline",
                )
            raw_capture = capture_task.result()
            capture_completed = True
        finally:
            if not capture_task.done():
                await _cancel_and_drain(capture_task)
            if not capture_completed and gate.task is not None:
                await _cancel_and_drain(gate.task)
        admitted = gate.admitted
        if admitted is None:
            raise SummedCaptureProducerError(
                "playback_result_missing", "admitted playback did not settle exactly"
            )

        prefix = (
            f"post-apply/{operation.attempt.attempt_id}/{issuance_id}/"
            f"{operation.capture_ordinal:04d}"
            if post_apply
            else (
                f"captures/{operation.attempt.attempt_id}/{issuance_id}/"
                f"{operation.capture_ordinal:04d}"
            )
        )
        raw_artifact = self.evidence_store.publish_raw_artifact(
            f"{prefix}/raw.wav", raw_capture.wav_bytes
        )
        if self.evidence_store.reopen_artifact(raw_artifact) != raw_capture.wav_bytes:
            raise SummedCaptureProducerError(
                "raw_capture_readback_mismatch", "raw WAV changed on exact reopen"
            )
        raw_path = self.evidence_store.bundle_dir.joinpath(
            *PurePosixPath(raw_artifact.relative_path).parts
        )
        generation_proof_fingerprint = generation_proof.evidence_fingerprint
        playback_proof = admitted.admission.admission.protection_evidence
        if (
            generation_proof_fingerprint is None
            or playback_proof is None
            or playback_proof.evidence_fingerprint is None
        ):
            raise SummedCaptureProducerError(
                "admission_proof_missing", "admitted playback omitted exact proof"
            )
        post_apply_operation = (
            cast(PostApplyCaptureOperation, operation) if post_apply else None
        )
        if post_apply_operation is not None:
            context_base = post_apply_operation.commissioning_context_fingerprint
        elif operation.evidence_kind == "delay_null":
            if (
                operation.null_walk_spec is None
                or operation.relative_delay_us is None
            ):
                raise SummedCaptureProducerError(
                    "operation_invalid", "delay operation omitted its exact coordinate"
                )
            context_base = delay_point_context_base_fingerprint(
                operation.target,
                operation.null_walk_spec,
                operation.relative_delay_us,
                context.graph.active_raw_fingerprint,
            )
        else:
            context_base = operation.target.context_base_fingerprint_for(
                operation.evidence_kind
            )
        context_fingerprint = (
            post_apply_operation.commissioning_context_fingerprint
            if post_apply_operation is not None
            else capture_attempt_context_fingerprint(
                self.authority,
                attempt=operation.attempt,
                evidence_kind=operation.evidence_kind,
                target_fingerprint=operation.attempt.target_fingerprint,
                context_base_fingerprint=context_base,
                graph_fingerprint=context.graph.active_raw_fingerprint,
                generation_protection_evidence_fingerprint=(
                    generation_proof_fingerprint
                ),
                playback_protection_evidence_fingerprint=(
                    playback_proof.evidence_fingerprint
                ),
            )
        )
        analysis_authority = self._fresh_authority()
        acoustic = analyze_summed_crossover(
            raw_path,
            sweep_meta.to_dict(),
            crossover_fc_hz=operation.target.electrical_fc_hz,
            null_threshold_db=DRIVER.null_threshold_db,
            expect_null=operation.evidence_kind in {"reverse", "delay_null"},
            calibration=analysis_authority.calibration,
            capture_geometry=REFERENCE_AXIS_GEOMETRY_ID,
            ambient_duration_s=CROSSOVER_AMBIENT_DURATION_S,
        )
        quality_issues = self._quality_issues(operation, acoustic)
        calibration_payload = analysis_authority.calibration.to_dict()
        analysis_payload = {
            "schema_version": 1,
            "kind": "jts_active_summed_capture_analysis",
            "algorithm_id": ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID,
            "algorithm_version": ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION,
            "threshold_profile_fingerprint": (
                active_region_threshold_profile_fingerprint()
            ),
            "null_threshold_db": DRIVER.null_threshold_db,
            "operation_fingerprint": operation.fingerprint,
            "issuance_id": issuance_id,
            "target_fingerprint": operation.attempt.target_fingerprint,
            "context_fingerprint": context_fingerprint,
            "graph_fingerprint": context.graph.active_raw_fingerprint,
            "raw_artifact": raw_artifact.to_dict(),
            "stimulus": stimulus.to_dict(),
            "generation_artifact": generation.artifact.to_dict(),
            "playback_artifact": admitted.admission.artifact.to_dict(),
            "sweep_meta": sweep_meta.to_dict(),
            "calibration": {
                "fingerprint": json_fingerprint(
                    {"schema_version": 1, "curve": calibration_payload}
                ),
                "curve": calibration_payload,
            },
            "capture_geometry": REFERENCE_AXIS_GEOMETRY_ID,
            "ambient_duration_s": CROSSOVER_AMBIENT_DURATION_S,
            "transport_metadata": raw_capture.metadata,
            "acoustic": _finite_json_evidence(acoustic.to_dict()),
        }
        analysis_artifact = self.evidence_store.publish_json_artifact(
            f"{prefix}/analysis.json", analysis_payload
        )
        if self.evidence_store.reopen_json_artifact(analysis_artifact) != analysis_payload:
            raise SummedCaptureProducerError(
                "analysis_readback_mismatch", "analysis changed on exact reopen"
            )
        quality_payload: dict[str, Any] = {
            "schema_version": 1,
            "kind": "jts_active_summed_capture_quality",
            "algorithm_id": ACTIVE_REGION_SUMMED_ANALYZER_POLICY_ID,
            "algorithm_version": ACTIVE_REGION_SUMMED_ANALYZER_POLICY_VERSION,
            "threshold_profile_fingerprint": (
                active_region_threshold_profile_fingerprint()
            ),
            "operation_fingerprint": operation.fingerprint,
            "issuance_id": issuance_id,
            "raw_artifact_fingerprint": raw_artifact.fingerprint,
            "analysis_artifact_fingerprint": analysis_artifact.fingerprint,
            "accepted": not quality_issues,
            "issues": quality_issues,
            "quality": _finite_json_evidence(acoustic.quality),
        }
        quality_artifact = self.evidence_store.publish_json_artifact(
            f"{prefix}/quality.json", quality_payload
        )
        if self.evidence_store.reopen_json_artifact(quality_artifact) != quality_payload:
            raise SummedCaptureProducerError(
                "quality_readback_mismatch", "quality decision changed on exact reopen"
            )
        if quality_issues:
            raise SummedCaptureProducerError(
                "capture_quality_refused",
                "summed capture quality refused: "
                f"{','.join(quality_issues)} ({quality_artifact.fingerprint})",
            )
        capture_identity = CaptureIdentity(
            consumer_id=(
                POST_APPLY_CONSUMER_ID
                if post_apply
                else ACTIVE_REGION_EVIDENCE_CONSUMER_ID
            ),
            measurement_kind=(
                POST_APPLY_MEASUREMENT_KIND
                if post_apply
                else measurement_kind_for_evidence(operation.evidence_kind)
            ),
            capture_id=f"capture-{issuance_id}",
            raw_artifact=raw_artifact,
            analysis_input_artifact=analysis_artifact,
            target_fingerprint=operation.attempt.target_fingerprint,
            context_fingerprint=context_fingerprint,
            geometry_id=REFERENCE_AXIS_GEOMETRY_ID,
            placement_fingerprint=operation.placement_fingerprint,
            quality_artifact=quality_artifact,
            admission_artifact=admitted.admission.artifact,
        )
        if post_apply:
            proof = AdmittedCaptureProof(
                capture=capture_identity,
                commissioning_session_id=self.authority.commissioning_session_id,
                generation_admission=generation.admission,
                admission=admitted.admission.admission,
                generation_artifact=generation.artifact,
            )
            return AdmittedCaptureCallbackResult(
                generation=generation,
                playback=admitted.admission,
                stimulus=stimulus,
                protection_evidence=playback_proof,
                payload=proof,
            )

        capture = AdmittedRegionCapture(
            authority=self.authority,
            plan_fingerprint=self.plan_fingerprint,
            attempt=operation.attempt,
            speaker_group_id=operation.target.speaker_group_id,
            region_id=operation.target.region_id,
            evidence_kind=operation.evidence_kind,
            target_fingerprint=operation.attempt.target_fingerprint,
            context_base_fingerprint=context_base,
            context_fingerprint=context_fingerprint,
            placement_fingerprint=operation.placement_fingerprint,
            graph_fingerprint=context.graph.active_raw_fingerprint,
            generation_protection_evidence_fingerprint=(
                generation_proof_fingerprint
            ),
            playback_protection_evidence_fingerprint=(
                playback_proof.evidence_fingerprint
            ),
            admission_id=admission_id,
            capture=capture_identity,
            stimulus=stimulus,
            generation_artifact=generation.artifact,
            playback_artifact=admitted.admission.artifact,
            generation_admission=generation.admission,
            playback_admission=admitted.admission.admission,
        )
        return AdmittedCaptureCallbackResult(
            generation=generation,
            playback=admitted.admission,
            stimulus=stimulus,
            protection_evidence=playback_proof,
            payload=capture,
        )
