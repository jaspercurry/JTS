# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jasper.active_speaker.camilla_yaml import (
    APPLIED_RESPONSE_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB,
    audible_outputs_for_role,
    emit_active_speaker_commissioning_config,
)
from jasper.active_speaker.capture_geometry import comparison_set_fingerprint
from jasper.active_speaker.commissioning_admission import (
    play_admitted_driver_capture,
)
from jasper.active_speaker.commissioning_evidence import (
    region_evidence_preset_fingerprint,
)
from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.commissioning_isolated_producer import (
    isolated_evidence_status,
    promote_isolated_driver_capture,
    resume_isolated_evidence,
)
from jasper.active_speaker.commissioning_run import CommissioningRunStore
from jasper.active_speaker.driver_acoustics import DRIVER_ACOUSTIC_KIND
from jasper.active_speaker.baseline_profile import topology_config_fingerprint
from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.staging import driver_commission_audible_evidence
from jasper.audio_measurement.admitted_playback import AdmittedPlaybackResult
from jasper.audio_measurement.calibration import CalibrationCurve
from jasper.audio_measurement.excitation_artifacts import (
    readmit_and_persist_playback_admission,
)
from jasper.audio_measurement.playback import PlaybackResult
from tests.test_active_speaker_commissioning_admission import _context
from tests.test_active_speaker_profile import _two_way_preset


