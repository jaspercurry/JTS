# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.active_speaker.commissioning_capture_producer import RawCaptureResult
from jasper.active_speaker.commissioning_evidence import (
    AdmittedRegionCapture,
    active_region_threshold_profile_fingerprint,
    derive_region_evidence_plan,
)
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
    attempt_capture_relative_path,
)
from jasper.active_speaker.commissioning_host import (
    CommissioningEvidenceHost,
    CommissioningHostAuthoritySnapshot,
)
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.driver_safety import (
    build_driver_safety_profile,
    evaluate_driver_safety_profile,
)
from jasper.active_speaker.measurement import (
    active_driver_targets,
    start_active_comparison_set,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.audio_measurement import admitted_playback
from jasper.audio_measurement.calibration import CalibrationCurve
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_commissioning_capture_producer import (
    _fake_playback,
    _synthetic_reference_axis_wav,
)
from tests.test_active_speaker_commissioning_host import _region_inputs
from tests.test_active_speaker_commissioning_runtime import FakePort
from tests.test_active_speaker_driver_safety import _manual_settings
from tests.test_active_speaker_profile import _two_way_preset


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.asyncio
async def test_real_producer_commits_synthetic_capture_through_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = mono_output_topology(mode="active_2_way")
    preset_raw = deepcopy(_two_way_preset())
    preset_raw["crossover_regions"][0]["fc_hz"] = 6_000
    preset = ActiveSpeakerPreset.from_mapping(preset_raw)

    manual = deepcopy(_manual_settings())
    for driver in manual["drivers"]:
        driver["hard_excitation_band_hz"] = [3_000, 12_000]
        driver["measurement_band_hz"] = [3_000, 12_000]
        driver["crossover_search_band_hz"] = [5_000, 7_000]
        driver["level_duration_limits"]["minimum_cooldown_s"] = 0
        if driver["role"] == "woofer":
            driver["required_protection_filters"][0]["cutoff_hz"] = 7_000
    safety_profile = build_driver_safety_profile(
        topology,
        manual_settings=manual,
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-14T12:00:00Z",
    )
    safety = evaluate_driver_safety_profile(safety_profile, topology)
    assert safety.confirmed_and_current
    assert safety.profile_fingerprint is not None

    bundle = open_bundle(
        topology,
        calibration_id="host-producer-calibration",
        sessions_dir=tmp_path / "sessions",
    )
    assert bundle is not None
    evidence_store = CommissioningEvidenceStore.open(
        bundle["bundle_dir"], expected_session_id=bundle["session_id"]
    )
    locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 6_000.0,
            "tone_peak_dbfs": -24.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": (
                -22.0 if target["role"] == "woofer" else -32.0
            ),
        }
        for target in active_driver_targets(topology)
    }
    comparison_set = start_active_comparison_set(
        topology,
        profile_context_id=safety.profile_fingerprint,
        setup_sha256=_hash("setup"),
        device_sha256=_hash("device"),
        calibration_id="host-producer-calibration",
        driver_level_locks=locks,
        bundle_session_id=evidence_store.session_id,
        state_path=tmp_path / "measurements.json",
        now="2026-07-14T12:00:01Z",
    )
    run_store = CommissioningRunStore(
        path=tmp_path / "run.json", owner_id="1" * 32
    )
    run = run_store.start(
        session_id=evidence_store.session_id,
        session_fingerprint=str(comparison_set["fingerprint"]),
    )
    plan = derive_region_evidence_plan(
        preset,
        topology,
        run=run,
        protected_safety_profile_fingerprint=safety.profile_fingerprint,
        comparison_set_fingerprint=str(comparison_set["fingerprint"]),
        threshold_profile_fingerprint=active_region_threshold_profile_fingerprint(),
        context_fingerprint=_hash("context"),
    )
    applied_profile = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "status": "applied",
        "baseline_id": "host-producer-baseline",
        "recomposition_snapshot": {
            "schema_version": 1,
            "domain": "full",
            "topology_id": topology.topology_id,
            "topology_fingerprint": topology_config_fingerprint(topology),
            "preset": preset.to_dict(),
            "playback_device": "hw:Loopback,0",
            "corrections": {
                "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
                "tweeter": {"gain_db": -6.0, "delay_ms": 0.0, "inverted": False},
            },
        },
    }
    calibration = CalibrationCurve(
        freqs_hz=[20.0, 20_000.0], correction_db=[0.0, 0.0]
    )
    authority = CommissioningHostAuthoritySnapshot(
        topology=topology,
        preset=preset,
        safety_profile=safety_profile,
        comparison_set=comparison_set,
        applied_profile=applied_profile,
        calibration_id="host-producer-calibration",
        calibration=calibration,
    )

    async def transport(play):
        playback = await play()
        return RawCaptureResult(
            _synthetic_reference_axis_wav(playback),
            {"fixture": "deterministic_reference_axis"},
        )

    monkeypatch.setattr(admitted_playback, "play_verified_wav", _fake_playback)
    host = CommissioningEvidenceHost(
        plan=plan,
        topology=topology,
        run_store=run_store,
        evidence_store=evidence_store,
        region_inputs=_region_inputs(evidence_store, plan),
        load_current_authority=lambda: authority,
        raw_capture_transport=transport,
    )
    fake = FakePort()
    predecessor_raw = fake.raw
    predecessor_volume = fake.volume

    committed = await host.capture_next_with_runtime(
        fake.port(), config_dir=str(tmp_path)
    )

    assert isinstance(committed, AdmittedRegionCapture)
    assert fake.raw == predecessor_raw
    assert fake.volume == predecessor_volume
    artifact = evidence_store.identify_artifact(
        attempt_capture_relative_path(committed.attempt_id, 0)
    )
    assert evidence_store.reopen_admitted_region_capture(artifact) == committed
    mutation = run_store.current_live_mutation(run)
    assert mutation is not None
    assert mutation.status == "committed"
    assert mutation.terminal_evidence_fingerprint == artifact.fingerprint
    next_operation = host.next_operation()
    assert next_operation is not None
    assert next_operation.attempt.attempt_id == committed.attempt_id
    assert next_operation.capture_ordinal == 1
