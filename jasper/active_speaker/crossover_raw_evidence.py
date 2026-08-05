# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen R15 RAW evidence construction and validation.

The capture and persistence owners remain outside this module. This adapter
projects accepted :class:`DriverResponse` observations onto the existing
bounded branch-chain basis, binds them to the session-owned protected-neutral
graph, and validates the complete version-one meaning before publication.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.branch_chain import CHAIN_GRID_HZ
from jasper.active_speaker.protected_neutral_graph import (
    PROTECTED_NEUTRAL_GRAPH_KIND,
    ROLE_ORDER,
    ProtectedNeutralGraphError,
    ProtectedNeutralGraphSpec,
    ProtectedNeutralSessionGraph,
)
from jasper.audio_measurement.analysis import resample_complex_response
from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.audio_measurement.program import KIND_SWEEP
from jasper.audio_measurement.transfer_composition import (
    RAW_EVIDENCE_KIND,
    LinearTransferSection,
    TotalTransferCompositionError,
    complex_fingerprint,
    compose_total_transfer,
    linkwitz_riley_response_complex,
    transfer_policy_payload,
)

RAW_EVIDENCE_SCHEMA_VERSION = 1
RAW_EVIDENCE_INVALID_REASON = "invalid_evidence_identity"
ANCHOR_POSE_ID = "anchor_00"
ANCHOR_REPEAT_GROUP_COUNT = 3


class RawEvidenceError(ValueError):
    """A raw evidence set is stale, mixed, malformed, or inconsistent."""

    def __init__(self, detail: str, *, reason: str = RAW_EVIDENCE_INVALID_REASON) -> None:
        super().__init__(detail)
        self.reason = reason


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _finite_float_list(values: np.ndarray) -> list[float]:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1 or not np.all(np.isfinite(array)):
        raise RawEvidenceError("raw evidence arrays must be finite and one-dimensional")
    return array.tolist()


def _complex_record(values: np.ndarray) -> dict[str, Any]:
    array = np.asarray(values, dtype=np.complex128)
    if array.ndim != 1 or not (
        np.all(np.isfinite(array.real)) and np.all(np.isfinite(array.imag))
    ):
        raise RawEvidenceError("complex raw evidence must be finite and one-dimensional")
    return {
        "real": array.real.astype(np.float64).tolist(),
        "imag": array.imag.astype(np.float64).tolist(),
        "fingerprint": complex_fingerprint(
            array, kind="jts_crossover_complex_response"
        ),
    }


def _complex_from_record(raw: Any, *, length: int) -> np.ndarray:
    if not isinstance(raw, Mapping) or set(raw) != {"real", "imag", "fingerprint"}:
        raise RawEvidenceError("complex response schema mismatch")
    real = raw["real"]
    imag = raw["imag"]
    if not isinstance(real, list) or not isinstance(imag, list):
        raise RawEvidenceError("complex response arrays are invalid")
    try:
        values = np.asarray(real, dtype=np.float64) + 1j * np.asarray(
            imag, dtype=np.float64
        )
    except (TypeError, ValueError) as exc:
        raise RawEvidenceError("complex response arrays are not numeric") from exc
    if (
        values.shape != (length,)
        or not np.all(np.isfinite(values.real))
        or not np.all(np.isfinite(values.imag))
        or raw["fingerprint"]
        != complex_fingerprint(values, kind="jts_crossover_complex_response")
    ):
        raise RawEvidenceError("complex response fingerprint or basis mismatch")
    return values


