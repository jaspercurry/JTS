# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker import bundles, driver_acoustics, legacy_replay, measurement
from jasper.active_speaker.baseline_profile import baseline_candidate_fingerprint
from jasper.active_speaker.capture_geometry import (
    DRIVER_PLACEMENT_POLICY_ID,
    normalized_placement_proof,
)
from jasper.active_speaker.driver_acoustics import ReplayedDriverResponse
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.reconstruction_capability import (
    ReconstructionCapability,
    ReconstructionRefusal,
    legacy_reconstruction_capability,
)
from jasper.audio_measurement import sweep
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.correction.bundles import write_json_artifact
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_profile import _two_way_preset


def _locks(topology):
    return {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -6.0,
        }
        for target in measurement.active_driver_targets(topology)
    }


def _topology_fingerprint(topology) -> str:
    return measurement._fingerprint(
        {
            "topology_id": topology.topology_id,
            "hardware": measurement._hardware_payload(topology),
        }
    )


def _applied_profile(topology) -> dict:
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    profile = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "status": "applied",
        "baseline_id": "baseline-1",
        "source": {"fingerprint": "source-1"},
        "recomposition_snapshot": {
            "schema_version": 1,
            "domain": "full",
            "topology_id": topology.topology_id,
            "topology_fingerprint": _topology_fingerprint(topology),
            "preset": preset.to_dict(),
            "playback_device": "hw:Loopback,0",
            "corrections": {
                "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
                "tweeter": {"gain_db": -6.0, "delay_ms": 0.0, "inverted": False},
            },
        },
    }
    profile["candidate_fingerprint"] = baseline_candidate_fingerprint(profile)
    return profile


def _fixture(tmp_path: Path, *, calibrated: bool = True):
    topology = mono_output_topology()
    applied_profile = _applied_profile(topology)
    calibration_id = "lab-cal" if calibrated else ""
    opened = bundles.open_bundle(
        topology,
        calibration_id=calibration_id,
        mic_calibration_sha256="f" * 64 if calibrated else None,
        sessions_dir=tmp_path,
    )
    assert opened is not None
    bundle_dir = Path(opened["bundle_dir"])
    comparison = measurement.start_active_comparison_set(
        topology,
        profile_context_id=applied_profile["candidate_fingerprint"],
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id=calibration_id,
        driver_level_locks=_locks(topology),
        bundle_session_id=opened["session_id"],
        state_path=tmp_path / "measurements.json",
        now="2026-07-13T12:00:00Z",
    )
    assert bundles.attach_comparison_set(
        bundle_dir,
        comparison_set_id=comparison["comparison_set_id"],
        comparison_set_fingerprint=comparison["fingerprint"],
    ) is not None
    target = next(
        item
        for item in measurement.active_driver_targets(topology)
        if item["role"] == "woofer"
    )
    _, sweep_meta = sweep.synchronized_swept_sine(duration_approx_s=0.2)
    curve = {
        "freqs_hz": [20.0, 20000.0],
        "correction_db": [0.0, 0.0],
        "phase_deg": None,
    }
    proof = normalized_placement_proof(
        policy_id=DRIVER_PLACEMENT_POLICY_ID,
        acknowledgement_binding="ack-near",
        relay_session_id="relay-near",
        capture_page={
            "capture_protocol_version": 2,
            "capture_page_build": "20260713.1",
        },
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint=target["target_fingerprint"],
        comparison_set=comparison,
    )
    excitation = {
        "schema_version": 1,
        "scope": "sweep_plus_role_gain_and_driver_level_lock",
        "sweep_peak_dbfs": sweep_meta.amplitude_dbfs,
        "commissioning_gain_db": 0.0,
        "locked_main_volume_db": -6.0,
        "effective_peak_dbfs": sweep_meta.amplitude_dbfs - 6.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-1",
        "topology_id": topology.topology_id,
        "role": "woofer",
    }
    analysis_input = {
        "schema_version": 1,
        "response_amplitude": "recompute_from_raw_wav",
        "display_fr_curve_peak_normalized": True,
        "sweep_meta": sweep_meta.to_dict(),
        "excitation": excitation,
        "calibration": (
            {"calibration_id": calibration_id, "curve": curve}
            if calibrated
            else None
        ),
        "capture_geometry": "near_field",
        "ambient_duration_s": None,
    }
    acoustic = {
        "verdict": "present",
        "capture_geometry": "near_field",
        "calibrated": calibrated,
        "mic_clipping": False,
        "quality": {"failed": False},
        "gating": {
            "applied": False,
            "exempt_reason": "near_field",
            "f_valid_floor_hz": None,
        },
        "fr_curve": {"freqs_hz": [20.0, 20000.0], "mag_db": [0.0, 0.0]},
    }
    repeat_entries = []
    for attempt in range(1, 4):
        source = tmp_path / f"near-{attempt}.wav"
        source.write_bytes(f"near-{attempt}".encode())
        appended = bundles.append_repeat_capture(
            bundle_dir,
            index=attempt - 1,
            wav_source_path=source,
            relative_path=f"repeat_captures/near_{attempt}.wav",
            payload={
                "recorded": True,
                "verdict": "present",
                "speaker_group_id": "mono",
                "role": "woofer",
                "acoustic": acoustic,
                "excitation": excitation,
                "placement_proof": proof,
                "analysis_input": analysis_input,
            },
        )
        assert appended is not None
        repeat_entries.append(
            {
                "attempt": attempt,
                "accepted": True,
                "artifact_path": appended["artifact_path"],
            }
        )
    record = {
        "captured": True,
        "outcome": "heard_correct_driver",
        "target_id": target["target_id"],
        "target_fingerprint": target["target_fingerprint"],
        "speaker_group_id": "mono",
        "role": "woofer",
        "output_index": target["output_index"],
        "acoustic": acoustic,
        "excitation": excitation,
        "placement_proof": proof,
        "bundle": {
            "session_id": opened["session_id"],
            "artifact_path": repeat_entries[0]["artifact_path"],
        },
        "repeats": {
            "target": 3,
            "accepted": 3,
            "per_repeat": repeat_entries,
        },
    }
    measurements = {
        "active_comparison_set": comparison,
        "summary": {
            "latest_driver_measurements": {"mono:woofer": record},
            "latest_reference_axis_driver_measurements": {},
        },
    }
    return topology, applied_profile, measurements, bundle_dir


