# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R15 protected-neutral graph and anchor RAW-evidence contracts.

This module is the crossover-v2 adapter around existing program analysis and
the strict commissioning evidence store.  It owns no capture, persistence, or
selector engine: it describes the one transient graph the existing playback
seam loads, freezes the two v1 JSON envelopes, and builds the anchor record
from the already-computed three paired driver responses.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.evidence_identity import (
    ExactDspStateIdentity,
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.audio_measurement.transfer_composition import (
    RAW_EVIDENCE_KIND,
    SELECTOR_RESULT_KIND,
    LinearTransferSection,
    complex_fingerprint,
    linkwitz_riley_response_complex,
    sections_fingerprint,
    serialize_sections,
    transfer_policy_payload,
)

RAW_EVIDENCE_SCHEMA_VERSION = 1
SELECTOR_RESULT_SCHEMA_VERSION = 1
RAW_EVIDENCE_INVALID_REASON = "invalid_evidence_identity"
ANCHOR_POSE_ID = "anchor_00"
ANCHOR_REPEAT_GROUP_COUNT = 3
ROLE_ORDER = ("woofer", "tweeter")
PROTECTION_FILTER_ORDER = 4
PROTECTED_NEUTRAL_GRAPH_KIND = "jts_crossover_protected_neutral_graph_v1"
PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS = (
    "prior_alignment",
    "prior_driver_linearization",
    "room_correction",
    "bass_extension",
    "preference_eq",
    "configured_crossover",
    "candidate_crossover",
)


class RawEvidenceError(ValueError):
    """A raw evidence set is stale, mixed, malformed, or self-inconsistent."""

    def __init__(self, detail: str, *, reason: str = RAW_EVIDENCE_INVALID_REASON) -> None:
        super().__init__(detail)
        self.reason = reason


def validate_protected_neutral_program_routing(
    program: Any, graph_identity: Mapping[str, Any]
) -> None:
    """Refuse any program whose active inputs do not match the exact graph."""

    role_channels = graph_identity.get("role_channels")
    summed_channel = graph_identity.get("summed_channel")
    if (
        graph_identity.get("kind") != PROTECTED_NEUTRAL_GRAPH_KIND
        or not isinstance(role_channels, Mapping)
        or tuple(role_channels) != ROLE_ORDER
        or not all(type(role_channels[role]) is int for role in ROLE_ORDER)
        or type(summed_channel) is not int
    ):
        raise RawEvidenceError("protected-neutral program graph identity is invalid")
    expected_channels = {int(role_channels[role]) for role in ROLE_ORDER}
    expected_channels.add(summed_channel)
    if int(getattr(program, "channels", 0)) != 1 + max(expected_channels):
        raise RawEvidenceError("pre-apply program channel count mismatches its graph")
    for segment in program.stimulus_segments():
        role = str(segment.role or "")
        expected = summed_channel if role == "summed" else role_channels.get(role)
        if expected is None or segment.channel != int(expected):
            raise RawEvidenceError(
                "pre-apply program stimulus routing mismatches its graph"
            )


@dataclass(frozen=True)
class ProtectedNeutralSessionGraph:
    """The exact graph object shared by playback and RAW-evidence publication."""

    yaml_text: str
    identity: Mapping[str, Any]
    protection_by_role: Mapping[str, tuple[LinearTransferSection, ...]]
    configured_by_role: Mapping[str, tuple[LinearTransferSection, ...]]
    polarity_sign_by_role: Mapping[str, int]
    limiter_threshold_dbfs: float
    volume_ceiling_db: float


def build_protected_neutral_session_graph(
    *,
    preset: Any,
    roles_bands: Sequence[Any],
    role_channels: Mapping[str, int],
    summed_channel: int,
    playback_device: str,
    limiter_threshold_dbfs: float = -12.0,
    volume_ceiling_db: float = 0.0,
) -> ProtectedNeutralSessionGraph:
    """Build once per session; the existing playback owner loads/restores it."""

    from jasper.active_speaker.camilla_yaml import emit_active_speaker_program_config

    protection = measurement_protection_from_role_bands(roles_bands)
    configured = configured_transfer_from_preset(preset)
    signs = role_polarity_signs(preset)
    yaml_text = emit_active_speaker_program_config(
        preset,
        role_channels=dict(role_channels),
        summed_channel=summed_channel,
        measurement_protection_by_role=protection,
        playback_device=playback_device,
        limiter_clip_limit_db=limiter_threshold_dbfs,
        volume_limit_db=volume_ceiling_db,
    )
    identity = protected_neutral_graph_identity(
        yaml_text,
        protection_by_role=protection,
        role_channels=role_channels,
        summed_channel=summed_channel,
        polarity_sign_by_role=signs,
        limiter_threshold_dbfs=limiter_threshold_dbfs,
        volume_ceiling_db=volume_ceiling_db,
    )
    return ProtectedNeutralSessionGraph(
        yaml_text=yaml_text,
        identity=identity,
        protection_by_role=protection,
        configured_by_role=configured,
        polarity_sign_by_role=signs,
        limiter_threshold_dbfs=float(limiter_threshold_dbfs),
        volume_ceiling_db=float(volume_ceiling_db),
    )


def measurement_protection_from_role_bands(
    roles_bands: Sequence[Any],
) -> dict[str, tuple[LinearTransferSection, ...]]:
    """Build role protection from the already-admitted declared hard bands."""

    result: dict[str, tuple[LinearTransferSection, ...]] = {}
    for role_band in roles_bands:
        role = str(getattr(role_band, "role", ""))
        band = getattr(role_band, "band", None)
        lo = float(getattr(band, "lower_hz", 0.0))
        hi = float(getattr(band, "upper_hz", 0.0))
        if role not in ROLE_ORDER or not (math.isfinite(lo) and math.isfinite(hi)):
            raise ValueError("protected-neutral graph requires finite woofer/tweeter bands")
        if not 0.0 < lo < hi < 24_000.0:
            raise ValueError("protected-neutral graph band must stay inside 48 kHz Nyquist")
        result[role] = (
            LinearTransferSection(
                highpass=True,
                frequency_hz=lo,
                order=PROTECTION_FILTER_ORDER,
                reason="declared_hard_excitation_floor",
            ),
            LinearTransferSection(
                highpass=False,
                frequency_hz=hi,
                order=PROTECTION_FILTER_ORDER,
                reason="declared_hard_excitation_ceiling",
            ),
        )
    if tuple(result) != ROLE_ORDER:
        raise ValueError("protected-neutral graph requires ordered woofer/tweeter roles")
    return result


def configured_transfer_from_preset(
    preset: Any,
) -> dict[str, tuple[LinearTransferSection, ...]]:
    """The complete configured crossover transfer ``C`` for each role."""

    result: dict[str, list[LinearTransferSection]] = {role: [] for role in ROLE_ORDER}
    regions = tuple(getattr(preset, "crossover_regions", ()) or ())
    if len(regions) != 1 or int(getattr(regions[0], "order", 0)) != 4:
        raise ValueError("R15 requires exactly one configured LR4 crossover region")
    for region in regions:
        lower = str(getattr(region, "lower_driver", ""))
        upper = str(getattr(region, "upper_driver", ""))
        frequency = float(getattr(region, "fc_hz", 0.0))
        order = int(getattr(region, "order", 0))
        if lower in result:
            result[lower].append(
                LinearTransferSection(
                    highpass=False,
                    frequency_hz=frequency,
                    order=order,
                    reason="configured_crossover_complete_transfer",
                )
            )
        if upper in result:
            result[upper].append(
                LinearTransferSection(
                    highpass=True,
                    frequency_hz=frequency,
                    order=order,
                    reason="configured_crossover_complete_transfer",
                )
            )
    if any(not result[role] for role in ROLE_ORDER):
        raise ValueError("configured 2-way crossover must define both role transfers")
    return {role: tuple(result[role]) for role in ROLE_ORDER}


def role_polarity_signs(preset: Any) -> dict[str, int]:
    signs = {role: 1 for role in ROLE_ORDER}
    for region in tuple(getattr(preset, "crossover_regions", ()) or ()):
        lower = str(getattr(region, "lower_driver", ""))
        upper = str(getattr(region, "upper_driver", ""))
        if lower in signs:
            signs[lower] = -1 if getattr(region, "lower_polarity", "") == "inverted" else 1
        if upper in signs:
            signs[upper] = -1 if getattr(region, "upper_polarity", "") == "inverted" else 1
    return signs


def protected_neutral_graph_identity(
    yaml_text: str,
    *,
    protection_by_role: Mapping[str, Sequence[LinearTransferSection]],
    role_channels: Mapping[str, int],
    summed_channel: int,
    polarity_sign_by_role: Mapping[str, int],
    limiter_threshold_dbfs: float,
    volume_ceiling_db: float,
) -> dict[str, Any]:
    """Fingerprint the exact emitted graph and its linear protection proof."""

    encoded = yaml_text.encode("utf-8")
    active_raw_sha256 = hashlib.sha256(encoded).hexdigest()
    exact_state = ExactDspStateIdentity(
        {
            "active_raw_sha256": active_raw_sha256,
            "active_raw_bytes": len(encoded),
        }
    )
    proof_core = {
        "schema_version": 1,
        "kind": "jts_crossover_measurement_protection_proof",
        "protection_sections_by_role": serialize_sections(protection_by_role),
        "protection_sections_fingerprint": sections_fingerprint(
            protection_by_role, kind="jts_crossover_measurement_protection_sections"
        ),
        "limiter": {
            "type": "soft_clip",
            "threshold_dbfs": float(limiter_threshold_dbfs),
            "excluded_from_linear_transfer_P": True,
        },
        "volume_ceiling_db": float(volume_ceiling_db),
        "excluded_filter_layers": list(PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS),
    }
    proof = {**proof_core, "fingerprint": json_fingerprint(proof_core)}
    graph_core = {
        "schema_version": 1,
        "kind": PROTECTED_NEUTRAL_GRAPH_KIND,
        "graph_id": f"protected-neutral-{active_raw_sha256[:16]}",
        "active_raw_sha256": active_raw_sha256,
        "exact_dsp_state_identity": exact_state.to_dict(),
        "protection_proof_identity": proof,
        "role_channels": {role: int(role_channels[role]) for role in ROLE_ORDER},
        "summed_channel": int(summed_channel),
        "polarity_sign_by_role": {
            role: int(polarity_sign_by_role[role]) for role in ROLE_ORDER
        },
        "lifecycle": {
            "load": "inline_transient_under_existing_dsp_writer_lock",
            "restore": "exact_entry_config_on_success_abort_or_playback_failure",
            "durable_profile_mutated": False,
            "undo_mutated": False,
            "process_restart_target": "unchanged_persisted_entry_config",
        },
    }
    return {**graph_core, "fingerprint": json_fingerprint(graph_core)}


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


def _response_id(
    *, session_id: str, capture_id: str, role: str, repeat_index: int
) -> str:
    return f"{session_id}:{capture_id}:{role}:{repeat_index}"


def _component_role_contracts(
    component_safety_profile: Mapping[str, Any],
    *,
    configured_fc_hz: float,
) -> tuple[dict[str, dict[str, str]], tuple[float, float]]:
    """Return exact role/target identities and the confirmed closed Fc fence."""

    raw_targets = component_safety_profile.get("targets")
    if not isinstance(raw_targets, list):
        raise RawEvidenceError("component safety profile has no confirmed targets")
    identities: dict[str, dict[str, str]] = {}
    search_bands: list[tuple[float, float]] = []
    for target in raw_targets:
        if not isinstance(target, Mapping):
            continue
        role = str(target.get("role") or "")
        if role not in ROLE_ORDER:
            continue
        if role in identities:
            raise RawEvidenceError(f"component safety profile repeats role {role}")
        target_id = str(target.get("target_id") or "")
        target_fingerprint = str(target.get("target_fingerprint") or "")
        search = target.get("crossover_search_band_hz")
        if (
            not target_id
            or len(target_fingerprint) != 64
            or not isinstance(search, list)
            or len(search) != 2
        ):
            raise RawEvidenceError(
                f"confirmed {role} target lacks exact identity or crossover bounds"
            )
        try:
            lo, hi = (float(search[0]), float(search[1]))
        except (TypeError, ValueError) as exc:
            raise RawEvidenceError(
                f"confirmed {role} crossover bounds are not numeric"
            ) from exc
        if not (math.isfinite(lo) and math.isfinite(hi) and 0.0 < lo <= hi):
            raise RawEvidenceError(f"confirmed {role} crossover bounds are invalid")
        identities[role] = {
            "role": role,
            "target_id": target_id,
            "target_fingerprint": target_fingerprint,
        }
        search_bands.append((lo, hi))
    if set(identities) != set(ROLE_ORDER):
        raise RawEvidenceError(
            "component safety profile must identify one woofer and one tweeter"
        )
    identities = {role: identities[role] for role in ROLE_ORDER}
    legal = (
        max(band[0] for band in search_bands),
        min(band[1] for band in search_bands),
    )
    if not legal[0] <= float(configured_fc_hz) <= legal[1]:
        raise RawEvidenceError(
            "configured Fc is outside confirmed component-policy bounds"
        )
    return identities, legal


def _driver_record(
    primary: Any,
    *,
    session_id: str,
    capture_id: str,
    attempt: int,
    role_band: Any,
    driver_identity: Mapping[str, str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    occurrences = (primary, *tuple(primary.repeat_responses))
    if len(occurrences) != ANCHOR_REPEAT_GROUP_COUNT:
        raise RawEvidenceError("anchor evidence requires exactly three responses per role")
    freqs = np.asarray(primary.freqs_hz, dtype=np.float64)
    band_lo = float(role_band.band.lower_hz)
    band_hi = float(role_band.band.upper_hz)
    response_ids: list[str] = []
    response_records: list[dict[str, Any]] = []
    complex_values: list[np.ndarray] = []
    for index, response in enumerate(occurrences):
        response_freqs = np.asarray(response.freqs_hz, dtype=np.float64)
        values = np.asarray(response.complex_tf, dtype=np.complex128)
        if not np.array_equal(response_freqs, freqs) or values.shape != freqs.shape:
            raise RawEvidenceError("paired responses do not share one frequency basis")
        response_id = _response_id(
            session_id=session_id,
            capture_id=capture_id,
            role=primary.role,
            repeat_index=index,
        )
        response_ids.append(response_id)
        complex_values.append(values)
        response_records.append(
            {
                "response_id": response_id,
                "repeat_index": index,
                "complex_response": _complex_record(values),
            }
        )
    matrix = np.stack(complex_values, axis=0)
    nominal = np.mean(matrix, axis=0, dtype=np.complex128)
    magnitudes = 20.0 * np.log10(np.maximum(np.abs(matrix), 1e-12))
    uncertainty = np.std(magnitudes, axis=0, dtype=np.float64)
    validity_floor = primary.validity_floor_hz
    valid = (
        np.isfinite(nominal.real)
        & np.isfinite(nominal.imag)
        & (freqs >= band_lo)
        & (freqs <= band_hi)
    )
    if isinstance(validity_floor, (int, float)) and math.isfinite(validity_floor):
        valid &= freqs >= float(validity_floor)
    record = {
        "role": primary.role,
        "driver_identity": dict(driver_identity),
        "capture_id": capture_id,
        "take_id": f"{ANCHOR_POSE_ID}_take_{attempt:02d}",
        "ordered_response_ids": response_ids,
        "responses": response_records,
        "nominal_complex_response": _complex_record(nominal),
        "nominal_magnitude_db": _finite_float_list(
            20.0 * np.log10(np.maximum(np.abs(nominal), 1e-12))
        ),
        "valid_mask": np.asarray(valid, dtype=bool).tolist(),
        "trust_band_hz": [band_lo, band_hi],
        "validity_floor_hz": (
            float(validity_floor)
            if isinstance(validity_floor, (int, float)) and math.isfinite(validity_floor)
            else None
        ),
        "snr": dict(primary.snr) if isinstance(primary.snr, Mapping) else None,
        "repeat_uncertainty_db": _finite_float_list(uncertainty),
    }
    return record, tuple(response_ids)


def build_raw_evidence_v1(
    *,
    session_id: str,
    analysis: Any,
    program: Any,
    capture_wav: bytes,
    attempt: int,
    roles_bands: Sequence[Any],
    measurement_graph: Mapping[str, Any],
    protection_by_role: Mapping[str, Sequence[LinearTransferSection]],
    polarity_sign_by_role: Mapping[str, int],
    configured_fc_hz: float,
    component_safety_profile: Mapping[str, Any],
    calibration_identity: Mapping[str, Any],
    limiter_threshold_dbfs: float,
) -> dict[str, Any]:
    """Build and immediately validate one anchor-only v1 RAW evidence set."""

    if not isinstance(capture_wav, (bytes, bytearray)) or not capture_wav:
        raise RawEvidenceError("raw evidence requires the exact capture WAV bytes")
    if set(getattr(analysis, "transfer_composition_fingerprints", {})) != set(
        ROLE_ORDER
    ):
        raise RawEvidenceError(
            "accepted raw evidence requires configured-Fc M*C/P composition"
        )
    capture_id = hashlib.sha256(bytes(capture_wav)).hexdigest()
    role_map = {str(item.role): item for item in roles_bands}
    response_map = {str(item.role): item for item in analysis.driver_responses}
    if tuple(role_map) != ROLE_ORDER or set(response_map) != set(ROLE_ORDER):
        raise RawEvidenceError("raw evidence requires ordered woofer/tweeter responses")
    driver_identities, legal_bounds = _component_role_contracts(
        component_safety_profile,
        configured_fc_hz=float(configured_fc_hz),
    )
    frequency_basis = np.asarray(response_map[ROLE_ORDER[0]].freqs_hz, dtype=np.float64)
    if frequency_basis.ndim != 1 or frequency_basis.size < 2 or not np.all(
        np.isfinite(frequency_basis)
    ) or not np.all(np.diff(frequency_basis) > 0.0):
        raise RawEvidenceError("frequency basis must be finite and strictly increasing")
    if not np.array_equal(
        frequency_basis, np.asarray(response_map[ROLE_ORDER[1]].freqs_hz, dtype=np.float64)
    ):
        raise RawEvidenceError("woofer and tweeter responses use different frequency bases")

    drivers: list[dict[str, Any]] = []
    response_ids_by_role: dict[str, tuple[str, ...]] = {}
    protection_records: list[dict[str, Any]] = []
    for role in ROLE_ORDER:
        record, ids = _driver_record(
            response_map[role],
            session_id=session_id,
            capture_id=capture_id,
            attempt=attempt,
            role_band=role_map[role],
            driver_identity=driver_identities[role],
        )
        drivers.append(record)
        response_ids_by_role[role] = ids
        P = linkwitz_riley_response_complex(
            frequency_basis,
            protection_by_role[role],
            polarity_sign=int(polarity_sign_by_role[role]),
        )
        protection_records.append(
            {
                "role": role,
                "filter_identity": {
                    "sections": [section.to_dict() for section in protection_by_role[role]],
                    "polarity_sign": int(polarity_sign_by_role[role]),
                    "fingerprint": json_fingerprint(
                        {
                            "schema_version": 1,
                            "kind": "jts_crossover_role_measurement_protection",
                            "role": role,
                            "sections": [
                                section.to_dict()
                                for section in protection_by_role[role]
                            ],
                            "polarity_sign": int(polarity_sign_by_role[role]),
                        }
                    ),
                },
                "complex_transfer_P": _complex_record(P),
            }
        )

    repeat_groups = [
        {
            "group_id": f"selector_repeat_{index:02d}",
            "stimulus_order": index,
            "accepted_order": index,
            "woofer_response_id": response_ids_by_role["woofer"][index],
            "tweeter_response_id": response_ids_by_role["tweeter"][index],
            "woofer_capture_id": capture_id,
            "tweeter_capture_id": capture_id,
            "woofer_take_id": f"{ANCHOR_POSE_ID}_take_{attempt:02d}",
            "tweeter_take_id": f"{ANCHOR_POSE_ID}_take_{attempt:02d}",
        }
        for index in range(ANCHOR_REPEAT_GROUP_COUNT)
    ]
    drift = analysis.drift
    alignment = analysis.alignment
    candidate = analysis.candidate
    locations = [
        {
            "segment_id": item.segment_id,
            "scheduled_start": int(item.scheduled_start),
            "located_start": int(item.located_start),
            "residual_samples": float(item.residual_samples),
            "confidence": float(item.confidence),
        }
        for item in analysis.locations
    ]
    gains = {
        role: float(program.segment("sweep_w" if role == "woofer" else "sweep_t").gain_db)
        for role in ROLE_ORDER
    }
    ledger_core = {
        "schema_version": 1,
        "kind": "jts_crossover_timing_gain_ledger_v1",
        "located_timing_corrections": locations,
        "inter_driver_drift_correction": {
            "epsilon_ppm": float(drift.epsilon_ppm) if drift else None,
            "per_role_epsilon_ppm": dict(drift.per_role_epsilon_ppm) if drift else {},
        },
        "stimulus_gain_db_by_role": gains,
        "capture_gain_level_solve_db_by_role": gains,
        "anchor_frame": {
            "trim_db": dict(candidate.trim_db) if candidate else None,
            "delay_us": float(alignment.delay_us) if alignment else None,
            "polarity": alignment.polarity if alignment else None,
        },
    }
    ledger = {**ledger_core, "fingerprint": json_fingerprint(ledger_core)}

    integrity = analysis.capture_integrity
    clipped_segments = (
        list(integrity.clipped_segments)
        if integrity is not None
        else [item.segment_id for item in analysis.locations if item.clipped]
    )
    maximum_effective_peak = max(
        float(segment.effective_peak_dbfs)
        for segment in program.stimulus_segments()
    )
    limiter_linear = maximum_effective_peak < float(limiter_threshold_dbfs)
    capture_linear = analysis.linearity_ok is True and not clipped_segments
    if not limiter_linear or not capture_linear:
        raise RawEvidenceError(
            "accepted raw evidence must prove linear capture and limiter non-engagement"
        )
    limiter_proof_core = {
        "schema_version": 1,
        "kind": "jts_crossover_limiter_linearity_proof_v1",
        "limiter_threshold_dbfs": float(limiter_threshold_dbfs),
        "maximum_effective_stimulus_peak_dbfs": maximum_effective_peak,
        "threshold_margin_db": float(limiter_threshold_dbfs) - maximum_effective_peak,
        "boundary_rule": "stimulus_peak_must_be_strictly_below_threshold",
        "limiter_engaged": False,
        "capture_linearity_passed": True,
        "clipped_segments": clipped_segments,
    }
    limiter_proof = {
        **limiter_proof_core,
        "fingerprint": json_fingerprint(limiter_proof_core),
    }

    safety_fp = component_safety_profile.get("profile_fingerprint")
    if not _is_sha256(safety_fp):
        raise RawEvidenceError(
            "component safety profile requires its confirmed canonical fingerprint"
        )
    component_core = {
        "schema_version": 1,
        "kind": "jts_crossover_component_safety_profile_identity",
        "profile_fingerprint": safety_fp,
        "targets_by_role": driver_identities,
    }
    component_identity = {
        **component_core,
        "fingerprint": json_fingerprint(component_core),
    }
    calibration_core = {
        "schema_version": 1,
        "kind": "jts_crossover_calibration_identity",
        **dict(calibration_identity),
    }
    calibration = {
        **calibration_core,
        "fingerprint": json_fingerprint(calibration_core),
    }
    stimulus_core = {
        "schema_version": 1,
        "kind": "jts_crossover_stimulus_identity",
        "program_id": str(program.program_id),
        "capture_wav_sha256": capture_id,
        "attempt": int(attempt),
    }
    stimulus = {**stimulus_core, "fingerprint": json_fingerprint(stimulus_core)}
    legal_lo, legal_hi = legal_bounds
    session_core = {
        "schema_version": 1,
        "kind": "jts_crossover_raw_session_identity",
        "session_id": session_id,
        "measurement_graph_fingerprint": measurement_graph.get("fingerprint"),
        "component_safety_identity_fingerprint": component_identity["fingerprint"],
        "calibration_fingerprint": calibration["fingerprint"],
        "stimulus_program_id": str(program.program_id),
    }
    session_fingerprint = json_fingerprint(session_core)
    evidence_set_id = f"{session_id}:{ANCHOR_POSE_ID}"
    core = {
        "schema_version": RAW_EVIDENCE_SCHEMA_VERSION,
        "kind": RAW_EVIDENCE_KIND,
        "session_id": session_id,
        "session_fingerprint": session_fingerprint,
        "evidence_set_id": evidence_set_id,
        "measurement_graph": dict(measurement_graph),
        "measurement_protection_by_role": protection_records,
        "total_transfer_composition_policy": transfer_policy_payload(),
        "component_safety_profile_identity": component_identity,
        "calibration_identity": calibration,
        "stimulus_identity": stimulus,
        "sample_rate_hz": int(program.sample_rate_hz),
        "frequency_basis_hz": _finite_float_list(frequency_basis),
        "configured_lr4_fc_hz": float(configured_fc_hz),
        "legal_fc_bounds_hz": [legal_lo, legal_hi],
        "poses": [
            {
                "pose_id": ANCHOR_POSE_ID,
                "order": 0,
                "lateral_offset_cm": 0.0,
                "pose_role": "anchor",
                "evidence_role": "design_axis",
                "capture_id": capture_id,
                "take_id": f"{ANCHOR_POSE_ID}_take_{attempt:02d}",
                "timing_gain_ledger": ledger,
                "selector_repeat_groups": repeat_groups,
                "drivers": {item["role"]: item for item in drivers},
                "linearity_limiter_proof": limiter_proof,
            }
        ],
    }
    evidence_set_fingerprint = json_fingerprint(core)
    payload = {
        **core,
        "evidence_set_fingerprint": evidence_set_fingerprint,
    }
    validate_raw_evidence_v1(payload, expected_session_id=session_id)
    return payload


_RAW_TOP_LEVEL_FIELDS = frozenset(
    {
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
)
_GRAPH_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "graph_id",
        "active_raw_sha256",
        "exact_dsp_state_identity",
        "protection_proof_identity",
        "role_channels",
        "summed_channel",
        "polarity_sign_by_role",
        "lifecycle",
        "fingerprint",
    }
)
_GRAPH_LIFECYCLE = {
    "load": "inline_transient_under_existing_dsp_writer_lock",
    "restore": "exact_entry_config_on_success_abort_or_playback_failure",
    "durable_profile_mutated": False,
    "undo_mutated": False,
    "process_restart_target": "unchanged_persisted_entry_config",
}
_PROTECTION_RECORD_FIELDS = frozenset(
    {"role", "filter_identity", "complex_transfer_P"}
)
_PROTECTION_IDENTITY_FIELDS = frozenset(
    {"sections", "polarity_sign", "fingerprint"}
)
_PROTECTION_PROOF_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "protection_sections_by_role",
        "protection_sections_fingerprint",
        "limiter",
        "volume_ceiling_db",
        "excluded_filter_layers",
        "fingerprint",
    }
)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
_DRIVER_FIELDS = frozenset(
    {
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
        "snr",
        "repeat_uncertainty_db",
    }
)
_RESPONSE_FIELDS = frozenset(
    {"response_id", "repeat_index", "complex_response"}
)
_COMPLEX_FIELDS = frozenset({"real", "imag", "fingerprint"})
_GROUP_FIELDS = frozenset(
    {
        "group_id",
        "stimulus_order",
        "accepted_order",
        "woofer_response_id",
        "tweeter_response_id",
        "woofer_capture_id",
        "tweeter_capture_id",
        "woofer_take_id",
        "tweeter_take_id",
    }
)
_POSE_FIELDS = frozenset(
    {
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
)
_POSE_CONTRACTS = (
    ("anchor_00", 0, 0.0, "anchor", "design_axis"),
    ("left_12_cm", 1, -12.0, "lateral", "onax"),
    ("right_12_cm", 2, 12.0, "lateral", "onax"),
    ("left_40_cm", 3, -40.0, "lateral", "offax"),
    ("right_40_cm", 4, 40.0, "lateral", "offax"),
)


def _validate_raw_pose_v1(
    pose: Mapping[str, Any],
    *,
    session_id: str,
    contract: tuple[str, int, float, str, str],
    freqs: np.ndarray,
    component_identity: Mapping[str, Any],
    graph_limiter_threshold_dbfs: float,
) -> None:
    pose_id, order, offset_cm, pose_role, evidence_role = contract
    if set(pose) != _POSE_FIELDS or (
        pose.get("pose_id"),
        pose.get("order"),
        pose.get("lateral_offset_cm"),
        pose.get("pose_role"),
        pose.get("evidence_role"),
    ) != (pose_id, order, offset_cm, pose_role, evidence_role):
        raise RawEvidenceError("raw pose schema or ordered identity is invalid")
    if not _is_sha256(pose.get("capture_id")):
        raise RawEvidenceError("raw pose capture identity is invalid")
    if not isinstance(pose.get("take_id"), str) or not pose["take_id"]:
        raise RawEvidenceError("raw pose take identity is invalid")

    groups = pose.get("selector_repeat_groups")
    if (
        not isinstance(groups, list)
        or len(groups) != ANCHOR_REPEAT_GROUP_COUNT
        or not all(
            isinstance(item, Mapping) and set(item) == _GROUP_FIELDS
            for item in groups
        )
    ):
        raise RawEvidenceError("raw pose must contain exactly three paired groups")
    if [item.get("accepted_order") for item in groups] != list(
        range(ANCHOR_REPEAT_GROUP_COUNT)
    ) or [item.get("stimulus_order") for item in groups] != list(
        range(ANCHOR_REPEAT_GROUP_COUNT)
    ):
        raise RawEvidenceError("raw pose groups are not in accepted stimulus order")

    ledger = pose["timing_gain_ledger"]
    proof = pose["linearity_limiter_proof"]
    for item, label in ((ledger, "timing/gain ledger"), (proof, "linearity proof")):
        if not isinstance(item, Mapping):
            raise RawEvidenceError(f"{label} is invalid")
        item_core = {key: value for key, value in item.items() if key != "fingerprint"}
        if item.get("fingerprint") != json_fingerprint(item_core):
            raise RawEvidenceError(f"{label} fingerprint mismatch")
    if proof.get("limiter_engaged") is not False or proof.get(
        "capture_linearity_passed"
    ) is not True:
        raise RawEvidenceError("accepted evidence lacks linear limiter proof")
    try:
        limiter_threshold = float(proof["limiter_threshold_dbfs"])
        maximum_peak = float(proof["maximum_effective_stimulus_peak_dbfs"])
        threshold_margin = float(proof["threshold_margin_db"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RawEvidenceError("accepted evidence limiter proof is malformed") from exc
    if (
        not all(math.isfinite(value) for value in (limiter_threshold, maximum_peak))
        or maximum_peak >= limiter_threshold
        or threshold_margin != limiter_threshold - maximum_peak
        or proof.get("boundary_rule")
        != "stimulus_peak_must_be_strictly_below_threshold"
        or proof.get("clipped_segments") != []
    ):
        raise RawEvidenceError("accepted evidence limiter proof is inconsistent")
    if limiter_threshold != graph_limiter_threshold_dbfs:
        raise RawEvidenceError("limiter proof and measurement graph are mixed")

    drivers_map = pose["drivers"]
    if not isinstance(drivers_map, Mapping) or set(drivers_map) != set(ROLE_ORDER):
        raise RawEvidenceError("raw driver roles are missing or reordered")
    drivers = [drivers_map[role] for role in ROLE_ORDER]
    if [item.get("role") for item in drivers] != list(ROLE_ORDER):
        raise RawEvidenceError("raw driver role identities are mixed")
    ids_by_role: dict[str, list[str]] = {}
    for driver in drivers:
        if not isinstance(driver, Mapping) or set(driver) != _DRIVER_FIELDS:
            raise RawEvidenceError("raw driver schema mismatch")
        identity = driver.get("driver_identity")
        expected_identity = component_identity.get("targets_by_role", {}).get(
            driver.get("role")
        )
        if not isinstance(identity, Mapping) or identity != expected_identity:
            raise RawEvidenceError("raw driver target identity is mixed")
        response_ids = driver.get("ordered_response_ids")
        responses = driver.get("responses")
        if not isinstance(response_ids, list) or len(response_ids) != 3:
            raise RawEvidenceError("raw driver response identities are incomplete")
        expected_response_ids = [
            _response_id(
                session_id=session_id,
                capture_id=pose["capture_id"],
                role=str(driver["role"]),
                repeat_index=index,
            )
            for index in range(ANCHOR_REPEAT_GROUP_COUNT)
        ]
        if (
            not isinstance(responses, list)
            or not all(
                isinstance(item, Mapping) and set(item) == _RESPONSE_FIELDS
                for item in responses
            )
            or [item.get("response_id") for item in responses] != response_ids
            or response_ids != expected_response_ids
            or [item.get("repeat_index") for item in responses] != [0, 1, 2]
        ):
            raise RawEvidenceError("raw driver response identities are mixed")
        ids_by_role[str(driver["role"])] = response_ids
        if driver.get("capture_id") != pose["capture_id"] or driver.get(
            "take_id"
        ) != pose["take_id"]:
            raise RawEvidenceError("raw driver capture/take identity is mixed")
        complex_rows: list[np.ndarray] = []
        for response in responses:
            complex_response = response.get("complex_response")
            if (
                not isinstance(complex_response, Mapping)
                or set(complex_response) != _COMPLEX_FIELDS
            ):
                raise RawEvidenceError("raw complex response is missing")
            if len(complex_response.get("real", ())) != freqs.size or len(
                complex_response.get("imag", ())
            ) != freqs.size:
                raise RawEvidenceError("raw complex response basis length mismatch")
            values = np.asarray(complex_response["real"], dtype=np.float64) + 1j * np.asarray(
                complex_response["imag"], dtype=np.float64
            )
            if (
                not np.all(np.isfinite(values.real))
                or not np.all(np.isfinite(values.imag))
                or complex_response.get("fingerprint")
                != complex_fingerprint(values, kind="jts_crossover_complex_response")
            ):
                raise RawEvidenceError("raw complex response fingerprint mismatch")
            complex_rows.append(values)
        nominal_record = driver["nominal_complex_response"]
        if (
            not isinstance(nominal_record, Mapping)
            or set(nominal_record) != _COMPLEX_FIELDS
        ):
            raise RawEvidenceError("nominal complex response schema mismatch")
        nominal = np.asarray(nominal_record["real"], dtype=np.float64) + 1j * np.asarray(
            nominal_record["imag"], dtype=np.float64
        )
        if (
            nominal.shape != freqs.shape
            or nominal_record.get("fingerprint")
            != complex_fingerprint(nominal, kind="jts_crossover_complex_response")
        ):
            raise RawEvidenceError("nominal complex response fingerprint mismatch")
        expected_nominal = np.mean(np.stack(complex_rows, axis=0), axis=0)
        if not np.array_equal(nominal, expected_nominal):
            raise RawEvidenceError("nominal response is not the arithmetic complex mean")
        expected_magnitude = 20.0 * np.log10(np.maximum(np.abs(nominal), 1e-12))
        if not np.array_equal(
            np.asarray(driver["nominal_magnitude_db"], dtype=np.float64),
            expected_magnitude,
        ):
            raise RawEvidenceError("nominal magnitude is not derived from complex response")
        if (
            len(driver.get("valid_mask", ())) != freqs.size
            or not all(type(value) is bool for value in driver["valid_mask"])
            or not any(driver["valid_mask"])
            or len(driver.get("repeat_uncertainty_db", ())) != freqs.size
            or not np.all(
                np.isfinite(
                    np.asarray(driver["repeat_uncertainty_db"], dtype=np.float64)
                )
            )
        ):
            raise RawEvidenceError("raw driver validity mask basis length mismatch")
        if not isinstance(driver.get("snr"), Mapping):
            raise RawEvidenceError("raw driver SNR evidence is missing")

    for index, group in enumerate(groups):
        if group.get("woofer_response_id") != ids_by_role["woofer"][index] or group.get(
            "tweeter_response_id"
        ) != ids_by_role["tweeter"][index]:
            raise RawEvidenceError("raw paired group references mixed responses")
        if any(
            group.get(key) != pose[value]
            for key, value in (
                ("woofer_capture_id", "capture_id"),
                ("tweeter_capture_id", "capture_id"),
                ("woofer_take_id", "take_id"),
                ("tweeter_take_id", "take_id"),
            )
        ):
            raise RawEvidenceError("raw paired group capture/take identity is mixed")


def validate_raw_evidence_v1(
    raw: Mapping[str, Any],
    *,
    expected_session_id: str | None = None,
    expected_graph_fingerprint: str | None = None,
    expected_safety_fingerprint: str | None = None,
    expected_calibration_fingerprint: str | None = None,
    expected_stimulus_fingerprint: str | None = None,
) -> None:
    """Strictly reject schema drift, stale sessions, and mixed identities."""

    if not isinstance(raw, Mapping) or set(raw) != _RAW_TOP_LEVEL_FIELDS:
        raise RawEvidenceError("raw evidence has unknown or missing top-level fields")
    if raw["schema_version"] != RAW_EVIDENCE_SCHEMA_VERSION or raw["kind"] != RAW_EVIDENCE_KIND:
        raise RawEvidenceError("raw evidence schema or kind mismatch")
    session_id = raw["session_id"]
    if not isinstance(session_id, str) or not session_id:
        raise RawEvidenceError("raw evidence session identity is invalid")
    if expected_session_id is not None and session_id != expected_session_id:
        raise RawEvidenceError("raw evidence belongs to a stale session")
    if raw["evidence_set_id"] != f"{session_id}:{ANCHOR_POSE_ID}":
        raise RawEvidenceError("raw evidence set identity is mixed")
    core = {
        key: value
        for key, value in raw.items()
        if key != "evidence_set_fingerprint"
    }
    expected_fp = json_fingerprint(core)
    if raw["evidence_set_fingerprint"] != expected_fp:
        raise RawEvidenceError("raw evidence fingerprint mismatch")
    graph = raw["measurement_graph"]
    if (
        not isinstance(graph, Mapping)
        or set(graph) != _GRAPH_FIELDS
        or graph.get("kind") != PROTECTED_NEUTRAL_GRAPH_KIND
        or graph.get("lifecycle") != _GRAPH_LIFECYCLE
        or not _is_sha256(graph.get("active_raw_sha256"))
    ):
        raise RawEvidenceError("measurement graph identity is not protected-neutral v1")
    graph_core = {key: value for key, value in graph.items() if key != "fingerprint"}
    if graph.get("fingerprint") != json_fingerprint(graph_core):
        raise RawEvidenceError("measurement graph fingerprint mismatch")
    try:
        exact_state = ExactDspStateIdentity.from_mapping(
            graph["exact_dsp_state_identity"]
        )
    except (EvidenceIdentityError, KeyError, TypeError, ValueError) as exc:
        raise RawEvidenceError("measurement graph exact DSP identity is invalid") from exc
    if exact_state.state.get("active_raw_sha256") != graph.get("active_raw_sha256"):
        raise RawEvidenceError("measurement graph exact DSP identity is mixed")
    proof_identity = graph["protection_proof_identity"]
    if not isinstance(proof_identity, Mapping) or set(
        proof_identity
    ) != _PROTECTION_PROOF_FIELDS:
        raise RawEvidenceError("measurement graph protection proof is invalid")
    proof_core = {
        key: value for key, value in proof_identity.items() if key != "fingerprint"
    }
    canonical_policy = transfer_policy_payload()
    graph_limiter = proof_identity.get("limiter")
    if (
        proof_identity.get("fingerprint") != json_fingerprint(proof_core)
        or proof_identity.get("excluded_filter_layers")
        != list(PROTECTED_NEUTRAL_GRAPH_EXCLUSIONS)
        or not isinstance(graph_limiter, Mapping)
        or set(graph_limiter)
        != {"type", "threshold_dbfs", "excluded_from_linear_transfer_P"}
        or graph_limiter.get("type") != "soft_clip"
        or graph_limiter.get("excluded_from_linear_transfer_P") is not True
        or not isinstance(graph_limiter.get("threshold_dbfs"), (int, float))
        or not math.isfinite(float(graph_limiter["threshold_dbfs"]))
        or not isinstance(proof_identity.get("volume_ceiling_db"), (int, float))
        or not math.isfinite(float(proof_identity["volume_ceiling_db"]))
        or float(proof_identity["volume_ceiling_db"]) > 0.0
    ):
        raise RawEvidenceError("measurement graph protection proof mismatch")
    if (
        expected_graph_fingerprint is not None
        and graph.get("fingerprint") != expected_graph_fingerprint
    ):
        raise RawEvidenceError("measurement graph belongs to a stale session")
    for field, expected in (
        ("component_safety_profile_identity", expected_safety_fingerprint),
        ("calibration_identity", expected_calibration_fingerprint),
        ("stimulus_identity", expected_stimulus_fingerprint),
    ):
        identity = raw[field]
        if not isinstance(identity, Mapping) or not identity.get("fingerprint"):
            raise RawEvidenceError(f"{field} is invalid")
        identity_core = {
            key: value for key, value in identity.items() if key != "fingerprint"
        }
        if identity["fingerprint"] != json_fingerprint(identity_core):
            raise RawEvidenceError(f"{field} fingerprint mismatch")
        if expected is not None and identity["fingerprint"] != expected:
            raise RawEvidenceError(f"{field} belongs to a stale session")
    policy = raw["total_transfer_composition_policy"]
    if not isinstance(policy, Mapping) or policy != canonical_policy:
        raise RawEvidenceError("total-transfer composition policy is invalid")
    session_core = {
        "schema_version": 1,
        "kind": "jts_crossover_raw_session_identity",
        "session_id": session_id,
        "measurement_graph_fingerprint": graph.get("fingerprint"),
        "component_safety_identity_fingerprint": raw[
            "component_safety_profile_identity"
        ].get("fingerprint"),
        "calibration_fingerprint": raw["calibration_identity"].get("fingerprint"),
        "stimulus_program_id": raw["stimulus_identity"].get("program_id"),
    }
    if raw["session_fingerprint"] != json_fingerprint(session_core):
        raise RawEvidenceError("raw evidence session fingerprint mismatch")
    roles = raw["measurement_protection_by_role"]
    if (
        not isinstance(roles, list)
        or not all(isinstance(item, Mapping) for item in roles)
        or [item.get("role") for item in roles] != list(ROLE_ORDER)
    ):
        raise RawEvidenceError("measurement protection roles are missing or reordered")
    role_channels = graph.get("role_channels")
    signs = graph.get("polarity_sign_by_role")
    summed_channel = graph.get("summed_channel")
    if (
        not isinstance(role_channels, Mapping)
        or set(role_channels) != set(ROLE_ORDER)
        or not all(type(role_channels[role]) is int for role in ROLE_ORDER)
        or len(set(role_channels.values())) != len(ROLE_ORDER)
        or not isinstance(signs, Mapping)
        or set(signs) != set(ROLE_ORDER)
        or any(signs[role] not in {-1, 1} for role in ROLE_ORDER)
        or type(summed_channel) is not int
        or summed_channel in role_channels.values()
        or sorted((*role_channels.values(), summed_channel)) != [0, 1, 2]
    ):
        raise RawEvidenceError("measurement graph routing identity is invalid")
    freqs = np.asarray(raw["frequency_basis_hz"], dtype=np.float64)
    if freqs.ndim != 1 or freqs.size < 2 or not np.all(np.isfinite(freqs)) or not np.all(
        np.diff(freqs) > 0.0
    ):
        raise RawEvidenceError("frequency basis is not finite and strictly increasing")
    if type(raw["sample_rate_hz"]) is not int or raw["sample_rate_hz"] <= 0:
        raise RawEvidenceError("sample rate is invalid")
    bounds = raw["legal_fc_bounds_hz"]
    configured_fc = raw["configured_lr4_fc_hz"]
    if (
        not isinstance(bounds, list)
        or len(bounds) != 2
        or not all(isinstance(value, (int, float)) for value in bounds)
        or not all(math.isfinite(float(value)) and float(value) > 0 for value in bounds)
        or not float(bounds[0]) <= float(configured_fc) <= float(bounds[1])
    ):
        raise RawEvidenceError("configured Fc or legal bounds are invalid")
    parsed_sections_by_role: dict[str, tuple[LinearTransferSection, ...]] = {}
    for protection in roles:
        transfer = protection.get("complex_transfer_P")
        identity = protection.get("filter_identity")
        if (
            set(protection) != _PROTECTION_RECORD_FIELDS
            or not isinstance(transfer, Mapping)
            or not isinstance(identity, Mapping)
            or set(identity) != _PROTECTION_IDENTITY_FIELDS
        ):
            raise RawEvidenceError("measurement protection transfer is invalid")
        values = np.asarray(transfer.get("real"), dtype=np.float64) + 1j * np.asarray(
            transfer.get("imag"), dtype=np.float64
        )
        if (
            set(transfer) != _COMPLEX_FIELDS
            or values.shape != freqs.shape
            or not np.all(np.isfinite(values.real))
            or not np.all(np.isfinite(values.imag))
            or transfer.get("fingerprint")
            != complex_fingerprint(values, kind="jts_crossover_complex_response")
        ):
            raise RawEvidenceError("measurement protection transfer fingerprint mismatch")
        filter_core = {
            "schema_version": 1,
            "kind": "jts_crossover_role_measurement_protection",
            "role": protection.get("role"),
            "sections": identity.get("sections"),
            "polarity_sign": identity.get("polarity_sign"),
        }
        if identity.get("fingerprint") != json_fingerprint(filter_core):
            raise RawEvidenceError("measurement protection filter identity mismatch")
        role = protection["role"]
        graph_sections = proof_identity.get("protection_sections_by_role", {}).get(
            role
        )
        graph_sign = graph.get("polarity_sign_by_role", {}).get(role)
        if identity.get("sections") != graph_sections or identity.get(
            "polarity_sign"
        ) != graph_sign:
            raise RawEvidenceError("measurement protection and graph identities are mixed")
        try:
            sections = tuple(
                LinearTransferSection.from_mapping(section)
                for section in identity["sections"]
            )
            emitted = linkwitz_riley_response_complex(
                freqs,
                sections,
                polarity_sign=int(identity["polarity_sign"]),
            )
        except (TypeError, ValueError) as exc:
            raise RawEvidenceError("measurement protection filter is invalid") from exc
        parsed_sections_by_role[role] = sections
        if not np.array_equal(values, emitted):
            raise RawEvidenceError("measurement protection P is not the emitted transfer")
    if proof_identity.get("protection_sections_fingerprint") != sections_fingerprint(
        parsed_sections_by_role,
        kind="jts_crossover_measurement_protection_sections",
    ):
        raise RawEvidenceError("measurement graph protection sections are mixed")
    poses = raw["poses"]
    if (
        not isinstance(poses, list)
        or len(poses) not in {1, len(_POSE_CONTRACTS)}
        or not all(isinstance(pose, Mapping) for pose in poses)
    ):
        raise RawEvidenceError("v1 raw evidence must contain anchor or all five poses")
    for pose, contract in zip(poses, _POSE_CONTRACTS):
        _validate_raw_pose_v1(
            pose,
            session_id=session_id,
            contract=contract,
            freqs=freqs,
            component_identity=raw["component_safety_profile_identity"],
            graph_limiter_threshold_dbfs=float(graph_limiter["threshold_dbfs"]),
        )
    if raw["stimulus_identity"].get("capture_wav_sha256") != poses[0]["capture_id"]:
        raise RawEvidenceError("stimulus and pose capture identities are mixed")


# Contract-only: R15 freezes the selector result envelope but does not select.
SELECTOR_RESULT_V1_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "evidence_set_id",
        "evidence_set_fingerprint",
        "grid_policy_fingerprint",
        "candidate_interval_bound_fingerprints",
        "candidate_interval_hz",
        "grid_fc",
        "grid_fingerprint",
        "metric_policy_fingerprint",
        "common_mask_fingerprint",
        "role_mask_fingerprints",
        "candidate_mask_fingerprints",
        "mask_bin_counts",
        "repeat_realization_policy_fingerprint",
        "comparison_policy_fingerprint",
        "candidate_records",
        "comparison_trace",
        "sensitivity_check_results",
        "outcome",
        "fingerprint",
    }
)
SELECTOR_CANDIDATE_V1_FIELDS = frozenset(
    {
        "fc_hz",
        "protection_transfer_fingerprint",
        "configured_transfer_fingerprint",
        "composition_policy_fingerprint",
        "required_mask_fingerprint",
        "transfer_ratio_fingerprint",
        "fitter_input_fingerprint",
        "complete_prescription_fingerprint",
        "scalar_metrics",
        "admissible",
        "inadmissibility_reason_codes",
    }
)
SELECTOR_SCALAR_KEYS = (
    "worst_anchor_branch_target_error_db",
    "anchor_absolute_sum_error_db",
    "worst_lateral_handoff_mismatch_db",
    "required_positive_headroom_db",
    "filter_biquad_count",
    "filter_absolute_gain_sum_db",
)
SELECTOR_DETERMINISTIC_SCALAR_KEYS = frozenset(SELECTOR_SCALAR_KEYS[3:])
SELECTOR_BOUND_KEYS = frozenset(
    {
        "component_policy",
        "measurement_protection",
        "measured_trust",
        "meaningful_contribution",
        "fit_authority",
    }
)
SELECTOR_OUTCOMES = frozenset({"selected", "abstained", "refused"})
SELECTOR_CANDIDATE_REASON_CODES = frozenset(
    {
        "unsupported_comparison_bins",
        "ill_conditioned_protection_deembedding",
        "stopband_correction_required",
        "protection_contract_violated",
        "headroom_contract_violated",
        "fit_failed",
        "runtime_contract_invalid",
        "incomplete_prescription",
    }
)
SELECTOR_OUTCOME_REASON_CODES = frozenset(
    {
        "unique_lexicographic_dominance",
        "metric_uncertainty_tie",
        "cyclic_comparison",
        "left_right_winner_instability",
        "leave_one_wide_side_winner_instability",
        "invalid_evidence_schema",
        "invalid_evidence_identity",
        "invalid_measurement_authority",
        "no_admissible_candidate",
        "configured_fc_inadmissible_for_abstention",
    }
)
SELECTOR_OUTCOME_V1_FIELDS = frozenset(
    {"status", "selected_fc_hz", "retained_fc_hz", "reason_codes"}
)


def validate_selector_result_v1(raw: Mapping[str, Any]) -> None:
    """Validate the frozen R15 selector envelope without implementing R17."""

    if not isinstance(raw, Mapping) or set(raw) != SELECTOR_RESULT_V1_FIELDS:
        raise RawEvidenceError("selector result has unknown or missing fields")
    if raw["schema_version"] != 1 or raw["kind"] != SELECTOR_RESULT_KIND:
        raise RawEvidenceError("selector result schema or kind mismatch")
    if not isinstance(raw["evidence_set_id"], str) or not raw[
        "evidence_set_id"
    ] or any(
        not _is_sha256(raw[field])
        for field in (
            "evidence_set_fingerprint",
            "grid_policy_fingerprint",
            "metric_policy_fingerprint",
            "common_mask_fingerprint",
            "repeat_realization_policy_fingerprint",
            "comparison_policy_fingerprint",
        )
    ):
        raise RawEvidenceError("selector result identity fingerprints are invalid")
    grid = raw["grid_fc"]
    if not isinstance(grid, list) or not grid or len(grid) > 5 or any(
        not isinstance(value, (int, float)) or not math.isfinite(float(value))
        for value in grid
    ) or any(float(b) <= float(a) for a, b in zip(grid, grid[1:])):
        raise RawEvidenceError("selector reporting grid must be finite and ascending")
    interval = raw["candidate_interval_hz"]
    if (
        not isinstance(interval, list)
        or len(interval) != 2
        or not all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in interval
        )
        or not 0.0 < float(interval[0]) <= float(interval[1])
        or not all(float(interval[0]) <= float(value) <= float(interval[1]) for value in grid)
    ):
        raise RawEvidenceError("selector candidate interval is invalid")
    bounds = raw["candidate_interval_bound_fingerprints"]
    if (
        not isinstance(bounds, Mapping)
        or set(bounds) != SELECTOR_BOUND_KEYS
        or not all(_is_sha256(value) for value in bounds.values())
    ):
        raise RawEvidenceError("selector candidate bound identities are incomplete")
    grid_core = {
        "schema_version": 1,
        "kind": "jts_crossover_selector_grid_v1",
        "grid_fc": grid,
    }
    if raw["grid_fingerprint"] != json_fingerprint(grid_core):
        raise RawEvidenceError("selector reporting grid fingerprint mismatch")
    role_masks = raw["role_mask_fingerprints"]
    candidate_masks = raw["candidate_mask_fingerprints"]
    if (
        not isinstance(role_masks, Mapping)
        or set(role_masks) != set(ROLE_ORDER)
        or not all(_is_sha256(value) for value in role_masks.values())
        or not isinstance(candidate_masks, Mapping)
        or not all(_is_sha256(value) for value in candidate_masks.values())
        or not isinstance(raw["mask_bin_counts"], Mapping)
    ):
        raise RawEvidenceError("selector mask identities are invalid")
    candidates = raw["candidate_records"]
    if not isinstance(candidates, list) or [item.get("fc_hz") for item in candidates] != grid:
        raise RawEvidenceError("selector candidates must follow ascending reporting order")
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != SELECTOR_CANDIDATE_V1_FIELDS:
            raise RawEvidenceError("selector candidate schema mismatch")
        if any(
            not _is_sha256(candidate[field])
            for field in (
                "protection_transfer_fingerprint",
                "configured_transfer_fingerprint",
                "composition_policy_fingerprint",
                "required_mask_fingerprint",
                "transfer_ratio_fingerprint",
                "fitter_input_fingerprint",
                "complete_prescription_fingerprint",
            )
        ):
            raise RawEvidenceError("selector candidate fingerprints are invalid")
        metrics = candidate["scalar_metrics"]
        if not isinstance(metrics, Mapping) or tuple(metrics) != SELECTOR_SCALAR_KEYS:
            raise RawEvidenceError("selector candidate scalar metric schema mismatch")
        for key, metric in metrics.items():
            if not isinstance(metric, Mapping) or set(metric) != {
                "value", "uncertainty_half_width"
            }:
                raise RawEvidenceError("selector metric uncertainty schema mismatch")
            value = metric["value"]
            uncertainty = metric["uncertainty_half_width"]
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not isinstance(uncertainty, (int, float))
                or not math.isfinite(float(uncertainty))
                or float(value) < 0.0
                or float(uncertainty) < 0.0
                or (
                    key in SELECTOR_DETERMINISTIC_SCALAR_KEYS
                    and float(uncertainty) != 0.0
                )
            ):
                raise RawEvidenceError("selector metric value is invalid")
        biquad_count = metrics["filter_biquad_count"]["value"]
        if type(biquad_count) is not int:
            raise RawEvidenceError("selector filter biquad count must be an integer")
        if not candidate["complete_prescription_fingerprint"]:
            raise RawEvidenceError("selector candidates may never carry partial prescriptions")
        admissible = candidate["admissible"]
        reasons = candidate["inadmissibility_reason_codes"]
        if (
            type(admissible) is not bool
            or not isinstance(reasons, list)
            or admissible == bool(reasons)
            or any(reason not in SELECTOR_CANDIDATE_REASON_CODES for reason in reasons)
        ):
            raise RawEvidenceError("selector candidate admissibility is inconsistent")
    if not isinstance(raw["comparison_trace"], list) or not isinstance(
        raw["sensitivity_check_results"], list
    ):
        raise RawEvidenceError("selector comparison or sensitivity evidence is invalid")
    outcome = raw["outcome"]
    if (
        not isinstance(outcome, Mapping)
        or set(outcome) != SELECTOR_OUTCOME_V1_FIELDS
        or outcome.get("status") not in SELECTOR_OUTCOMES
        or not isinstance(outcome.get("reason_codes"), list)
        or not outcome["reason_codes"]
        or any(
            reason not in SELECTOR_OUTCOME_REASON_CODES
            for reason in outcome["reason_codes"]
        )
    ):
        raise RawEvidenceError("selector outcome is invalid")
    status = outcome["status"]
    if (status == "selected") != (outcome.get("selected_fc_hz") is not None):
        raise RawEvidenceError("selected_fc_hz is legal only for selected outcomes")
    if (status == "abstained") != (outcome.get("retained_fc_hz") is not None):
        raise RawEvidenceError("retained_fc_hz is legal only for abstained outcomes")
    selected_or_retained = (
        outcome.get("selected_fc_hz")
        if status == "selected"
        else outcome.get("retained_fc_hz")
    )
    if selected_or_retained is not None and (
        not isinstance(selected_or_retained, (int, float))
        or not math.isfinite(float(selected_or_retained))
        or selected_or_retained not in grid
    ):
        raise RawEvidenceError("selector outcome Fc is not a reporting-grid candidate")
    core = {key: value for key, value in raw.items() if key != "fingerprint"}
    if raw["fingerprint"] != json_fingerprint(core):
        raise RawEvidenceError("selector result fingerprint mismatch")