def _fingerprinted(kind: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    core = {"schema_version": 1, "kind": kind, **dict(fields)}
    return {**core, "fingerprint": json_fingerprint(core)}


def _validate_fingerprinted(
    raw: Any, *, fields: set[str], kind: str, label: str
) -> Mapping[str, Any]:
    expected = {"schema_version", "kind", "fingerprint", *fields}
    if (
        not isinstance(raw, Mapping)
        or set(raw) != expected
        or raw.get("schema_version") != 1
        or raw.get("kind") != kind
    ):
        raise RawEvidenceError(f"{label} schema mismatch")
    core = {key: value for key, value in raw.items() if key != "fingerprint"}
    if raw["fingerprint"] != json_fingerprint(core):
        raise RawEvidenceError(f"{label} fingerprint mismatch")
    return raw


def _graph_spec(graph: Any) -> ProtectedNeutralGraphSpec:
    fields = {
        "schema_version",
        "kind",
        "graph_id",
        "active_raw_sha256",
        "exact_dsp_state_identity",
        "canonical_spec",
        "canonical_spec_fingerprint",
        "fingerprint",
    }
    if (
        not isinstance(graph, Mapping)
        or set(graph) != fields
        or graph.get("schema_version") != 1
        or graph.get("kind") != PROTECTED_NEUTRAL_GRAPH_KIND
        or not _is_sha256(graph.get("active_raw_sha256"))
        or graph.get("graph_id")
        != f"protected-neutral-{str(graph.get('active_raw_sha256'))[:16]}"
    ):
        raise RawEvidenceError("measurement graph identity is invalid")
    graph_core = {key: value for key, value in graph.items() if key != "fingerprint"}
    if graph.get("fingerprint") != json_fingerprint(graph_core):
        raise RawEvidenceError("measurement graph fingerprint mismatch")
    try:
        exact = ExactDspStateIdentity.from_mapping(graph["exact_dsp_state_identity"])
        spec = ProtectedNeutralGraphSpec.from_dict(graph["canonical_spec"])
    except (EvidenceIdentityError, ProtectedNeutralGraphError, TypeError, ValueError) as exc:
        raise RawEvidenceError("measurement graph exact specification is invalid") from exc
    if (
        exact.state.get("active_raw_sha256") != graph["active_raw_sha256"]
        or not isinstance(exact.state.get("active_raw_bytes"), int)
        or exact.state["active_raw_bytes"] <= 0
        or graph["canonical_spec_fingerprint"] != spec.fingerprint
    ):
        raise RawEvidenceError("measurement graph exact state is mixed")
    return spec


def _role_identity(spec: ProtectedNeutralGraphSpec, role: str) -> dict[str, str]:
    item = spec.role(role)
    return {
        "role": role,
        "target_id": item.target_id,
        "target_fingerprint": item.target_fingerprint,
    }


def _linear_identity(
    *, kind: str, role: str, sections: Sequence[LinearTransferSection], sign: int
) -> dict[str, Any]:
    return _fingerprinted(
        kind,
        {
            "role": role,
            "sections": [section.to_dict() for section in sections],
            "polarity_sign": int(sign),
        },
    )


def _response_id(
    *, session_id: str, capture_id: str, role: str, segment_id: str
) -> str:
    core = f"{session_id}:{capture_id}:{role}:{segment_id}"
    return f"response:{hashlib.sha256(core.encode('utf-8')).hexdigest()}"


def _sweep_cycles(program: Any) -> list[dict[str, str]]:
    sweeps = [
        segment
        for segment in program.stimulus_segments()
        if segment.kind == KIND_SWEEP and segment.role in ROLE_ORDER
    ]
    if len(sweeps) % 2:
        raise RawEvidenceError("accepted sweep ledger contains an incomplete role cycle")
    cycles: list[dict[str, str]] = []
    for index in range(0, len(sweeps), 2):
        pair = sweeps[index : index + 2]
        if tuple(segment.role for segment in pair) != ROLE_ORDER:
            raise RawEvidenceError("accepted sweep ledger is not woofer/HF paired")
        cycles.append({str(segment.role): str(segment.segment_id) for segment in pair})
    if len(cycles) < ANCHOR_REPEAT_GROUP_COUNT:
        raise RawEvidenceError("anchor evidence requires at least three complete cycles")
    return cycles


def _responses_by_segment(primary: Any) -> dict[str, Any]:
    occurrences = (primary, *tuple(primary.repeat_responses))
    by_segment: dict[str, Any] = {}
    for response in occurrences:
        segment_id = str(getattr(response, "segment_id", "") or "")
        if not segment_id or segment_id in by_segment:
            raise RawEvidenceError("driver response lacks a unique source segment identity")
        by_segment[segment_id] = response
    return by_segment


def _snr_record(snr: Any) -> dict[str, Any]:
    if not isinstance(snr, Mapping):
        raise RawEvidenceError("accepted RAW driver response lacks SNR evidence")
    return _fingerprinted("jts_crossover_snr_evidence_v1", {"verdict": dict(snr)})


def _driver_record(
    primary: Any,
    *,
    session_id: str,
    capture_id: str,
    take_id: str,
    program: Any,
    cycles: Sequence[Mapping[str, str]],
    driver_identity: Mapping[str, str],
    protection: np.ndarray,
    configured: np.ndarray,
) -> tuple[dict[str, Any], dict[str, str]]:
    role = str(primary.role)
    source = _responses_by_segment(primary)
    ordered_segment_ids = [cycle[role] for cycle in cycles]
    if set(source) != set(ordered_segment_ids):
        raise RawEvidenceError(
            f"accepted {role} observations do not match the program timing ledger"
        )
    response_records: list[dict[str, Any]] = []
    response_ids: list[str] = []
    values_by_id: dict[str, np.ndarray] = {}
    for accepted_order, segment_id in enumerate(ordered_segment_ids):
        response = source[segment_id]
        try:
            values = resample_complex_response(
                np.asarray(response.freqs_hz, dtype=np.float64),
                np.asarray(response.complex_tf, dtype=np.complex128),
                CHAIN_GRID_HZ,
            )
        except ValueError as exc:
            raise RawEvidenceError("driver response cannot use the bounded evidence grid") from exc
        response_id = _response_id(
            session_id=session_id,
            capture_id=capture_id,
            role=role,
            segment_id=segment_id,
        )
        response_ids.append(response_id)
        values_by_id[response_id] = values
        response_records.append(
            {
                "response_id": response_id,
                "source_segment_id": segment_id,
                "accepted_order": accepted_order,
                "capture_id": capture_id,
                "take_id": take_id,
                "complex_response": _complex_record(values),
            }
        )

    selector_ids = response_ids[:ANCHOR_REPEAT_GROUP_COUNT]
    selector_matrix = np.stack([values_by_id[item] for item in selector_ids], axis=0)
    nominal = np.asarray(
        np.mean(selector_matrix, axis=0, dtype=np.complex128),
        dtype=np.complex128,
    )
    magnitudes = 20.0 * np.log10(np.maximum(np.abs(selector_matrix), 1e-12))
    uncertainty = np.std(magnitudes, axis=0, dtype=np.float64)
    sweep_segments = [program.segment(segment_id) for segment_id in ordered_segment_ids]
    trust_band = (
        min(float(segment.f1_hz) for segment in sweep_segments),
        max(float(segment.f2_hz) for segment in sweep_segments),
    )
    floor = getattr(primary, "validity_floor_hz", None)
    validity_floor: float | None = None
    if isinstance(floor, (int, float)) and not isinstance(floor, bool):
        parsed_floor = float(floor)
        if math.isfinite(parsed_floor) and parsed_floor > 0.0:
            validity_floor = parsed_floor
    valid = (CHAIN_GRID_HZ >= trust_band[0]) & (CHAIN_GRID_HZ <= trust_band[1])
    if validity_floor is not None:
        valid &= CHAIN_GRID_HZ >= validity_floor
    if not np.any(valid):
        raise RawEvidenceError("driver has no trustworthy bounded evidence bins")
    try:
        composition = compose_total_transfer(
            nominal, configured, protection, required_mask=valid
        )
    except (TotalTransferCompositionError, ValueError) as exc:
        raise RawEvidenceError(
            "configured-Fc total-transfer composition is inadmissible"
        ) from exc
    return (
        {
            "role": role,
            "driver_identity": dict(driver_identity),
            "capture_id": capture_id,
            "take_id": take_id,
            "ordered_response_ids": response_ids,
            "responses": response_records,
            "nominal_complex_response": _complex_record(nominal),
            "nominal_magnitude_db": _finite_float_list(
                20.0 * np.log10(np.maximum(np.abs(nominal), 1e-12))
            ),
            "valid_mask": valid.tolist(),
            "trust_band_hz": [trust_band[0], trust_band[1]],
            "validity_floor_hz": validity_floor,
            "snr_evidence": _snr_record(primary.snr),
            "repeat_uncertainty_db": _finite_float_list(uncertainty),
            "configured_fitter_input_S": _complex_record(
                composition.fitter_input
            ),
            "configured_composition_fingerprints": dict(composition.fingerprints),
        },
        {segment_id: response_id for segment_id, response_id in zip(ordered_segment_ids, response_ids)},
    )


def _timing_gain_ledger(
    *, analysis: Any, program: Any, gains: Mapping[str, float]
) -> dict[str, Any]:
    drift = analysis.drift
    alignment = analysis.alignment
    candidate = analysis.candidate
    if drift is None or alignment is None or candidate is None:
        raise RawEvidenceError("RAW anchor requires drift, alignment, and anchor frame")
    program_segments = {item.segment_id: item for item in program.segments}
    locations: list[dict[str, Any]] = []
    for item in analysis.locations:
        segment = program_segments.get(item.segment_id)
        if segment is None:
            raise RawEvidenceError("located timing references an unknown program segment")
        locations.append(
            {
                "segment_id": str(item.segment_id),
                "kind": str(segment.kind),
                "role": segment.role,
                "scheduled_start": int(item.scheduled_start),
                "located_start": int(item.located_start),
                "residual_samples": float(item.residual_samples),
                "confidence": float(item.confidence),
                "clipped": bool(item.clipped),
            }
        )
    per_role = dict(drift.per_role_epsilon_ppm)
    trim = dict(candidate.trim_db)
    if set(per_role) != set(ROLE_ORDER) or set(trim) != set(ROLE_ORDER):
        raise RawEvidenceError("timing ledger lacks both role identities")
    fields = {
        "located_timing_corrections": locations,
        "inter_driver_drift_correction": {
            "epsilon_ppm": float(drift.epsilon_ppm),
            "per_role_epsilon_ppm": {
                role: float(per_role[role]) for role in ROLE_ORDER
            },
        },
        "stimulus_gain_db_by_segment": {
            str(item.segment_id): float(item.gain_db)
            for item in program.stimulus_segments()
            if item.kind == KIND_SWEEP
        },
        "capture_gain_level_solve_db_by_role": {
            role: float(gains[role]) for role in ROLE_ORDER
        },
        "anchor_frame": {
            "trim_db": {role: float(trim[role]) for role in ROLE_ORDER},
            "delay_us": float(alignment.delay_us),
            "polarity": str(alignment.polarity),
        },
    }
    return _fingerprinted("jts_crossover_timing_gain_ledger_v1", fields)


def _linearity_proof(
    *,
    analysis: Any,
    program: Any,
    spec: ProtectedNeutralGraphSpec,
) -> dict[str, Any]:
    integrity = analysis.capture_integrity
    clipped = (
        list(integrity.clipped_segments)
        if integrity is not None
        else [item.segment_id for item in analysis.locations if item.clipped]
    )
    maximum_programmed_gain = max(
        float(segment.gain_db) for segment in program.stimulus_segments()
    )
    maximum_peak = spec.worst_case_limiter_input_peak_dbfs
    margin = spec.worst_case_limiter_margin_db
    if (
        analysis.linearity_ok is not True
        or clipped
        or maximum_programmed_gain > spec.stimulus_peak_ceiling_dbfs
        or margin <= 0.0
    ):
        raise RawEvidenceError(
            "accepted raw evidence must prove linear capture and limiter non-engagement"
        )
    return _fingerprinted(
        "jts_crossover_limiter_linearity_proof_v1",
        {
            "proof_basis": "emitted_graph_worst_case_at_volume_ceiling",
            "limiter_threshold_dbfs": spec.limiter_threshold_dbfs,
            "stimulus_gain_ceiling_dbfs": spec.stimulus_peak_ceiling_dbfs,
            "volume_ceiling_db": spec.volume_ceiling_db,
            "graph_headroom_gain_db": spec.graph_headroom_gain_db,
            "maximum_programmed_stimulus_gain_dbfs": maximum_programmed_gain,
            "maximum_effective_stimulus_peak_dbfs": maximum_peak,
            "threshold_margin_db": margin,
            "boundary_rule": "stimulus_peak_must_be_strictly_below_threshold",
            "limiter_engaged": False,
            "capture_linearity_passed": True,
            "clipped_segments": clipped,
        },
    )


def build_raw_evidence_v1(
    *,
    session_id: str,
    analysis: Any,
    program: Any,
    capture_wav: bytes,
    attempt: int,
    session_graph: ProtectedNeutralSessionGraph,
    configured_fc_hz: float,
    component_safety_profile: Mapping[str, Any],
    calibration_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Build and immediately validate one anchor-only v1 RAW evidence set."""

    if not isinstance(session_id, str) or not session_id:
        raise RawEvidenceError("raw evidence requires a session identity")
    if type(attempt) is not int or attempt < 1:
        raise RawEvidenceError("raw evidence attempt must be a positive integer")
    if not isinstance(capture_wav, (bytes, bytearray)) or not capture_wav:
        raise RawEvidenceError("raw evidence requires the exact capture WAV bytes")
    if not isinstance(session_graph, ProtectedNeutralSessionGraph):
        raise RawEvidenceError("raw evidence requires the session-owned graph object")
    spec = _graph_spec(session_graph.identity)
    if spec.to_dict() != session_graph.spec.to_dict():
        raise RawEvidenceError("session graph object and emitted identity are mixed")
    if component_safety_profile.get("profile_fingerprint") != spec.profile_fingerprint:
        raise RawEvidenceError("component profile and session graph are mixed")
    if set(getattr(analysis, "transfer_composition_fingerprints", {})) != set(
        ROLE_ORDER
    ):
        raise RawEvidenceError(
            "accepted raw evidence requires configured-Fc M*C/P composition"
        )
    capture_id = hashlib.sha256(bytes(capture_wav)).hexdigest()
    take_id = f"{ANCHOR_POSE_ID}_take_{attempt:02d}"
    cycles = _sweep_cycles(program)
    response_map = {str(item.role): item for item in analysis.driver_responses}
    if set(response_map) != set(ROLE_ORDER):
        raise RawEvidenceError("raw evidence requires woofer and tweeter responses")

    frequency_basis = np.asarray(CHAIN_GRID_HZ, dtype=np.float64)
    protection_records: list[dict[str, Any]] = []
    transfer_by_role: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for role in ROLE_ORDER:
        P_sections = tuple(session_graph.protection_by_role[role])
        C_sections = tuple(session_graph.configured_by_role[role])
        P_sign = int(session_graph.measurement_polarity_sign_by_role[role])
        C_sign = int(session_graph.configured_polarity_sign_by_role[role])
        if P_sections != spec.sections_by_role()[role] or P_sign != spec.role(
            role
        ).physical_polarity_sign:
            raise RawEvidenceError("session graph P is inconsistent with its specification")
        P = linkwitz_riley_response_complex(
            frequency_basis, P_sections, polarity_sign=P_sign
        )
        C = linkwitz_riley_response_complex(
            frequency_basis, C_sections, polarity_sign=C_sign
        )
        transfer_by_role[role] = (P, C)
        protection_records.append(
            {
                "role": role,
                "measurement_filter_identity": _linear_identity(
                    kind="jts_crossover_role_measurement_protection",
                    role=role,
                    sections=P_sections,
                    sign=P_sign,
                ),
                "complex_transfer_P": _complex_record(P),
                "configured_filter_identity": _linear_identity(
                    kind="jts_crossover_role_configured_transfer",
                    role=role,
                    sections=C_sections,
                    sign=C_sign,
                ),
                "complex_transfer_C": _complex_record(C),
            }
        )

    drivers: dict[str, Any] = {}
    response_ids_by_segment: dict[str, dict[str, str]] = {}
    for role in ROLE_ORDER:
        P, C = transfer_by_role[role]
        driver, ids = _driver_record(
            response_map[role],
            session_id=session_id,
            capture_id=capture_id,
            take_id=take_id,
            program=program,
            cycles=cycles,
            driver_identity=_role_identity(spec, role),
            protection=P,
            configured=C,
        )
        drivers[role] = driver
        response_ids_by_segment[role] = ids

    groups = []
    for order, cycle in enumerate(cycles[:ANCHOR_REPEAT_GROUP_COUNT]):
        groups.append(
            {
                "group_id": f"selector_repeat_{order:02d}",
                "program_cycle_id": f"measure_cycle_{order:02d}",
                "stimulus_order": order,
                "accepted_order": order,
                "woofer_segment_id": cycle["woofer"],
                "tweeter_segment_id": cycle["tweeter"],
                "woofer_response_id": response_ids_by_segment["woofer"][
                    cycle["woofer"]
                ],
                "tweeter_response_id": response_ids_by_segment["tweeter"][
                    cycle["tweeter"]
                ],
                "woofer_capture_id": capture_id,
                "tweeter_capture_id": capture_id,
                "woofer_take_id": take_id,
                "tweeter_take_id": take_id,
            }
        )

    gains = {
        role: float(program.segment(cycles[0][role]).gain_db) for role in ROLE_ORDER
    }
    ledger = _timing_gain_ledger(analysis=analysis, program=program, gains=gains)
    proof = _linearity_proof(
        analysis=analysis,
        program=program,
        spec=spec,
    )
    targets = {role: spec.role(role).to_dict() for role in ROLE_ORDER}
    component_identity = _fingerprinted(
        "jts_crossover_component_safety_profile_identity",
        {
            "profile_fingerprint": spec.profile_fingerprint,
            "targets_by_role": targets,
        },
    )
    if set(calibration_identity) != {"applied", "calibration_id"}:
        raise RawEvidenceError("calibration identity schema mismatch")
    applied = calibration_identity["applied"]
    calibration_id = calibration_identity["calibration_id"]
    if type(applied) is not bool or (
        applied and (not isinstance(calibration_id, str) or not calibration_id)
    ) or (not applied and calibration_id is not None):
        raise RawEvidenceError("calibration identity meaning is invalid")
    calibration = _fingerprinted(
        "jts_crossover_calibration_identity",
        {"applied": applied, "calibration_id": calibration_id},
    )
    stimulus = _fingerprinted(
        "jts_crossover_stimulus_identity",
        {
            "program_id": str(program.program_id),
            "program_manifest_fingerprint": json_fingerprint(program.to_dict()),
            "capture_wav_sha256": capture_id,
            "attempt": attempt,
            "take_id": take_id,
        },
    )
    legal_bounds = (
        max(spec.role(role).crossover_search_band_hz[0] for role in ROLE_ORDER),
        min(spec.role(role).crossover_search_band_hz[1] for role in ROLE_ORDER),
    )
    configured_fc = float(configured_fc_hz)
    if not _finite_number(configured_fc) or not legal_bounds[0] <= configured_fc <= legal_bounds[1]:
        raise RawEvidenceError("configured Fc is outside component-policy bounds")
    session_core = {
        "schema_version": 1,
        "kind": "jts_crossover_raw_session_identity",
        "session_id": session_id,
        "measurement_graph_fingerprint": session_graph.identity["fingerprint"],
        "component_safety_identity_fingerprint": component_identity["fingerprint"],
        "calibration_fingerprint": calibration["fingerprint"],
        "stimulus_program_id": str(program.program_id),
    }
    core = {
        "schema_version": RAW_EVIDENCE_SCHEMA_VERSION,
        "kind": RAW_EVIDENCE_KIND,
        "session_id": session_id,
        "session_fingerprint": json_fingerprint(session_core),
        "evidence_set_id": f"{session_id}:{ANCHOR_POSE_ID}",
        "measurement_graph": dict(session_graph.identity),
        "measurement_protection_by_role": protection_records,
        "total_transfer_composition_policy": transfer_policy_payload(),
        "component_safety_profile_identity": component_identity,
        "calibration_identity": calibration,
        "stimulus_identity": stimulus,
        "sample_rate_hz": int(program.sample_rate_hz),
        "frequency_basis_hz": _finite_float_list(frequency_basis),
        "configured_lr4_fc_hz": configured_fc,
        "legal_fc_bounds_hz": list(legal_bounds),
        "poses": [
            {
                "pose_id": ANCHOR_POSE_ID,
                "order": 0,
                "lateral_offset_cm": 0.0,
                "pose_role": "anchor",
                "evidence_role": "design_axis",
                "capture_id": capture_id,
                "take_id": take_id,
                "timing_gain_ledger": ledger,
                "selector_repeat_groups": groups,
                "drivers": drivers,
                "linearity_limiter_proof": proof,
            }
        ],
    }
    payload = {**core, "evidence_set_fingerprint": json_fingerprint(core)}
    validate_raw_evidence_v1(payload, expected_session_id=session_id)
    return payload


_RAW_FIELDS = {
    "schema_version",
    "kind",
    "session_id",
    "session_fingerprint",
    "evidence_set_id",
    "measurement_graph",
    "measurement_protection_by_role",
    "total_transfer_composition_policy",
    "component_safety_profile_identity",
    "calibration_identity",
    "stimulus_identity",
    "sample_rate_hz",
    "frequency_basis_hz",
    "configured_lr4_fc_hz",
    "legal_fc_bounds_hz",
    "poses",
    "evidence_set_fingerprint",
}
_PROTECTION_FIELDS = {
    "role",
    "measurement_filter_identity",
    "complex_transfer_P",
    "configured_filter_identity",
    "complex_transfer_C",
}
_DRIVER_FIELDS = {
    "role",
    "driver_identity",
    "capture_id",
    "take_id",
    "ordered_response_ids",
    "responses",
    "nominal_complex_response",
    "nominal_magnitude_db",
    "valid_mask",
    "trust_band_hz",
    "validity_floor_hz",
    "snr_evidence",
    "repeat_uncertainty_db",
    "configured_fitter_input_S",
    "configured_composition_fingerprints",
}
_RESPONSE_FIELDS = {
    "response_id",
    "source_segment_id",
    "accepted_order",
    "capture_id",
    "take_id",
    "complex_response",
}
_GROUP_FIELDS = {
    "group_id",
    "program_cycle_id",
    "stimulus_order",
    "accepted_order",
    "woofer_segment_id",
    "tweeter_segment_id",
    "woofer_response_id",
    "tweeter_response_id",
    "woofer_capture_id",
    "tweeter_capture_id",
    "woofer_take_id",
    "tweeter_take_id",
}
_POSE_FIELDS = {
    "pose_id",
    "order",
    "lateral_offset_cm",
    "pose_role",
    "evidence_role",
    "capture_id",
    "take_id",
    "timing_gain_ledger",
    "selector_repeat_groups",
    "drivers",
    "linearity_limiter_proof",
}
_POSE_CONTRACTS = (
    ("anchor_00", 0, 0.0, "anchor", "design_axis"),
    ("left_12_cm", 1, -12.0, "lateral", "onax"),
    ("right_12_cm", 2, 12.0, "lateral", "onax"),
    ("left_40_cm", 3, -40.0, "lateral", "offax"),
    ("right_40_cm", 4, 40.0, "lateral", "offax"),
)


def _validate_linear_identity(
    raw: Any, *, role: str, kind: str
) -> tuple[tuple[LinearTransferSection, ...], int]:
    identity = _validate_fingerprinted(
        raw,
        fields={"role", "sections", "polarity_sign"},
        kind=kind,
        label="linear transfer identity",
    )
    if identity["role"] != role or identity["polarity_sign"] not in {-1, 1}:
        raise RawEvidenceError("linear transfer role or polarity is invalid")
    if not isinstance(identity["sections"], list):
        raise RawEvidenceError("linear transfer sections are invalid")
    try:
        sections = tuple(
            LinearTransferSection.from_mapping(item) for item in identity["sections"]
        )
    except (TypeError, ValueError) as exc:
        raise RawEvidenceError("linear transfer section is invalid") from exc
    return sections, int(identity["polarity_sign"])


def _validate_ledger(
    raw: Any, *, stimulus_gain_ceiling_dbfs: float
) -> Mapping[str, Any]:
    ledger = _validate_fingerprinted(
        raw,
        fields={
            "located_timing_corrections",
            "inter_driver_drift_correction",
            "stimulus_gain_db_by_segment",
            "capture_gain_level_solve_db_by_role",
            "anchor_frame",
        },
        kind="jts_crossover_timing_gain_ledger_v1",
        label="timing/gain ledger",
    )
    locations = ledger["located_timing_corrections"]
    location_fields = {
        "segment_id",
        "kind",
        "role",
        "scheduled_start",
        "located_start",
        "residual_samples",
        "confidence",
        "clipped",
    }
    if (
        not isinstance(locations, list)
        or not locations
        or any(not isinstance(item, Mapping) or set(item) != location_fields for item in locations)
        or len({item["segment_id"] for item in locations}) != len(locations)
    ):
        raise RawEvidenceError("timing ledger locations are invalid")
    for item in locations:
        if (
            not isinstance(item["segment_id"], str)
            or not item["segment_id"]
            or not isinstance(item["kind"], str)
            or item["role"] not in {*ROLE_ORDER, None}
            or type(item["scheduled_start"]) is not int
            or type(item["located_start"]) is not int
            or item["scheduled_start"] < 0
            or item["located_start"] < 0
            or not _finite_number(item["residual_samples"])
            or not _finite_number(item["confidence"])
            or not 0.0 <= float(item["confidence"]) <= 1.0
            or type(item["clipped"]) is not bool
        ):
            raise RawEvidenceError("timing ledger location meaning is invalid")
    drift = ledger["inter_driver_drift_correction"]
    if (
        not isinstance(drift, Mapping)
        or set(drift) != {"epsilon_ppm", "per_role_epsilon_ppm"}
        or not _finite_number(drift["epsilon_ppm"])
        or not isinstance(drift["per_role_epsilon_ppm"], Mapping)
        or set(drift["per_role_epsilon_ppm"]) != set(ROLE_ORDER)
        or not all(_finite_number(value) for value in drift["per_role_epsilon_ppm"].values())
    ):
        raise RawEvidenceError("timing ledger drift meaning is invalid")
    stimulus_gains = ledger["stimulus_gain_db_by_segment"]
    capture_gains = ledger["capture_gain_level_solve_db_by_role"]
    if (
        not isinstance(stimulus_gains, Mapping)
        or not stimulus_gains
        or not all(isinstance(key, str) and key and _finite_number(value) for key, value in stimulus_gains.items())
        or any(
            float(value) > stimulus_gain_ceiling_dbfs
            for value in stimulus_gains.values()
        )
        or not isinstance(capture_gains, Mapping)
        or set(capture_gains) != set(ROLE_ORDER)
        or not all(_finite_number(value) for value in capture_gains.values())
    ):
        raise RawEvidenceError("timing ledger gain meaning is invalid")
    frame = ledger["anchor_frame"]
    if (
        not isinstance(frame, Mapping)
        or set(frame) != {"trim_db", "delay_us", "polarity"}
        or not isinstance(frame["trim_db"], Mapping)
        or set(frame["trim_db"]) != set(ROLE_ORDER)
        or not all(_finite_number(value) for value in frame["trim_db"].values())
        or not _finite_number(frame["delay_us"])
        or frame["polarity"] not in {"normal", "inverted"}
    ):
        raise RawEvidenceError("timing ledger anchor frame is invalid")
    return ledger


def _validate_linearity_proof(raw: Any, *, spec: ProtectedNeutralGraphSpec) -> None:
    proof = _validate_fingerprinted(
        raw,
        fields={
            "proof_basis",
            "limiter_threshold_dbfs",
            "stimulus_gain_ceiling_dbfs",
            "volume_ceiling_db",
            "graph_headroom_gain_db",
            "maximum_programmed_stimulus_gain_dbfs",
            "maximum_effective_stimulus_peak_dbfs",
            "threshold_margin_db",
            "boundary_rule",
            "limiter_engaged",
            "capture_linearity_passed",
            "clipped_segments",
        },
        kind="jts_crossover_limiter_linearity_proof_v1",
        label="linearity/limiter proof",
    )
    limiter = proof["limiter_threshold_dbfs"]
    ceiling = proof["stimulus_gain_ceiling_dbfs"]
    volume_ceiling = proof["volume_ceiling_db"]
    headroom = proof["graph_headroom_gain_db"]
    programmed = proof["maximum_programmed_stimulus_gain_dbfs"]
    peak = proof["maximum_effective_stimulus_peak_dbfs"]
    margin = proof["threshold_margin_db"]
    if (
        not all(
            _finite_number(value)
            for value in (
                limiter,
                ceiling,
                volume_ceiling,
                headroom,
                programmed,
                peak,
                margin,
            )
        )
        or proof["proof_basis"] != "emitted_graph_worst_case_at_volume_ceiling"
        or float(limiter) != spec.limiter_threshold_dbfs
        or float(ceiling) != spec.stimulus_peak_ceiling_dbfs
        or float(volume_ceiling) != spec.volume_ceiling_db
        or float(headroom) != spec.graph_headroom_gain_db
        or float(programmed) > float(ceiling)
        or float(peak) != spec.worst_case_limiter_input_peak_dbfs
        or float(peak) >= float(limiter)
        or float(margin) != spec.worst_case_limiter_margin_db
        or float(margin) <= 0.0
        or proof["boundary_rule"] != "stimulus_peak_must_be_strictly_below_threshold"
        or proof["limiter_engaged"] is not False
        or proof["capture_linearity_passed"] is not True
        or proof["clipped_segments"] != []
    ):
        raise RawEvidenceError("linearity/limiter proof is inconsistent")


def _validate_driver(
    raw: Any,
    *,
    role: str,
    session_id: str,
    capture_id: str,
    take_id: str,
    freqs: np.ndarray,
    expected_identity: Mapping[str, str],
    expected_segment_ids: Sequence[str],
    groups: Sequence[Mapping[str, Any]],
    protection: np.ndarray,
    configured: np.ndarray,
) -> None:
    if not isinstance(raw, Mapping) or set(raw) != _DRIVER_FIELDS or raw["role"] != role:
        raise RawEvidenceError("raw driver schema or role mismatch")
    if raw["driver_identity"] != expected_identity:
        raise RawEvidenceError("raw driver target identity is mixed")
    if raw["capture_id"] != capture_id or raw["take_id"] != take_id:
        raise RawEvidenceError("raw driver capture/take identity is mixed")
    response_ids = raw["ordered_response_ids"]
    responses = raw["responses"]
    if (
        not isinstance(response_ids, list)
        or len(response_ids) < ANCHOR_REPEAT_GROUP_COUNT
        or len(set(response_ids)) != len(response_ids)
        or not isinstance(responses, list)
        or len(responses) != len(response_ids)
    ):
        raise RawEvidenceError("raw driver repeat evidence is incomplete")
    if len(expected_segment_ids) != len(response_ids):
        raise RawEvidenceError("raw driver responses do not cover the timing ledger")
    values_by_id: dict[str, np.ndarray] = {}
    segment_by_id: dict[str, str] = {}
    for order, response in enumerate(responses):
        if (
            not isinstance(response, Mapping)
            or set(response) != _RESPONSE_FIELDS
            or response["response_id"] != response_ids[order]
            or response["accepted_order"] != order
            or response["capture_id"] != capture_id
            or response["take_id"] != take_id
            or not isinstance(response["source_segment_id"], str)
            or not response["source_segment_id"]
            or response["source_segment_id"] in segment_by_id.values()
            or response["response_id"]
            != _response_id(
                session_id=session_id,
                capture_id=capture_id,
                role=role,
                segment_id=response["source_segment_id"],
            )
        ):
            raise RawEvidenceError("raw response identity is mixed")
        values_by_id[response["response_id"]] = _complex_from_record(
            response["complex_response"], length=freqs.size
        )
        segment_by_id[response["response_id"]] = response["source_segment_id"]
    if [segment_by_id[item] for item in response_ids] != list(expected_segment_ids):
        raise RawEvidenceError("raw driver response order differs from the timing ledger")
    selector_ids = [group[f"{role}_response_id"] for group in groups]
    selector_segments = [group[f"{role}_segment_id"] for group in groups]
    if any(
        response_id not in values_by_id
        or segment_by_id[response_id] != segment_id
        for response_id, segment_id in zip(selector_ids, selector_segments)
    ):
        raise RawEvidenceError("selector group does not reference its source observation")
    selector_rows = np.stack([values_by_id[item] for item in selector_ids], axis=0)
    expected_nominal = np.mean(selector_rows, axis=0, dtype=np.complex128)
    nominal = _complex_from_record(raw["nominal_complex_response"], length=freqs.size)
    if not np.array_equal(nominal, expected_nominal):
        raise RawEvidenceError("nominal response is not the selector-group complex mean")
    expected_magnitude = 20.0 * np.log10(np.maximum(np.abs(nominal), 1e-12))
    try:
        magnitude = np.asarray(raw["nominal_magnitude_db"], dtype=np.float64)
        uncertainty = np.asarray(raw["repeat_uncertainty_db"], dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise RawEvidenceError("driver magnitude or uncertainty is invalid") from exc
    expected_uncertainty = np.std(
        20.0 * np.log10(np.maximum(np.abs(selector_rows), 1e-12)),
        axis=0,
        dtype=np.float64,
    )
    if (
        not np.array_equal(magnitude, expected_magnitude)
        or not np.array_equal(uncertainty, expected_uncertainty)
        or not np.all(np.isfinite(uncertainty))
    ):
        raise RawEvidenceError("driver magnitude or repeat uncertainty is fabricated")
    trust = raw["trust_band_hz"]
    floor = raw["validity_floor_hz"]
    mask = raw["valid_mask"]
    if (
        not isinstance(trust, list)
        or len(trust) != 2
        or not all(_finite_number(value) and float(value) > 0.0 for value in trust)
        or not float(trust[0]) < float(trust[1])
        or (floor is not None and (not _finite_number(floor) or float(floor) <= 0.0))
        or not isinstance(mask, list)
        or len(mask) != freqs.size
        or not all(type(value) is bool for value in mask)
    ):
        raise RawEvidenceError("driver trustworthy mask contract is invalid")
    expected_mask = (freqs >= float(trust[0])) & (freqs <= float(trust[1]))
    if floor is not None:
        expected_mask &= freqs >= float(floor)
    if mask != expected_mask.tolist() or not np.any(expected_mask):
        raise RawEvidenceError("driver trustworthy mask is inconsistent")
    _validate_fingerprinted(
        raw["snr_evidence"],
        fields={"verdict"},
        kind="jts_crossover_snr_evidence_v1",
        label="driver SNR evidence",
    )
    if not isinstance(raw["snr_evidence"]["verdict"], Mapping):
        raise RawEvidenceError("driver SNR verdict is invalid")
    try:
        composition = compose_total_transfer(
            nominal, configured, protection, required_mask=expected_mask
        )
    except (TotalTransferCompositionError, ValueError) as exc:
        raise RawEvidenceError("configured composition no longer satisfies policy") from exc
    stored_S = _complex_from_record(
        raw["configured_fitter_input_S"], length=freqs.size
    )
    if (
        not np.array_equal(stored_S, composition.fitter_input)
        or raw["configured_composition_fingerprints"]
        != dict(composition.fingerprints)
    ):
        raise RawEvidenceError("configured S=M*C/P proof is inconsistent")


def _validate_pose(
    pose: Any,
    *,
    contract: tuple[str, int, float, str, str],
    session_id: str,
    freqs: np.ndarray,
    spec: ProtectedNeutralGraphSpec,
    transfers: Mapping[str, tuple[np.ndarray, np.ndarray]],
) -> None:
    pose_id, order, offset, pose_role, evidence_role = contract
    if (
        not isinstance(pose, Mapping)
        or set(pose) != _POSE_FIELDS
        or (
            pose["pose_id"],
            pose["order"],
            pose["lateral_offset_cm"],
            pose["pose_role"],
            pose["evidence_role"],
        )
        != (pose_id, order, offset, pose_role, evidence_role)
        or not _is_sha256(pose["capture_id"])
        or not isinstance(pose["take_id"], str)
        or not pose["take_id"]
    ):
        raise RawEvidenceError("raw pose schema or ordered identity is invalid")
    ledger = _validate_ledger(
        pose["timing_gain_ledger"],
        stimulus_gain_ceiling_dbfs=spec.stimulus_peak_ceiling_dbfs,
    )
    groups = pose["selector_repeat_groups"]
    if (
        not isinstance(groups, list)
        or len(groups) != ANCHOR_REPEAT_GROUP_COUNT
        or any(not isinstance(group, Mapping) or set(group) != _GROUP_FIELDS for group in groups)
        or [group["accepted_order"] for group in groups] != list(range(3))
        or [group["stimulus_order"] for group in groups] != list(range(3))
        or len({group["group_id"] for group in groups}) != 3
        or len({group["program_cycle_id"] for group in groups}) != 3
    ):
        raise RawEvidenceError("raw pose requires exactly three ordered paired groups")
    sweep_locations = [
        item
        for item in ledger["located_timing_corrections"]
        if item["kind"] == KIND_SWEEP
    ]
    if (
        len(sweep_locations) < ANCHOR_REPEAT_GROUP_COUNT * len(ROLE_ORDER)
        or len(sweep_locations) % len(ROLE_ORDER)
        or any(
            left["scheduled_start"] >= right["scheduled_start"]
            for left, right in zip(sweep_locations, sweep_locations[1:])
        )
    ):
        raise RawEvidenceError("timing ledger has no canonical ordered sweep cycles")
    ledger_cycles: list[dict[str, str]] = []
    for index in range(0, len(sweep_locations), len(ROLE_ORDER)):
        pair = sweep_locations[index : index + len(ROLE_ORDER)]
        if tuple(item["role"] for item in pair) != ROLE_ORDER:
            raise RawEvidenceError("timing ledger sweep cycle roles are noncanonical")
        ledger_cycles.append(
            {role: str(item["segment_id"]) for role, item in zip(ROLE_ORDER, pair)}
        )
    located_ids = {item["segment_id"] for item in sweep_locations}
    stimulus_gains = ledger["stimulus_gain_db_by_segment"]
    for group_order, group in enumerate(groups):
        cycle = ledger_cycles[group_order]
        if any(
            not isinstance(group[key], str) or not group[key]
            for key in (
                "group_id",
                "program_cycle_id",
                "woofer_segment_id",
                "tweeter_segment_id",
                "woofer_response_id",
                "tweeter_response_id",
            )
        ) or (
            group["group_id"] != f"selector_repeat_{group_order:02d}"
            or group["program_cycle_id"] != f"measure_cycle_{group_order:02d}"
            or group["stimulus_order"] != group_order
            or group["accepted_order"] != group_order
            or any(
                group[f"{role}_segment_id"] != cycle[role] for role in ROLE_ORDER
            )
        ) or any(
            group[key] != pose[value]
            for key, value in (
                ("woofer_capture_id", "capture_id"),
                ("tweeter_capture_id", "capture_id"),
                ("woofer_take_id", "take_id"),
                ("tweeter_take_id", "take_id"),
            )
        ) or any(
            group[f"{role}_segment_id"] not in located_ids
            or group[f"{role}_segment_id"] not in stimulus_gains
            for role in ROLE_ORDER
        ):
            raise RawEvidenceError("raw paired group timing/capture identity is mixed")
    _validate_linearity_proof(
        pose["linearity_limiter_proof"],
        spec=spec,
    )
    drivers = pose["drivers"]
    if not isinstance(drivers, Mapping) or tuple(drivers) != ROLE_ORDER:
        raise RawEvidenceError("raw driver mapping is missing or reordered")
    for role in ROLE_ORDER:
        P, C = transfers[role]
        _validate_driver(
            drivers[role],
            role=role,
            session_id=session_id,
            capture_id=pose["capture_id"],
            take_id=pose["take_id"],
            freqs=freqs,
            expected_identity=_role_identity(spec, role),
            expected_segment_ids=[cycle[role] for cycle in ledger_cycles],
            groups=groups,
            protection=P,
            configured=C,
        )


def validate_raw_evidence_v1(
    raw: Mapping[str, Any],
    *,
    expected_session_id: str | None = None,
    expected_graph_fingerprint: str | None = None,
    expected_safety_fingerprint: str | None = None,
    expected_calibration_fingerprint: str | None = None,
    expected_stimulus_fingerprint: str | None = None,
) -> None:
    """Reject schema drift, stale identities, and re-signable semantic fraud."""

    if not isinstance(raw, Mapping) or set(raw) != _RAW_FIELDS:
        raise RawEvidenceError("raw evidence has unknown or missing top-level fields")
    if raw["schema_version"] != 1 or raw["kind"] != RAW_EVIDENCE_KIND:
        raise RawEvidenceError("raw evidence schema or kind mismatch")
    session_id = raw["session_id"]
    if not isinstance(session_id, str) or not session_id:
        raise RawEvidenceError("raw evidence session identity is invalid")
    if expected_session_id is not None and session_id != expected_session_id:
        raise RawEvidenceError("raw evidence belongs to a stale session")
    if raw["evidence_set_id"] != f"{session_id}:{ANCHOR_POSE_ID}":
        raise RawEvidenceError("raw evidence set identity is mixed")
    core = {key: value for key, value in raw.items() if key != "evidence_set_fingerprint"}
    if raw["evidence_set_fingerprint"] != json_fingerprint(core):
        raise RawEvidenceError("raw evidence fingerprint mismatch")
    spec = _graph_spec(raw["measurement_graph"])
    if (
        expected_graph_fingerprint is not None
        and raw["measurement_graph"]["fingerprint"] != expected_graph_fingerprint
    ):
        raise RawEvidenceError("measurement graph belongs to a stale session")
    policy = raw["total_transfer_composition_policy"]
    if policy != transfer_policy_payload():
        raise RawEvidenceError("total-transfer composition policy is invalid")
    component = _validate_fingerprinted(
        raw["component_safety_profile_identity"],
        fields={"profile_fingerprint", "targets_by_role"},
        kind="jts_crossover_component_safety_profile_identity",
        label="component safety identity",
    )
    if (
        component["profile_fingerprint"] != spec.profile_fingerprint
        or component["targets_by_role"]
        != {role: spec.role(role).to_dict() for role in ROLE_ORDER}
    ):
        raise RawEvidenceError("component safety identity is mixed")
    if (
        expected_safety_fingerprint is not None
        and component["fingerprint"] != expected_safety_fingerprint
    ):
        raise RawEvidenceError("component safety identity is stale")
    calibration = _validate_fingerprinted(
        raw["calibration_identity"],
        fields={"applied", "calibration_id"},
        kind="jts_crossover_calibration_identity",
        label="calibration identity",
    )
    if (
        type(calibration["applied"]) is not bool
        or (
            calibration["applied"]
            and (
                not isinstance(calibration["calibration_id"], str)
                or not calibration["calibration_id"]
            )
        )
        or (not calibration["applied"] and calibration["calibration_id"] is not None)
    ):
        raise RawEvidenceError("calibration identity meaning is invalid")
    if (
        expected_calibration_fingerprint is not None
        and calibration["fingerprint"] != expected_calibration_fingerprint
    ):
        raise RawEvidenceError("calibration identity is stale")
    stimulus = _validate_fingerprinted(
        raw["stimulus_identity"],
        fields={
            "program_id",
            "program_manifest_fingerprint",
            "capture_wav_sha256",
            "attempt",
            "take_id",
        },
        kind="jts_crossover_stimulus_identity",
        label="stimulus identity",
    )
    if (
        not _is_sha256(stimulus["program_id"])
        or not _is_sha256(stimulus["program_manifest_fingerprint"])
        or not _is_sha256(stimulus["capture_wav_sha256"])
        or type(stimulus["attempt"]) is not int
        or stimulus["attempt"] < 1
        or stimulus["take_id"] != f"anchor_00_take_{stimulus['attempt']:02d}"
    ):
        raise RawEvidenceError("stimulus identity meaning is invalid")
    if (
        expected_stimulus_fingerprint is not None
        and stimulus["fingerprint"] != expected_stimulus_fingerprint
    ):
        raise RawEvidenceError("stimulus identity is stale")
    session_core = {
        "schema_version": 1,
        "kind": "jts_crossover_raw_session_identity",
        "session_id": session_id,
        "measurement_graph_fingerprint": raw["measurement_graph"]["fingerprint"],
        "component_safety_identity_fingerprint": component["fingerprint"],
        "calibration_fingerprint": calibration["fingerprint"],
        "stimulus_program_id": stimulus["program_id"],
    }
    if raw["session_fingerprint"] != json_fingerprint(session_core):
        raise RawEvidenceError("raw evidence session fingerprint mismatch")
    if raw["sample_rate_hz"] != spec.sample_rate_hz:
        raise RawEvidenceError("raw evidence sample rate is inconsistent")
    try:
        freqs = np.asarray(raw["frequency_basis_hz"], dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise RawEvidenceError("raw evidence frequency basis is invalid") from exc
    if not np.array_equal(freqs, CHAIN_GRID_HZ):
        raise RawEvidenceError("raw evidence must use the canonical bounded branch grid")
    expected_bounds = [
        max(spec.role(role).crossover_search_band_hz[0] for role in ROLE_ORDER),
        min(spec.role(role).crossover_search_band_hz[1] for role in ROLE_ORDER),
    ]
    if (
        raw["legal_fc_bounds_hz"] != expected_bounds
        or not _finite_number(raw["configured_lr4_fc_hz"])
        or not expected_bounds[0]
        <= float(raw["configured_lr4_fc_hz"])
        <= expected_bounds[1]
    ):
        raise RawEvidenceError("configured Fc or component-policy bounds are invalid")
    protections = raw["measurement_protection_by_role"]
    if (
        not isinstance(protections, list)
        or [item.get("role") for item in protections if isinstance(item, Mapping)]
        != list(ROLE_ORDER)
    ):
        raise RawEvidenceError("measurement protection roles are missing or reordered")
    transfers: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for record in protections:
        if not isinstance(record, Mapping) or set(record) != _PROTECTION_FIELDS:
            raise RawEvidenceError("measurement protection record schema mismatch")
        role = str(record["role"])
        P_sections, P_sign = _validate_linear_identity(
            record["measurement_filter_identity"],
            role=role,
            kind="jts_crossover_role_measurement_protection",
        )
        C_sections, C_sign = _validate_linear_identity(
            record["configured_filter_identity"],
            role=role,
            kind="jts_crossover_role_configured_transfer",
        )
        if (
            P_sections != spec.sections_by_role()[role]
            or P_sign != spec.role(role).physical_polarity_sign
            or len(C_sections) != 1
            or C_sections[0].highpass != (role == "tweeter")
            or C_sections[0].frequency_hz != float(raw["configured_lr4_fc_hz"])
            or C_sections[0].order != 4
            or C_sections[0].reason != "configured_crossover_complete_transfer"
        ):
            raise RawEvidenceError("measurement P or complete configured C is inconsistent")
        P = _complex_from_record(record["complex_transfer_P"], length=freqs.size)
        C = _complex_from_record(record["complex_transfer_C"], length=freqs.size)
        expected_P = linkwitz_riley_response_complex(
            freqs, P_sections, polarity_sign=P_sign
        )
        expected_C = linkwitz_riley_response_complex(
            freqs, C_sections, polarity_sign=C_sign
        )
        if not np.array_equal(P, expected_P) or not np.array_equal(C, expected_C):
            raise RawEvidenceError("emitted P or configured C transfer is inconsistent")
        transfers[role] = (P, C)
    poses = raw["poses"]
    if (
        not isinstance(poses, list)
        or len(poses) not in {1, len(_POSE_CONTRACTS)}
        or not all(isinstance(pose, Mapping) for pose in poses)
    ):
        raise RawEvidenceError("v1 raw evidence must contain anchor or all five poses")
    if len(poses) == len(_POSE_CONTRACTS) and (
        len({pose["capture_id"] for pose in poses}) != len(poses)
        or len({pose["take_id"] for pose in poses}) != len(poses)
    ):
        raise RawEvidenceError("five-pose captures and takes must be distinct")
    for pose, contract in zip(poses, _POSE_CONTRACTS):
        _validate_pose(
            pose,
            contract=contract,
            session_id=session_id,
            freqs=freqs,
            spec=spec,
            transfers=transfers,
        )
    anchor = poses[0]
    if (
        stimulus["capture_wav_sha256"] != anchor["capture_id"]
        or stimulus["take_id"] != anchor["take_id"]
    ):
        raise RawEvidenceError("stimulus and anchor capture/take identities are mixed")