def _fake_replay(*_args, **_kwargs):
    return ReplayedDriverResponse(
        freqs_hz=(100.0, 1000.0),
        magnitude_db=(-17.0, -3.0),
        quality={"failed": False},
        gating={"applied": False, "exempt_reason": "near_field"},
        calibration_support_hz=(20.0, 20000.0),
        replay_support_hz=(20.0, 20000.0),
    )


def test_exact_current_winner_replay_is_permanently_non_authoritative(
    tmp_path: Path, monkeypatch
) -> None:
    topology, applied, measurements, _bundle_dir = _fixture(tmp_path)
    monkeypatch.setattr(legacy_replay, "replay_driver_response", _fake_replay)

    result = legacy_replay.resolve_and_replay_legacy_current_winner(
        topology,
        applied,
        measurements,
        speaker_group_id="mono",
        role="woofer",
        sessions_root=tmp_path,
    ).to_dict()

    assert result["evidence_classification"] == "legacy_non_admitted"
    assert result["authoritative"] is False
    assert all(
        result[name] is False
        for name in (
            "authorizes_candidate",
            "authorizes_apply",
            "authorizes_verification",
            "authorizes_receipt",
            "authorizes_playback",
        )
    )
    assert result["response"]["amplitude_reference"] == (
        "played_excitation_normalized"
    )
    assert result["response"]["peak_normalized"] is False
    assert result["response"]["active_electrical_crossover_included"] is True
    assert result["response"]["natural_driver_plant_isolated"] is False
    assert "candidate_fingerprint" not in result


