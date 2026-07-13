# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Strict, permanently non-authoritative replay of a current legacy winner.

Historical B2b captures predate Shared excitation admission. They remain useful
for diagnosing the response that was actually measured, but no content hash or
successful replay can upgrade them into candidate/apply/receipt authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper.correction.bundles import (
    CURRENT_ARTIFACT_MANIFEST_VERSION,
    BundleError,
    _relative_artifact_path,
    _sha256_file,
    read_artifact_manifest,
)
from jasper.output_topology import OutputTopology

from . import bundles, measurement
from .baseline_profile import baseline_candidate_fingerprint
from .capture_geometry import (
    DRIVER_PLACEMENT_POLICY_ID,
    REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
    capture_proof_valid,
    comparison_set_valid,
    driver_level_lock,
)
from .crossover_contract import (
    crossover_snapshot_state,
    preset_matches_applied_profile,
    verified_driver_excitation,
)
from .driver_acoustics import DriverAcousticsError, replay_driver_response
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset

LEGACY_REPLAY_KIND = "jts_active_speaker_legacy_winner_replay"
LEGACY_REPLAY_SCHEMA_VERSION = 1
LEGACY_REPLAY_ALGORITHM_ID = "active_legacy_winner_replay_v1"
LEGACY_EVIDENCE_CLASSIFICATION = "legacy_non_admitted"