def test_production_driver_captures_build_exact_complete_isolated_evidence(
    tmp_path,
    monkeypatch,
):
    topology, safety_profile, _targets, comparison, applied, _raw, _load = _context(
        tmp_path,
        monkeypatch,
    )
    comparison["calibration_id"] = "mic-1"
    comparison["fingerprint"] = comparison_set_fingerprint(comparison)
    common = {
        "hard_excitation_band_hz": [500, 20_000],
        "measurement_band_hz": [500, 10_000],
        "crossover_search_band_hz": [4000, 6000],
        "level_duration_limits": {
            "max_effective_peak_dbfs": -65,
            "max_sweep_duration_s": 4,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 0,
        },
    }
    safety_profile = build_driver_safety_profile(
        topology,
        manual_settings={
            "drivers": [
                {
                    **common,
                    "target_id": "mono:woofer",
                    "role": "woofer",
                    "model": "Example W6",
                    "required_protection_filters": [
                        {
                            "kind": "lowpass",
                            "cutoff_hz": 5000,
                            "minimum_slope_db_per_octave": 24,
                        }
                    ],
                    "cabinet": {
                        "enclosure_kind": "sealed",
                        "radiator_count": 1,
                        "effective_radiating_diameter_mm": 132,
                        "baffle_width_mm": 210,
                    },
                },
                {
                    **common,
                    "target_id": "mono:tweeter",
                    "role": "tweeter",
                    "model": "Example T1",
                    "required_protection_filters": [
                        {
                            "kind": "highpass",
                            "cutoff_hz": 5000,
                            "minimum_slope_db_per_octave": 24,
                        }
                    ],
                    "cabinet": {
                        "enclosure_kind": "sealed",
                        "radiator_count": 1,
                        "effective_radiating_diameter_mm": 25,
                    },
                },
            ],
            "crossover_candidates": [],
        },
        driver_research=None,
        confirm=True,
        confirmed_at="2026-07-15T12:00:00Z",
    )
    preset_payload = _two_way_preset()
    preset_payload["crossover_regions"][0]["fc_hz"] = 5000
    preset = ActiveSpeakerPreset.from_mapping(preset_payload)
    applied = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_baseline_profile_candidate",
        "status": "applied",
        "baseline_id": "isolated-producer-baseline",
        "recomposition_snapshot": {
            "schema_version": 1,
            "domain": "full",
            "topology_id": topology.topology_id,
            "topology_fingerprint": topology_config_fingerprint(topology),
            "preset": preset.to_dict(),
            "playback_device": "hw:CARD=DAC8x,DEV=0",
            "corrections": {
                "woofer": {
                    "gain_db": 0.0,
                    "delay_ms": 0.0,
                    "inverted": False,
                },
                "tweeter": {
                    "gain_db": 0.0,
                    "delay_ms": 0.0,
                    "inverted": False,
                },
            },
        },
    }
    run_store = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="a" * 32,
    )
    run = run_store.start(
        session_id=comparison["bundle_session_id"],
        session_fingerprint=comparison["fingerprint"],
    )
    evidence_store = CommissioningEvidenceStore.open(
        tmp_path / run.session_id,
        expected_session_id=run.session_id,
    )

    async def fake_play(
        stimulus_bundle_dir,
        *,
        stimulus,
        authority,
        generation,
        issue_current_inputs,
        alsa_device,
        timeout_s,
    ):
        del timeout_s
        current = await issue_current_inputs()
        result = readmit_and_persist_playback_admission(
            authority,
            generation,
            current_limits=current.limits,
            current_protection_evidence=current.protection_evidence,
        )
        assert result.artifact is not None
        return AdmittedPlaybackResult(
            playback=PlaybackResult(
                wav_path=Path(
                    stimulus_bundle_dir,
                    stimulus.artifact.relative_path,
                ),
                alsa_device=alsa_device,
                returncode=0,
            ),
            admission=result.artifact,
        )

    import jasper.active_speaker.commissioning_admission as admission_module

    monkeypatch.setattr(admission_module, "play_admitted_wav", fake_play)

    async def no_sleep(_delay_s):
        return None

    monkeypatch.setattr(admission_module.asyncio, "sleep", no_sleep)
    calibration = CalibrationCurve(
        freqs_hz=[20.0, 20_000.0],
        correction_db=[0.0, 0.0],
    )

    async def capture(role: str):
        raw = emit_active_speaker_commissioning_config(
            preset,
            playback_device="hw:CARD=DAC8x,DEV=0",
            audible_outputs=audible_outputs_for_role(preset, role),
            audible_gain_db=-50.0,
            volume_limit_db=-4.0,
            startup_headroom_db=COMMISSIONING_HEADROOM_DB,
            filter_mode=APPLIED_RESPONSE_FILTER_MODE,
        )
        intent = driver_commission_audible_evidence(
            raw,
            preset=preset,
            audible_outputs=audible_outputs_for_role(preset, role),
            filter_mode=APPLIED_RESPONSE_FILTER_MODE,
        )
        load_payload = {
            "preflight": {"audible_evidence": intent},
            "load": {
                "status": "loaded",
                "target": {"speaker_group_id": "mono", "role": role},
            },
        }

        async def read_running():
            return raw

        async def read_volume():
            return -4.0

        playback = await play_admitted_driver_capture(
            topology=topology,
            safety_profile=safety_profile,
            comparison_set=comparison,
            applied_profile=applied,
            speaker_group_id="mono",
            role=role,
            commissioning_gain_db=-50.0,
            expected_main_volume_db=-4.0,
            load_payload=load_payload,
            read_running_config=read_running,
            read_main_volume_db=read_volume,
            load_current_context=lambda: (
                topology,
                safety_profile,
                comparison,
                applied,
            ),
            alsa_device="correction_substream",
            timeout_margin_s=9.0,
        )
        effective = playback.handoff.admission["request"]["effective_peak_dbfs"]
        excitation = {
            "schema_version": 1,
            "scope": "sweep_plus_role_gain_and_driver_level_lock",
            "sweep_peak_dbfs": -12.0,
            "commissioning_gain_db": -50.0,
            "locked_main_volume_db": -4.0,
            "effective_peak_dbfs": effective,
            "role": role,
        }
        provisional = {
            "excitation": excitation,
            "acoustic": {
                "kind": DRIVER_ACOUSTIC_KIND,
                "verdict": "present",
                "present": True,
                "calibrated": True,
                "capture_geometry": "reference_axis",
                "mic_clipping": False,
                "gating": {"applied": True, "f_valid_floor_hz": 150.0},
                "snr": {"decision_class": "magnitude", "verdict": "ok"},
                "overlap_levels": [
                    {
                        "fc_hz": 5_000.0,
                        "level_db": -32.0 if role == "woofer" else -34.0,
                        "usable": True,
                        "above_validity_floor": True,
                        "near_validity_floor": False,
                        "snr_verdict": "ok",
                    }
                ],
            },
        }
        return promote_isolated_driver_capture(
            topology=topology,
            preset=preset,
            comparison_set=comparison,
            applied_profile=applied,
            calibration_id="mic-1",
            calibration=calibration,
            speaker_group_id="mono",
            role=role,
            capture_geometry="reference_axis",
            wav_bytes=f"wav:{playback.handoff.admission_id}".encode(),
            sweep_meta=playback.sweep_meta.to_dict(),
            provisional=provisional,
            admission_handoff=playback.handoff.to_dict(),
            run=run,
            run_store=run_store,
            evidence_store=evidence_store,
        )

    results = [asyncio.run(capture("woofer")) for _index in range(3)]
    results.extend(asyncio.run(capture("tweeter")) for _index in range(2))
    reopen_driver = CommissioningEvidenceStore.reopen_isolated_driver_evidence
    reopen_attempt = CommissioningEvidenceStore.reopen_isolated_attempt_captures
    deep_reopens = 0
    attempt_reopens = 0

    def count_deep_reopen(self, **kwargs):
        nonlocal deep_reopens
        deep_reopens += 1
        return reopen_driver(self, **kwargs)

    def count_attempt_reopen(self, attempt_id):
        nonlocal attempt_reopens
        attempt_reopens += 1
        return reopen_attempt(self, attempt_id)

    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "reopen_isolated_driver_evidence",
        count_deep_reopen,
    )
    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "reopen_isolated_attempt_captures",
        count_attempt_reopen,
    )
    assert resume_isolated_evidence(
        run=run,
        run_store=run_store,
        evidence_store=evidence_store,
    ) is None
    assert deep_reopens == 0
    assert attempt_reopens == 0
    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "reopen_isolated_driver_evidence",
        reopen_driver,
    )
    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "reopen_isolated_attempt_captures",
        reopen_attempt,
    )
    publish_driver = CommissioningEvidenceStore.publish_isolated_driver_evidence
    failed_once = False

    def fail_final_driver_once(self, evidence):
        nonlocal failed_once
        if not failed_once and evidence.role == "tweeter":
            failed_once = True
            raise OSError("injected per-driver anchor interruption")
        return publish_driver(self, evidence)

    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "publish_isolated_driver_evidence",
        fail_final_driver_once,
    )
    with pytest.raises(OSError, match="injected per-driver"):
        asyncio.run(capture("tweeter"))
    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "publish_isolated_driver_evidence",
        publish_driver,
    )
    publish_complete = (
        CommissioningEvidenceStore.publish_complete_isolated_driver_evidence
    )

    def fail_complete_once(self, evidence):
        raise OSError("injected run aggregate interruption")

    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "publish_complete_isolated_driver_evidence",
        fail_complete_once,
    )
    with pytest.raises(OSError, match="injected run aggregate"):
        resume_isolated_evidence(
            run=run,
            run_store=run_store,
            evidence_store=evidence_store,
        )
    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "publish_complete_isolated_driver_evidence",
        publish_complete,
    )
    resumed = resume_isolated_evidence(
        run=run,
        run_store=run_store,
        evidence_store=evidence_store,
    )

    assert results[0]["accepted"] == 1
    assert results[2]["driver_complete"] is True
    assert resumed is not None
    complete = evidence_store.reopen_complete_isolated_driver_evidence(
        run_id=run.run_id
    )
    plan = evidence_store.reopen_region_evidence_plan(run=run)
    assert plan.preset_fingerprint == region_evidence_preset_fingerprint(preset)
    assert [item.canonical_key for item in complete.drivers] == [
        ("mono", "woofer"),
        ("mono", "tweeter"),
    ]
    assert all(len(item.captures) == 3 for item in complete.drivers)
    assert run_store.snapshot()["current"]["lifecycle_state"] == "protected"
    assert isolated_evidence_status(
        run=run,
        run_store=run_store,
        evidence_store=evidence_store,
    ) == {
        "status": "complete",
        "plan_fingerprint": plan.fingerprint,
        "drivers": [
            {
                "speaker_group_id": "mono",
                "role": "woofer",
                "accepted": 3,
                "required": 3,
                "complete": True,
            },
            {
                "speaker_group_id": "mono",
                "role": "tweeter",
                "accepted": 3,
                "required": 3,
                "complete": True,
            },
        ],
        "complete_fingerprint": complete.fingerprint,
    }