def test_display_curve_mutation_cannot_change_replay_identity(
    tmp_path: Path, monkeypatch
) -> None:
    topology, applied, measurements, _bundle_dir = _fixture(tmp_path)
    monkeypatch.setattr(legacy_replay, "replay_driver_response", _fake_replay)
    first_evidence = legacy_replay.resolve_legacy_current_winner(
        topology,
        applied,
        measurements,
        speaker_group_id="mono",
        role="woofer",
        sessions_root=tmp_path,
    )
    first = legacy_replay.replay_legacy_current_winner(first_evidence).to_dict()
    changed = copy.deepcopy(measurements)
    changed["summary"]["latest_driver_measurements"]["mono:woofer"]["acoustic"][
        "fr_curve"
    ] = {"freqs_hz": [99.0], "mag_db": [999.0]}
    second_evidence = legacy_replay.resolve_legacy_current_winner(
        topology,
        applied,
        changed,
        speaker_group_id="mono",
        role="woofer",
        sessions_root=tmp_path,
    )
    second = legacy_replay.replay_legacy_current_winner(second_evidence).to_dict()
    assert second["diagnostic_fingerprint"] == first["diagnostic_fingerprint"]
    assert second["response"] == first["response"]


def test_result_mutation_rebinds_serialized_diagnostic_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    topology, applied, measurements, _bundle_dir = _fixture(tmp_path)
    monkeypatch.setattr(legacy_replay, "replay_driver_response", _fake_replay)
    result = legacy_replay.resolve_and_replay_legacy_current_winner(
        topology,
        applied,
        measurements,
        speaker_group_id="mono",
        role="woofer",
        sessions_root=tmp_path,
    )
    before = result.to_dict()
    result.response["magnitude_db"][0] = 999.0
    after = result.to_dict()

    assert after["diagnostic_fingerprint"] != before["diagnostic_fingerprint"]
    without_fingerprint = dict(after)
    without_fingerprint.pop("diagnostic_fingerprint")
    assert after["diagnostic_fingerprint"] == legacy_replay._canonical_fingerprint(
        without_fingerprint
    )


def test_resolver_refuses_substitution_staleness_and_integrity_failures(
    tmp_path: Path,
) -> None:
    topology, applied, measurements, bundle_dir = _fixture(tmp_path)

    stale = copy.deepcopy(applied)
    stale["candidate_fingerprint"] = "another-profile"
    with pytest.raises(legacy_replay.LegacyReplayError) as stale_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            stale,
            measurements,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert stale_error.value.reason == "comparison_context_invalid"

    duplicate = copy.deepcopy(measurements)
    winner = duplicate["summary"]["latest_driver_measurements"]["mono:woofer"]
    winner["repeats"]["per_repeat"][1]["artifact_path"] = winner["bundle"][
        "artifact_path"
    ]
    with pytest.raises(legacy_replay.LegacyReplayError) as duplicate_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            duplicate,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert duplicate_error.value.reason == "bundle_pointer_invalid"

    pointed = measurements["summary"]["latest_driver_measurements"]["mono:woofer"][
        "bundle"
    ]["artifact_path"]
    (bundle_dir / pointed).write_bytes(b"tampered")
    with pytest.raises(legacy_replay.LegacyReplayError) as integrity_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            measurements,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert integrity_error.value.reason == "bundle_artifact_integrity_mismatch"


def test_resolver_refuses_missing_calibration_without_synthesizing_it(
    tmp_path: Path,
) -> None:
    topology, applied, measurements, _bundle_dir = _fixture(
        tmp_path, calibrated=False
    )
    with pytest.raises(legacy_replay.LegacyReplayError) as error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            measurements,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert error.value.reason == "calibration_required"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("baseline_id", "substituted-baseline"),
        ("gain_source", "manual_or_unbound_gain"),
        ("commissioning_gain_db", -1.0),
        ("locked_main_volume_db", -7.0),
    ),
)
def test_excitation_terms_are_bound_to_applied_profile_and_comparison_lock(
    tmp_path: Path, field: str, replacement: object
) -> None:
    topology, applied, measurements, bundle_dir = _fixture(tmp_path)
    record = measurements["summary"]["latest_driver_measurements"]["mono:woofer"]
    excitation = dict(record["excitation"])
    excitation[field] = replacement
    excitation["effective_peak_dbfs"] = (
        float(excitation["sweep_peak_dbfs"])
        + float(excitation["commissioning_gain_db"])
        + float(excitation["locked_main_volume_db"])
    )
    record["excitation"] = excitation
    wav_rel = record["bundle"]["artifact_path"]
    analysis_rel = str(Path(wav_rel).with_suffix(".json"))
    analysis = bundles._read_json(bundle_dir / analysis_rel)
    analysis["excitation"] = excitation
    analysis["analysis_input"]["excitation"] = excitation
    write_json_artifact(
        bundle_dir,
        analysis_rel,
        analysis,
        kind="repeat_capture_analysis",
        sensitivity="derived",
        recomputable=True,
        generated_by="test",
        dependencies=[wav_rel],
        schema_version=1,
    )

    with pytest.raises(legacy_replay.LegacyReplayError) as error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            measurements,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert error.value.reason == "excitation_binding_mismatch"