class LegacyReplayError(ValueError):
    """A current winner cannot be resolved or replayed without guessing."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class LegacyWinnerEvidence:
    """Content-verified inputs for one exact current measurement winner."""

    target_id: str
    capture_geometry: str
    comparison_set_id: str
    comparison_set_fingerprint: str
    applied_profile_fingerprint: str
    session_id: str
    bundle_dir: Path
    wav_path: Path
    wav_relative_path: str
    analysis_relative_path: str
    wav_sha256: str
    analysis_sha256: str
    placement_proof_sha256: str
    calibration_id: str
    calibration_curve_sha256: str
    calibration_curve: Any
    excitation: dict[str, Any]
    sweep_meta: dict[str, Any]
    ambient_duration_s: float | None


@dataclass(frozen=True)
class LegacyReplayResult:
    """A diagnostic response whose non-authority cannot be omitted."""

    target_id: str
    capture_geometry: str
    source: dict[str, Any]
    response: dict[str, Any]
    quality: dict[str, Any]
    gating: dict[str, Any]

    def _semantic_payload(self) -> dict[str, Any]:
        # JSON round-trip both deep-copies the serialized result and guarantees
        # the fingerprint covers exactly the JSON semantics a caller receives.
        variable = json.loads(json.dumps({
            "source": self.source,
            "response": self.response,
            "quality": self.quality,
            "gating": self.gating,
        }))
        return {
            "schema_version": LEGACY_REPLAY_SCHEMA_VERSION,
            "kind": LEGACY_REPLAY_KIND,
            "algorithm_id": LEGACY_REPLAY_ALGORITHM_ID,
            "evidence_classification": LEGACY_EVIDENCE_CLASSIFICATION,
            "authoritative": False,
            "authorizes_candidate": False,
            "authorizes_apply": False,
            "authorizes_verification": False,
            "authorizes_receipt": False,
            "authorizes_playback": False,
            "target_id": self.target_id,
            "capture_geometry": self.capture_geometry,
            **variable,
        }

    @property
    def diagnostic_fingerprint(self) -> str:
        return _canonical_fingerprint(self._semantic_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = self._semantic_payload()
        payload["diagnostic_fingerprint"] = _canonical_fingerprint(payload)
        return payload


def _error(reason: str, detail: str) -> LegacyReplayError:
    return LegacyReplayError(reason, detail)


def _strict_number(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _canonical_fingerprint(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _topology_fingerprint(topology: OutputTopology) -> str:
    return measurement._fingerprint(
        {
            "topology_id": topology.topology_id,
            "hardware": measurement._hardware_payload(topology),
        }
    )


def _manifest_entries(
    bundle_dir: Path, manifest: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise _error("bundle_manifest_invalid", "artifact manifest has no list")
    by_path: dict[str, Mapping[str, Any]] = {}
    for raw in artifacts:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("path"), str):
            raise _error(
                "bundle_manifest_invalid", "artifact manifest entry is malformed"
            )
        try:
            relative = _relative_artifact_path(bundle_dir, raw["path"])
        except BundleError as exc:
            raise _error("bundle_manifest_invalid", str(exc)) from exc
        if relative != raw["path"] or relative in by_path:
            raise _error(
                "bundle_manifest_invalid",
                "artifact manifest paths must be canonical and unique",
            )
        by_path[relative] = raw
    return by_path


def _verified_artifact(
    bundle_dir: Path,
    entries: Mapping[str, Mapping[str, Any]],
    relative_path: str,
    *,
    kind: str,
    dependencies: list[str],
    schema_version: int | None,
) -> Mapping[str, Any]:
    entry = entries.get(relative_path)
    if not isinstance(entry, Mapping):
        raise _error("bundle_artifact_missing", f"manifest lacks {relative_path}")
    schema_valid = (
        type(entry.get("schema_version")) is int
        and entry.get("schema_version") == schema_version
        if schema_version is not None
        else "schema_version" not in entry
    )
    if (
        entry.get("kind") != kind
        or entry.get("dependencies") != dependencies
        or not schema_valid
        or not isinstance(entry.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        or type(entry.get("byte_size")) is not int
        or entry["byte_size"] < 0
    ):
        raise _error(
            "bundle_artifact_invalid", f"manifest entry for {relative_path} is invalid"
        )
    path = bundle_dir / relative_path
    try:
        stat = path.stat()
        actual_sha = _sha256_file(path)
    except OSError as exc:
        raise _error(
            "bundle_artifact_missing", f"could not read {relative_path}: {exc}"
        ) from exc
    if stat.st_size != entry["byte_size"] or actual_sha != entry["sha256"]:
        raise _error(
            "bundle_artifact_integrity_mismatch",
            f"size or hash mismatch for {relative_path}",
        )
    return entry


def _current_record(
    topology: OutputTopology,
    measurements: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
    capture_geometry: str,
) -> Mapping[str, Any]:
    summary = measurements.get("summary")
    if not isinstance(summary, Mapping):
        raise _error("measurement_state_invalid", "measurement summary is missing")
    key = (
        "latest_driver_measurements"
        if capture_geometry == "near_field"
        else "latest_reference_axis_driver_measurements"
    )
    latest = summary.get(key)
    target_id = f"{speaker_group_id}:{role}"
    record = latest.get(target_id) if isinstance(latest, Mapping) else None
    if not isinstance(record, Mapping):
        raise _error("current_winner_missing", "current target has no winning capture")
    targets = {
        str(item.get("target_id")): item
        for item in measurement.active_driver_targets(topology)
    }
    target = targets.get(target_id)
    if (
        target is None
        or record.get("target_id") != target_id
        or record.get("target_fingerprint") != target.get("target_fingerprint")
        or record.get("speaker_group_id") != speaker_group_id
        or record.get("role") != role
        or record.get("output_index") != target.get("output_index")
    ):
        raise _error("current_winner_stale", "winner does not match current target")
    return record


def resolve_legacy_current_winner(
    topology: OutputTopology,
    applied_profile: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
    capture_geometry: str = "near_field",
    sessions_root: Path | None = None,
) -> LegacyWinnerEvidence:
    """Resolve only the summary-selected current winner and its exact sidecar."""

    if capture_geometry not in {"near_field", "reference_axis"}:
        raise _error("capture_geometry_invalid", "unsupported capture geometry")
    topology_fp = _topology_fingerprint(topology)
    snapshot_state = crossover_snapshot_state(
        applied_profile,
        expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=topology_fp,
    )
    if snapshot_state.get("valid") is not True:
        raise _error(
            "applied_crossover_invalid",
            str(snapshot_state.get("reason") or "applied crossover is invalid"),
        )
    snapshot = applied_profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping):
        raise _error("applied_crossover_invalid", "applied snapshot is missing")
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
        preset.validate()
    except (ActiveSpeakerConfigError, TypeError, ValueError) as exc:
        raise _error("applied_crossover_invalid", str(exc)) from exc
    if not preset_matches_applied_profile(preset, applied_profile):
        raise _error("applied_crossover_mismatch", "applied preset is not exact")

    comparison = measurements.get("active_comparison_set")
    profile_fingerprint = baseline_candidate_fingerprint(applied_profile)
    declared_profile_fingerprint = str(
        applied_profile.get("candidate_fingerprint") or ""
    )
    if (
        not comparison_set_valid(comparison)
        or not isinstance(comparison, Mapping)
        or comparison.get("topology_id") != topology.topology_id
        or declared_profile_fingerprint != profile_fingerprint
        or comparison.get("profile_context_id") != profile_fingerprint
    ):
        raise _error(
            "comparison_context_invalid",
            "active comparison is malformed or belongs to another applied profile",
        )
    comparison = dict(comparison)
    record = _current_record(
        topology,
        measurements,
        speaker_group_id=speaker_group_id,
        role=role,
        capture_geometry=capture_geometry,
    )
    target_id = f"{speaker_group_id}:{role}"
    target = next(
        item
        for item in measurement.active_driver_targets(topology)
        if item.get("target_id") == target_id
    )
    policy_id = (
        DRIVER_PLACEMENT_POLICY_ID
        if capture_geometry == "near_field"
        else REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID
    )
    if (
        record.get("captured") is not True
        or record.get("outcome") != "heard_correct_driver"
        or not capture_proof_valid(
            record,
            comparison,
            policy_id=policy_id,
            role=role,
            speaker_group_id=speaker_group_id,
            target_fingerprint=str(target.get("target_fingerprint") or ""),
        )
    ):
        raise _error(
            "capture_binding_mismatch",
            "winner does not belong to the current target and geometry",
        )
    acoustic = record.get("acoustic")
    repeats = record.get("repeats")
    repeat_entries = repeats.get("per_repeat") if isinstance(repeats, Mapping) else None
    if (
        not isinstance(acoustic, Mapping)
        or acoustic.get("capture_geometry") != capture_geometry
        or acoustic.get("verdict") != "present"
        or not isinstance(repeats, Mapping)
        or not isinstance(repeat_entries, list)
        or repeats.get("target") != 3
        or repeats.get("accepted") not in {2, 3}
        or not 2 <= len(repeat_entries) <= 4
    ):
        raise _error("capture_analysis_invalid", "winner analysis is not usable")
    if not all(
        isinstance(item, Mapping)
        and type(item.get("attempt")) is int
        and isinstance(item.get("accepted"), bool)
        for item in repeat_entries
    ):
        raise _error("capture_analysis_invalid", "repeat ledger is malformed")

    bundle_ref = record.get("bundle")
    if not isinstance(bundle_ref, Mapping) or set(bundle_ref) != {
        "session_id",
        "artifact_path",
    }:
        raise _error("bundle_pointer_invalid", "winner has no exact bundle pointer")
    session_id = bundle_ref.get("session_id")
    raw_wav_path = bundle_ref.get("artifact_path")
    if (
        not isinstance(session_id, str)
        or re.fullmatch(r"[0-9a-f]{12}", session_id) is None
        or not isinstance(raw_wav_path, str)
        or comparison.get("bundle_session_id") != session_id
    ):
        raise _error("bundle_pointer_invalid", "winner pointer is outside its session")
    root = sessions_root if sessions_root is not None else bundles.sessions_dir()
    bundle_dir = root / session_id
    try:
        wav_relative_path = _relative_artifact_path(bundle_dir, raw_wav_path)
    except BundleError as exc:
        raise _error("bundle_pointer_invalid", str(exc)) from exc
    if (
        wav_relative_path != raw_wav_path
        or not wav_relative_path.startswith("repeat_captures/")
        or not wav_relative_path.endswith(".wav")
    ):
        raise _error("bundle_pointer_invalid", "pointer is not a repeat WAV")
    accepted_winners = [
        item
        for item in repeat_entries
        if item.get("accepted") is True
        and item.get("artifact_path") == wav_relative_path
    ]
    attempts = [item["attempt"] for item in repeat_entries]
    accepted_count = sum(item["accepted"] is True for item in repeat_entries)
    if (
        len(accepted_winners) != 1
        or sorted(attempts) != list(range(1, len(repeat_entries) + 1))
        or accepted_count != repeats.get("accepted")
        or accepted_count not in {2, 3}
    ):
        raise _error("bundle_pointer_invalid", "pointer is not the unique winner")
    analysis_relative_path = str(Path(wav_relative_path).with_suffix(".json"))

    try:
        info = bundles._read_info(bundle_dir)
        manifest = read_artifact_manifest(bundle_dir)
    except (OSError, BundleError, ValueError) as exc:
        raise _error("bundle_invalid", str(exc)) from exc
    fingerprints = info.get("fingerprints")
    mic = fingerprints.get("mic") if isinstance(fingerprints, Mapping) else None
    output_assignments = (
        fingerprints.get("output_assignments")
        if isinstance(fingerprints, Mapping)
        else None
    )
    calibration_id = str(comparison.get("calibration_id") or "")
    if (
        type(info.get("bundle_schema_version")) is not int
        or info.get("bundle_schema_version") != bundles.BUNDLE_SCHEMA_VERSION
        or info.get("kind") != bundles.BUNDLE_KIND
        or info.get("session_id") != session_id
        or not isinstance(fingerprints, Mapping)
        or fingerprints.get("topology_id") != topology.topology_id
        or fingerprints.get("topology_fingerprint") != topology_fp
        or fingerprints.get("comparison_set_id") != comparison.get("comparison_set_id")
        or fingerprints.get("comparison_set_fingerprint")
        != comparison.get("fingerprint")
        or not isinstance(mic, Mapping)
        or mic.get("calibration_id") != calibration_id
        or not isinstance(output_assignments, list)
        or {
            "group_id": speaker_group_id,
            "role": role,
            "physical_output_index": target.get("output_index"),
        }
        not in output_assignments
    ):
        raise _error("bundle_context_mismatch", "bundle context is stale")
    if (
        type(manifest.get("manifest_schema_version")) is not int
        or manifest.get("manifest_schema_version")
        != CURRENT_ARTIFACT_MANIFEST_VERSION
        or type(manifest.get("bundle_schema_version")) is not int
        or manifest.get("bundle_schema_version") != bundles.BUNDLE_SCHEMA_VERSION
    ):
        raise _error("bundle_manifest_schema_mismatch", "manifest header is invalid")
    entries = _manifest_entries(bundle_dir, manifest)
    _verified_artifact(
        bundle_dir,
        entries,
        "info.json",
        kind="metadata",
        dependencies=[],
        schema_version=bundles.BUNDLE_SCHEMA_VERSION,
    )
    wav_entry = _verified_artifact(
        bundle_dir,
        entries,
        wav_relative_path,
        kind="capture_wav",
        dependencies=[],
        schema_version=None,
    )
    analysis_entry = _verified_artifact(
        bundle_dir,
        entries,
        analysis_relative_path,
        kind="repeat_capture_analysis",
        dependencies=[wav_relative_path],
        schema_version=bundles.BUNDLE_SCHEMA_VERSION,
    )
    try:
        analysis = bundles._read_json(bundle_dir / analysis_relative_path)
    except BundleError as exc:
        raise _error("capture_analysis_invalid", str(exc)) from exc
    sibling_acoustic = analysis.get("acoustic")
    sibling_proof = analysis.get("placement_proof")
    binding_fields = (
        "verdict",
        "capture_geometry",
        "calibrated",
        "mic_clipping",
        "quality",
        "gating",
    )
    if (
        analysis.get("recorded") is not True
        or analysis.get("verdict") != "present"
        or analysis.get("speaker_group_id") != speaker_group_id
        or analysis.get("role") != role
        or not isinstance(sibling_acoustic, Mapping)
        or any(
            sibling_acoustic.get(field) != acoustic.get(field)
            for field in binding_fields
        )
        or not isinstance(sibling_proof, Mapping)
        or dict(sibling_proof) != dict(record.get("placement_proof") or {})
    ):
        raise _error("capture_binding_mismatch", "sidecar identity is inconsistent")
    analysis_input = analysis.get("analysis_input")
    expected_keys = {
        "schema_version",
        "response_amplitude",
        "display_fr_curve_peak_normalized",
        "sweep_meta",
        "excitation",
        "calibration",
        "capture_geometry",
        "ambient_duration_s",
    }
    if (
        not isinstance(analysis_input, Mapping)
        or set(analysis_input) != expected_keys
        or type(analysis_input.get("schema_version")) is not int
        or analysis_input.get("schema_version") != 1
        or analysis_input.get("response_amplitude") != "recompute_from_raw_wav"
        or analysis_input.get("display_fr_curve_peak_normalized") is not True
        or analysis_input.get("capture_geometry") != capture_geometry
    ):
        raise _error("capture_analysis_invalid", "replay contract is malformed")
    from jasper.audio_measurement.calibration import CalibrationCurve
    from jasper.audio_measurement.sweep import SweepMeta

    raw_sweep = analysis_input.get("sweep_meta")
    try:
        if not isinstance(raw_sweep, Mapping):
            raise ValueError("sweep metadata must be an object")
        sweep_meta = SweepMeta.from_dict(raw_sweep).to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("capture_analysis_invalid", str(exc)) from exc
    excitation = verified_driver_excitation(analysis_input.get("excitation"))
    snapshot_corrections = snapshot.get("corrections")
    snapshot_correction = (
        snapshot_corrections.get(role)
        if isinstance(snapshot_corrections, Mapping)
        else None
    )
    lock = driver_level_lock(comparison, speaker_group_id, role)
    applied_baseline_id = applied_profile.get("baseline_id")
    correction_gain = (
        _strict_number(snapshot_correction.get("gain_db"))
        if isinstance(snapshot_correction, Mapping)
        else None
    )
    lock_gain = (
        _strict_number(lock.get("commissioning_gain_db"))
        if isinstance(lock, Mapping)
        else None
    )
    lock_volume = (
        _strict_number(lock.get("locked_main_volume_db"))
        if isinstance(lock, Mapping)
        else None
    )
    if (
        excitation is None
        or any(
            name not in excitation
            for name in ("gain_source", "baseline_id", "topology_id", "role")
        )
        or excitation["topology_id"] != topology.topology_id
        or excitation["role"] != role
        or excitation["scope"] != "sweep_plus_role_gain_and_driver_level_lock"
        or not isinstance(applied_baseline_id, str)
        or not applied_baseline_id
        or excitation["baseline_id"] != applied_baseline_id
        or excitation["gain_source"]
        != "applied_baseline_recomposition_snapshot"
        or correction_gain is None
        or lock_gain is None
        or lock_volume is None
        or abs(float(excitation["commissioning_gain_db"]) - correction_gain)
        > 1e-6
        or abs(float(excitation["commissioning_gain_db"]) - lock_gain) > 1e-6
        or abs(float(excitation["locked_main_volume_db"]) - lock_volume) > 1e-6
        or abs(
            float(sweep_meta["amplitude_dbfs"])
            - float(excitation["sweep_peak_dbfs"])
        )
        > 1e-6
        or dict(analysis_input.get("excitation") or {})
        != dict(analysis.get("excitation") or {})
        or dict(analysis_input.get("excitation") or {})
        != dict(record.get("excitation") or {})
    ):
        raise _error("excitation_binding_mismatch", "excitation ledger is invalid")
    calibration = analysis_input.get("calibration")
    if (
        not calibration_id
        or not isinstance(calibration, Mapping)
        or set(calibration) != {"calibration_id", "curve"}
        or calibration.get("calibration_id") != calibration_id
        or acoustic.get("calibrated") is not True
    ):
        raise _error(
            "calibration_required",
            "strict replay requires the embedded calibrated-microphone curve",
        )
    try:
        raw_curve = calibration.get("curve")
        if not isinstance(raw_curve, Mapping):
            raise ValueError("calibration curve must be an object")
        calibration_curve = CalibrationCurve.from_dict(raw_curve)
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("calibration_curve_invalid", str(exc)) from exc
    calibration_curve_sha = _canonical_fingerprint(calibration_curve.to_dict())
    ambient_duration = analysis_input.get("ambient_duration_s")
    if ambient_duration is not None:
        ambient_duration = _strict_number(ambient_duration)
        if ambient_duration is None or ambient_duration <= 0.0:
            raise _error("capture_analysis_invalid", "ambient duration is invalid")
    return LegacyWinnerEvidence(
        target_id=target_id,
        capture_geometry=capture_geometry,
        comparison_set_id=str(comparison["comparison_set_id"]),
        comparison_set_fingerprint=str(comparison["fingerprint"]),
        applied_profile_fingerprint=profile_fingerprint,
        session_id=session_id,
        bundle_dir=bundle_dir,
        wav_path=bundle_dir / wav_relative_path,
        wav_relative_path=wav_relative_path,
        analysis_relative_path=analysis_relative_path,
        wav_sha256=str(wav_entry["sha256"]),
        analysis_sha256=str(analysis_entry["sha256"]),
        placement_proof_sha256=_canonical_fingerprint(
            dict(record.get("placement_proof") or {})
        ),
        calibration_id=calibration_id,
        calibration_curve_sha256=calibration_curve_sha,
        calibration_curve=calibration_curve,
        excitation=excitation,
        sweep_meta=sweep_meta,
        ambient_duration_s=ambient_duration,
    )


def replay_legacy_current_winner(evidence: LegacyWinnerEvidence) -> LegacyReplayResult:
    """Replay verified legacy inputs and preserve their permanent classification."""

    analysis_path = evidence.bundle_dir / evidence.analysis_relative_path
    try:
        if (
            _sha256_file(evidence.wav_path) != evidence.wav_sha256
            or _sha256_file(analysis_path) != evidence.analysis_sha256
        ):
            raise _error(
                "legacy_replay_input_changed",
                "winner WAV or replay sidecar changed after resolution",
            )
        analysis = bundles._read_json(analysis_path)
    except (BundleError, OSError) as exc:
        raise _error("legacy_replay_input_changed", str(exc)) from exc
    analysis_input = analysis.get("analysis_input")
    calibration = (
        analysis_input.get("calibration")
        if isinstance(analysis_input, Mapping)
        else None
    )
    if (
        not isinstance(analysis_input, Mapping)
        or dict(analysis_input.get("sweep_meta") or {}) != evidence.sweep_meta
        or verified_driver_excitation(analysis_input.get("excitation"))
        != evidence.excitation
        or not isinstance(calibration, Mapping)
        or calibration.get("calibration_id") != evidence.calibration_id
        or _canonical_fingerprint(dict(calibration.get("curve") or {}))
        != evidence.calibration_curve_sha256
        or _canonical_fingerprint(evidence.calibration_curve.to_dict())
        != evidence.calibration_curve_sha256
    ):
        raise _error(
            "legacy_replay_input_changed",
            "resolved replay inputs no longer match the immutable sidecar",
        )
    try:
        replayed = replay_driver_response(
            evidence.wav_path,
            evidence.sweep_meta,
            calibration=evidence.calibration_curve,
            capture_geometry=evidence.capture_geometry,
            ambient_duration_s=evidence.ambient_duration_s,
            scalar_playback_gain_db=float(
                evidence.excitation["scalar_playback_gain_db"]
            ),
        )
    except (DriverAcousticsError, OSError, TypeError, ValueError) as exc:
        raise _error("legacy_replay_failed", str(exc)) from exc
    source = {
        "session_id": evidence.session_id,
        "wav_path": evidence.wav_relative_path,
        "wav_sha256": evidence.wav_sha256,
        "analysis_path": evidence.analysis_relative_path,
        "analysis_sha256": evidence.analysis_sha256,
        "placement_proof_sha256": evidence.placement_proof_sha256,
        "sweep_meta_sha256": _canonical_fingerprint(evidence.sweep_meta),
        "excitation_sha256": _canonical_fingerprint(evidence.excitation),
        "calibration_id": evidence.calibration_id,
        "calibration_curve_sha256": evidence.calibration_curve_sha256,
        "comparison_set_id": evidence.comparison_set_id,
        "comparison_set_fingerprint": evidence.comparison_set_fingerprint,
        "applied_profile_fingerprint": evidence.applied_profile_fingerprint,
    }
    response = {
        "response_domain": "magnitude_db_only",
        "amplitude_reference": "played_excitation_normalized",
        "peak_normalized": False,
        "active_electrical_crossover_included": True,
        "natural_driver_plant_isolated": False,
        "phase_available": False,
        "frequency_hz": list(replayed.freqs_hz),
        "magnitude_db": list(replayed.magnitude_db),
        "calibration_support_hz": list(replayed.calibration_support_hz),
        "replay_support_hz": list(replayed.replay_support_hz),
        "scalar_playback_gain_db": float(
            evidence.excitation["scalar_playback_gain_db"]
        ),
    }
    return LegacyReplayResult(
        target_id=evidence.target_id,
        capture_geometry=evidence.capture_geometry,
        source=source,
        response=response,
        quality=replayed.quality,
        gating=replayed.gating,
    )


def resolve_and_replay_legacy_current_winner(
    topology: OutputTopology,
    applied_profile: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
    capture_geometry: str = "near_field",
    sessions_root: Path | None = None,
) -> LegacyReplayResult:
    """Resolve and immediately replay one fresh current-state snapshot."""

    evidence = resolve_legacy_current_winner(
        topology,
        applied_profile,
        measurements,
        speaker_group_id=speaker_group_id,
        role=role,
        capture_geometry=capture_geometry,
        sessions_root=sessions_root,
    )
    return replay_legacy_current_winner(evidence)