@pytest.mark.parametrize("manifest_field", ("kind", "dependencies"))
def test_manifest_kind_and_dependency_substitution_refuses(
    tmp_path: Path, manifest_field: str
) -> None:
    topology, applied, measurements, bundle_dir = _fixture(tmp_path)
    record = measurements["summary"]["latest_driver_measurements"]["mono:woofer"]
    wav_rel = record["bundle"]["artifact_path"]
    analysis_rel = str(Path(wav_rel).with_suffix(".json"))
    manifest_path = bundle_dir / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["artifacts"] if item["path"] == analysis_rel)
    entry[manifest_field] = "wrong_kind" if manifest_field == "kind" else []
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(legacy_replay.LegacyReplayError) as error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            measurements,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert error.value.reason == "bundle_artifact_invalid"


def test_sidecar_target_session_and_geometry_substitution_refuse(tmp_path: Path) -> None:
    topology, applied, measurements, bundle_dir = _fixture(tmp_path)
    record = measurements["summary"]["latest_driver_measurements"]["mono:woofer"]
    sidecar = bundle_dir / str(Path(record["bundle"]["artifact_path"]).with_suffix(".json"))
    sidecar.write_text("{}")
    with pytest.raises(legacy_replay.LegacyReplayError) as sidecar_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            measurements,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path,
        )
    assert sidecar_error.value.reason == "bundle_artifact_integrity_mismatch"

    _, _, fresh, _ = _fixture(tmp_path / "fresh")
    substituted = copy.deepcopy(fresh)
    current = substituted["summary"]["latest_driver_measurements"]["mono:woofer"]
    current["bundle"]["session_id"] = "0" * 12
    with pytest.raises(legacy_replay.LegacyReplayError) as session_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            substituted,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path / "fresh",
        )
    assert session_error.value.reason == "bundle_pointer_invalid"

    target_changed = copy.deepcopy(fresh)
    target_changed["summary"]["latest_driver_measurements"]["mono:woofer"][
        "role"
    ] = "tweeter"
    with pytest.raises(legacy_replay.LegacyReplayError) as target_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            target_changed,
            speaker_group_id="mono",
            role="woofer",
            sessions_root=tmp_path / "fresh",
        )
    assert target_error.value.reason == "current_winner_stale"

    with pytest.raises(legacy_replay.LegacyReplayError) as geometry_error:
        legacy_replay.resolve_legacy_current_winner(
            topology,
            applied,
            fresh,
            speaker_group_id="mono",
            role="woofer",
            capture_geometry="reference_axis",
            sessions_root=tmp_path / "fresh",
        )
    assert geometry_error.value.reason == "current_winner_missing"


def test_replay_rechecks_resolved_files_before_analysis(
    tmp_path: Path, monkeypatch
) -> None:
    topology, applied, measurements, _bundle_dir = _fixture(tmp_path)
    evidence = legacy_replay.resolve_legacy_current_winner(
        topology,
        applied,
        measurements,
        speaker_group_id="mono",
        role="woofer",
        sessions_root=tmp_path,
    )
    evidence.wav_path.write_bytes(b"changed-after-resolve")
    monkeypatch.setattr(legacy_replay, "replay_driver_response", _fake_replay)

    with pytest.raises(legacy_replay.LegacyReplayError) as error:
        legacy_replay.replay_legacy_current_winner(evidence)
    assert error.value.reason == "legacy_replay_input_changed"


def test_response_replay_removes_only_scalar_gain_and_never_peak_normalizes(
    tmp_path: Path,
) -> None:
    signal, meta = sweep.synchronized_swept_sine(
        duration_approx_s=0.5,
        amplitude_dbfs=-12.0,
    )
    path = tmp_path / "capture.wav"
    sweep.write_sweep_wav(path, (signal * 0.25).astype(np.float32), meta.sample_rate)
    calibration = CalibrationCurve(
        freqs_hz=[10.0, 24000.0],
        correction_db=[0.0, 0.0],
    )

    baseline = driver_acoustics.replay_driver_response(
        path,
        meta.to_dict(),
        calibration=calibration,
        capture_geometry="near_field",
        ambient_duration_s=None,
        scalar_playback_gain_db=0.0,
    )
    normalized = driver_acoustics.replay_driver_response(
        path,
        meta.to_dict(),
        calibration=calibration,
        capture_geometry="near_field",
        ambient_duration_s=None,
        scalar_playback_gain_db=-6.0,
    )

    delta = np.asarray(normalized.magnitude_db) - np.asarray(baseline.magnitude_db)
    assert np.max(np.abs(delta - 6.0)) < 1e-6
    assert abs(max(baseline.magnitude_db)) > 1.0
    assert baseline.calibration_support_hz == (10.0, 24000.0)
    assert baseline.replay_support_hz == (20.0, 20000.0)
    assert baseline.freqs_hz[0] >= 20.0
    assert baseline.freqs_hz[-1] <= 20000.0


def test_legacy_cabinet_and_splice_capability_is_typed_and_not_ready() -> None:
    sealed = legacy_reconstruction_capability(
        {
            "enclosure_kind": "sealed",
            "radiator_count": 1,
            "effective_radiating_diameter_mm": 132.0,
            "baffle_width_mm": 210.0,
        }
    ).to_dict()
    assert sealed["ready"] is False
    assert sealed["refusals"] == [
        ReconstructionRefusal.GEOMETRY_MISSING.value,
        ReconstructionRefusal.CAPTURE_NOT_ADMITTED.value,
    ]
    assert sealed["authorizes_splice"] is False
    assert sealed["authorizes_candidate"] is False

    vented = legacy_reconstruction_capability(
        {"enclosure_kind": "vented", "radiator_count": 1}
    ).to_dict()
    assert vented["refusals"] == [
        ReconstructionRefusal.ENCLOSURE_UNSUPPORTED.value,
        ReconstructionRefusal.CAPTURE_NOT_ADMITTED.value,
    ]


def test_reconstruction_capability_cannot_be_forged_ready() -> None:
    with pytest.raises(TypeError, match="factory"):
        ReconstructionCapability((), "forged")


@pytest.mark.parametrize(
    ("cabinet", "expected"),
    (
        (None, ReconstructionRefusal.GEOMETRY_MISSING),
        ({"enclosure_kind": "unknown"}, ReconstructionRefusal.ENCLOSURE_UNSUPPORTED),
        (
            {"enclosure_kind": "passive_radiator", "radiator_count": 1},
            ReconstructionRefusal.ENCLOSURE_UNSUPPORTED,
        ),
        (
            {"enclosure_kind": "sealed", "radiator_count": 2},
            ReconstructionRefusal.SOURCE_COUNT_UNSUPPORTED,
        ),
        (
            {"enclosure_kind": "sealed", "radiator_count": 1},
            ReconstructionRefusal.GEOMETRY_MISSING,
        ),
        (
            {
                "enclosure_kind": "sealed",
                "radiator_count": True,
                "effective_radiating_diameter_mm": float("nan"),
                "baffle_width_mm": "wide",
            },
            ReconstructionRefusal.SOURCE_COUNT_UNSUPPORTED,
        ),
    ),
)
def test_cabinet_refusal_contract_covers_unsupported_shapes(
    cabinet: dict | None, expected: ReconstructionRefusal
) -> None:
    result = legacy_reconstruction_capability(cabinet).to_dict()
    assert result["ready"] is False
    assert result["refusals"][0] == expected.value
    assert result["refusals"][-1] == ReconstructionRefusal.CAPTURE_NOT_ADMITTED.value
