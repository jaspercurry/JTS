# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging

from pathlib import Path

import pytest

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
    clear_active_comparison_set,
    confirmed_driver_roles,
    current_driver_floor_evidence,
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
    start_active_comparison_set,
)
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology


def _topology(
    *,
    tweeter_output: int = 1,
    tweeter_verified: bool = True,
) -> OutputTopology:
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono",
        "name": "Bench mono",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": tweeter_output,
                        "identity_verified": tweeter_verified,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _three_way_topology() -> OutputTopology:
    """A mono 3-way matching ``tests.test_active_speaker_profile``'s
    ``_three_way_preset(layout="mono")``: woofer=output 0, mid=output 1,
    tweeter=output 2, crossovers at 350 Hz (woofer/mid) and 2500 Hz
    (mid/tweeter)."""
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench_mono_3way",
        "name": "Bench mono 3-way",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_3_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "mid",
                        "physical_output_index": 1,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 2,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    })


def _safe_session(
    *,
    role: str,
    output_index: int,
    playback_id: str,
) -> dict:
    target = {
        "speaker_group_id": "mono",
        "role": role,
        "driver_role": role,
        "output_index": output_index,
    }
    return {
        "status": "armed",
        "quiet_start": {
            "status": "floor_confirmed",
            "floor_audio_confirmed": True,
            "current_target": target,
            "last_operator_result": {
                "accepted": True,
                "outcome": "heard_correct_driver",
                "playback_id": playback_id,
                "target": target,
            },
        },
    }


def _record_summed_test(
    topology: OutputTopology,
    state_path: Path,
    *,
    playback_id: str = "summed-playback-1",
    audio_emitted: bool = True,
    playback_issues: list[dict] | None = None,
) -> dict:
    return record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay" if audio_emitted else "wav_artifact",
                "playback_id": playback_id,
                "audio_emitted": audio_emitted,
                "artifact": {
                    "wav_basename": f"tone_{playback_id}.wav",
                    "metadata_basename": f"tone_{playback_id}.json",
                    "target_output_indices": [0, 1],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -72},
                "stimulus": {
                    "kind": "jts_active_speaker_speech_stimulus",
                    "text": "Like and subscribe to Jasper tech.",
                    "duration_ms": 12000,
                },
                "issues": playback_issues or [],
            },
        },
        state_path=state_path,
        now="2026-06-14T12:02:30Z",
    )


def test_summed_test_records_spoken_stimulus_metadata(tmp_path: Path) -> None:
    topology = _topology()
    payload = _record_summed_test(topology, tmp_path / "measurements.json")

    latest = payload["summary"]["latest_summed_tests"]["mono"]
    assert latest["stimulus"] == {
        "kind": "jts_active_speaker_speech_stimulus",
        "text": "Like and subscribe to Jasper tech.",
        "duration_ms": 12000,
    }


def test_failed_summed_test_without_artifact_does_not_claim_output_mismatch(
    tmp_path: Path,
) -> None:
    topology = _topology()
    payload = record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "failed",
                "backend": "wav_artifact",
                "playback_id": "summed-playback-failed",
                "audio_emitted": False,
                "artifact": None,
                "tone": {"frequency_hz": 2500, "level_dbfs": -80},
                "issues": [{
                    "severity": "blocker",
                    "code": "tone_backend_failed",
                    "message": "tone artifact directory is not writable",
                }],
            },
        },
        state_path=tmp_path / "measurements.json",
        now="2026-06-18T20:00:00Z",
    )

    latest = payload["summary"]["latest_summed_tests"]["mono"]
    codes = {issue["code"] for issue in latest["issues"]}
    assert latest["target_output_indices"] == []
    assert "tone_backend_failed" in codes
    assert "summed_test_artifact_missing" in codes
    assert "summed_test_output_mismatch" not in codes


def test_summed_test_output_mismatch_requires_inspectable_artifact(
    tmp_path: Path,
) -> None:
    topology = _topology()
    payload = record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "wav_artifact",
                "playback_id": "summed-playback-wrong-output",
                "audio_emitted": False,
                "artifact": {
                    "wav_basename": "tone_wrong.wav",
                    "metadata_basename": "tone_wrong.json",
                    "target_output_indices": [0],
                    "channel_count": 2,
                },
                "tone": {"frequency_hz": 2500, "level_dbfs": -80},
                "issues": [],
            },
        },
        state_path=tmp_path / "measurements.json",
        now="2026-06-18T20:01:00Z",
    )

    latest = payload["summary"]["latest_summed_tests"]["mono"]
    assert latest["target_output_indices"] == [0]
    assert latest["expected_output_indices"] == [0, 1]
    assert "summed_test_output_mismatch" in {
        issue["code"] for issue in latest["issues"]
    }


def test_measurement_state_lists_active_driver_and_summed_targets(
    tmp_path: Path,
) -> None:
    topology = _topology()
    payload = load_measurement_state(
        topology,
        state_path=tmp_path / "measurements.json",
    )

    assert [target["target_id"] for target in active_driver_targets(topology)] == [
        "mono:woofer",
        "mono:tweeter",
    ]
    assert [target["speaker_group_id"] for target in active_summed_targets(topology)] == [
        "mono",
    ]
    assert payload["status"] == "needs_driver_measurements"
    assert payload["summary"]["required_driver_count"] == 2
    assert payload["permissions"]["may_not_play_audio"] is True
    assert payload["permissions"]["may_not_load_camilla"] is True


def test_driver_measurement_counts_correct_driver_without_requiring_mic(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    missing_mic = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-1",
            "test_level_dbfs": -72,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-1",
        ),
        state_path=state_path,
        now="2026-06-14T12:00:00Z",
    )
    latest = missing_mic["summary"]["latest_driver_measurements"]["mono:woofer"]

    assert latest["captured"] is True
    assert "driver_measurement_mic_missing" in {
        issue["code"] for issue in latest["issues"]
    }
    assert missing_mic["summary"]["driver_measurements_complete"] is False
    assert missing_mic["summary"]["captured_driver_count"] == 1

    captured = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-2",
            "test_level_dbfs": -68,
            "observed_mic_dbfs": -42.5,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-2",
        ),
        state_path=state_path,
        now="2026-06-14T12:01:00Z",
    )

    assert captured["summary"]["latest_driver_measurements"]["mono:woofer"][
        "captured"
    ] is True
    assert captured["summary"]["captured_driver_count"] == 1


def test_confirmed_driver_roles_are_current_topology_captured_roles(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-woofer",
            "test_level_dbfs": -72,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer",
        ),
        state_path=state_path,
    )
    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "tweeter",
            "outcome": "heard_wrong_driver",
            "playback_id": "playback-tweeter",
            "test_level_dbfs": -72,
        },
        safe_session=_safe_session(
            role="tweeter",
            output_index=1,
            playback_id="playback-tweeter",
        ),
        state_path=state_path,
    )

    assert confirmed_driver_roles(
        topology,
        speaker_group_id="mono",
        state_path=state_path,
    ) == ["woofer"]
    assert confirmed_driver_roles(
        _topology(tweeter_output=2),
        speaker_group_id="mono",
        state_path=state_path,
    ) == ["woofer"]


def _current_woofer_floor_state(tmp_path: Path):
    topology = _topology()
    state = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-woofer",
            "observed_mic_dbfs": -42.0,
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer",
        ),
        state_path=tmp_path / "measurements.json",
    )
    return topology, state


def test_current_driver_floor_evidence_accepts_exact_current_target(
    tmp_path: Path,
) -> None:
    topology, state = _current_woofer_floor_state(tmp_path)

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is True
    assert evidence["source"] == "durable_current_driver_measurement"
    assert evidence["playback_id"] == "playback-woofer"


def test_current_driver_floor_evidence_rejects_forged_matching_output(
    tmp_path: Path,
) -> None:
    topology, state = _current_woofer_floor_state(tmp_path)
    record = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    # Reproducer: both the record and embedded confirmation agree on 999, but
    # the current topology owns output 0. Agreement with oneself is not enough.
    record["output_index"] = 999
    record["floor_confirmation"]["target"]["output_index"] = 999

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is False
    assert evidence["reason"] == "driver_floor_confirmation_invalid"


@pytest.mark.parametrize(
    "malformed_issues",
    [{}, [None], [{}], [{"severity": "mystery"}]],
)
def test_current_driver_floor_evidence_rejects_malformed_issues_container(
    tmp_path: Path,
    malformed_issues,
) -> None:
    topology, state = _current_woofer_floor_state(tmp_path)
    record = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    record["issues"] = malformed_issues

    evidence = current_driver_floor_evidence(
        topology,
        state,
        speaker_group_id="mono",
        role="woofer",
    )

    assert evidence["valid"] is False
    assert evidence["reason"] == "driver_floor_confirmation_invalid"


def test_latest_wrong_driver_result_removes_confirmed_driver_role(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_correct_driver",
            "playback_id": "playback-woofer-ok",
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer-ok",
        ),
        state_path=state_path,
    )
    assert confirmed_driver_roles(
        topology,
        speaker_group_id="mono",
        state_path=state_path,
    ) == ["woofer"]

    record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "heard_wrong_driver",
            "playback_id": "playback-woofer-wrong",
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-woofer-wrong",
        ),
        state_path=state_path,
    )

    assert confirmed_driver_roles(
        topology,
        speaker_group_id="mono",
        state_path=state_path,
    ) == []


def test_summed_validation_waits_for_all_driver_measurements(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    blocked = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -39,
        },
        state_path=state_path,
        now="2026-06-14T12:00:00Z",
    )

    assert blocked["summary"]["summed_validation_complete"] is False
    assert blocked["summary"]["latest_summed_validations"]["mono"][
        "validated"
    ] is False
    assert "summed_validation_driver_measurements_missing" in {
        issue["code"]
        for issue in blocked["summary"]["latest_summed_validations"]["mono"]["issues"]
    }

    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42,
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role,
                output_index=output_index,
                playback_id=playback_id,
            ),
            state_path=state_path,
            now=f"2026-06-14T12:0{1 if role == 'woofer' else 2}:00Z",
        )
    missing_test = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "polarity": "normal",
            "delay_ms": 0,
        },
        state_path=state_path,
        now="2026-06-14T12:03:00Z",
    )

    assert missing_test["summary"]["summed_validation_complete"] is False
    assert "summed_validation_test_missing" in {
        issue["code"]
        for issue in missing_test["summary"]["latest_summed_validations"]["mono"][
            "issues"
        ]
    }

    blocked_test = _record_summed_test(
        topology,
        state_path,
        playback_id="summed-playback-blocked",
        audio_emitted=False,
        playback_issues=[{
            "severity": "blocker",
            "code": "summed_commission_load_failed",
            "message": "could not open the combined active-speaker test path",
        }],
    )
    assert "summed_commission_load_failed" in {
        issue["code"]
        for issue in blocked_test["summary"]["latest_summed_tests"]["mono"][
            "issues"
        ]
    }

    _record_summed_test(
        topology,
        state_path,
        playback_id="summed-playback-artifact",
        audio_emitted=False,
    )
    artifact_only = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "polarity": "normal",
            "delay_ms": 0,
            "summed_test_id": "summed-playback-artifact",
        },
        state_path=state_path,
        now="2026-06-14T12:03:30Z",
    )

    assert artifact_only["summary"]["summed_validation_complete"] is False
    assert "summed_validation_audio_missing" in {
        issue["code"]
        for issue in artifact_only["summary"]["latest_summed_validations"]["mono"][
            "issues"
        ]
    }

    _record_summed_test(topology, state_path, playback_id="summed-playback-audible")
    ready = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "polarity": "normal",
            "delay_ms": 0,
            "summed_test_id": "summed-playback-audible",
        },
        state_path=state_path,
        now="2026-06-14T12:04:00Z",
    )

    assert ready["status"] == "ready_for_baseline"
    assert ready["summary"]["driver_measurements_complete"] is True
    assert ready["summary"]["summed_validation_complete"] is True
    assert ready["permissions"]["may_compile_baseline"] is True

    reloaded = load_measurement_state(topology, state_path=state_path)
    assert reloaded["status"] == "ready_for_baseline"
    assert reloaded["summary"]["summed_validation_complete"] is True
    assert reloaded["summary"]["latest_summed_validations"]["mono"][
        "validated"
    ] is True

    superseded = _record_summed_test(
        topology,
        state_path,
        playback_id="summed-playback-newer",
    )

    assert superseded["status"] == "needs_summed_validation"
    assert superseded["summary"]["summed_validation_complete"] is False
    assert superseded["summary"]["validated_summed_group_count"] == 0
    assert superseded["permissions"]["may_compile_baseline"] is False
    assert (
        superseded["summary"]["latest_summed_tests"]["mono"]["summed_test_id"]
        == "summed-playback-newer"
    )
    assert (
        superseded["summary"]["latest_summed_validations"]["mono"]["summed_test_id"]
        == "summed-playback-audible"
    )


def test_summed_validation_accepts_operator_check_after_audible_test_without_mic(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role,
                output_index=output_index,
                playback_id=playback_id,
            ),
            state_path=state_path,
            now=f"2026-06-14T12:0{1 if role == 'woofer' else 2}:00Z",
        )
    _record_summed_test(topology, state_path, playback_id="summed-playback-audible")

    no_operator_check = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "summed_test_id": "summed-playback-audible",
        },
        state_path=state_path,
        now="2026-06-14T12:03:00Z",
    )
    latest = no_operator_check["summary"]["latest_summed_validations"]["mono"]
    assert latest["validated"] is False
    assert latest["operator_listening_check"] is False
    assert "summed_validation_mic_missing" in {
        issue["code"] for issue in latest["issues"]
    }

    operator_check = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "summed_test_id": "summed-playback-audible",
            "operator_listening_check": True,
        },
        state_path=state_path,
        now="2026-06-14T12:04:00Z",
    )
    latest = operator_check["summary"]["latest_summed_validations"]["mono"]

    assert operator_check["status"] == "ready_for_baseline"
    assert operator_check["summary"]["summed_validation_complete"] is True
    assert operator_check["permissions"]["may_compile_baseline"] is True
    assert latest["validated"] is True
    assert latest["operator_listening_check"] is True
    assert latest["observed_mic_dbfs"] is None
    assert latest["acoustic"] is None
    assert "summed_validation_mic_missing" in {
        issue["code"] for issue in latest["issues"]
    }


def test_summed_validation_accepts_backend_driver_target_proof(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    _record_summed_test(
        topology,
        state_path,
        playback_id="summed-playback-revalidate",
    )
    payload = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "polarity": "normal",
            "delay_ms": 0,
            "summed_test_id": "summed-playback-revalidate",
        },
        state_path=state_path,
        driver_target_proof_complete=True,
        now="2026-06-14T12:05:00Z",
    )

    latest = payload["summary"]["latest_summed_validations"]["mono"]
    assert latest["validated"] is True
    assert latest["driver_target_proof_complete"] is True
    assert "summed_validation_driver_measurements_missing" not in {
        issue["code"] for issue in latest["issues"]
    }
    # The low-level measurement summary still describes raw measurement state;
    # profile compilation composes this validation with the backend proof.
    assert payload["summary"]["driver_measurements_complete"] is False
    assert payload["summary"]["summed_validation_complete"] is False


def test_driver_measurement_requires_accepted_floor_result_for_same_target(
    tmp_path: Path,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    payload = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "tweeter",
            "outcome": "heard_correct_driver",
            "observed_mic_dbfs": -42,
            "playback_id": "playback-tweeter",
        },
        safe_session=_safe_session(
            role="woofer",
            output_index=0,
            playback_id="playback-tweeter",
        ),
        state_path=state_path,
    )
    latest = payload["summary"]["latest_driver_measurements"]["mono:tweeter"]

    assert latest["captured"] is False
    assert "driver_measurement_target_mismatch" in {
        issue["code"] for issue in latest["issues"]
    }
    assert payload["summary"]["driver_measurements_complete"] is False


def test_measurements_do_not_carry_across_output_topology_changes(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "measurements.json"
    original = _topology()
    for role in ("woofer", "tweeter"):
        output_index = 0 if role == "woofer" else 1
        playback_id = f"playback-{role}"
        record_driver_measurement(
            original,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42,
                "playback_id": playback_id,
            },
            safe_session=_safe_session(
                role=role,
                output_index=output_index,
                playback_id=playback_id,
            ),
            state_path=state_path,
        )

    changed = _topology(tweeter_output=2, tweeter_verified=False)
    payload = load_measurement_state(changed, state_path=state_path)

    assert payload["summary"]["driver_measurements_complete"] is False
    assert payload["summary"]["captured_driver_count"] == 1
    assert payload["summary"]["stale_driver_record_count"] == 1
    assert payload["summary"]["missing_driver_targets"] == [
        target for target in active_driver_targets(changed)
        if target["role"] == "tweeter"
    ]
    assert "stale_measurement_evidence_ignored" in {
        issue["code"] for issue in payload["issues"]
    }


def test_new_level_run_invalidates_prior_comparison_set(tmp_path: Path) -> None:
    state_path = tmp_path / "measurements.json"
    topology = _topology()
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }
    comparison_set = start_active_comparison_set(
        topology,
        profile_context_id="protected-profile",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
        state_path=state_path,
        now="2026-07-11T12:00:00Z",
    )

    assert load_measurement_state(topology, state_path=state_path)[
        "active_comparison_set"
    ] == comparison_set

    cleared = clear_active_comparison_set(topology, state_path=state_path)

    assert cleared["active_comparison_set"] is None
    assert load_measurement_state(topology, state_path=state_path)[
        "active_comparison_set"
    ] is None


def test_start_active_comparison_set_stamps_bundle_session_id(
    tmp_path: Path,
) -> None:
    """bundle_session_id joins a comparison set to a durable commissioning
    bundle (jasper.active_speaker.bundles) without becoming part of the
    fingerprinted, comparison-critical core — comparison_set_valid must
    still pass and the fingerprint must not depend on it."""

    from jasper.active_speaker.capture_geometry import (
        comparison_set_fingerprint,
        comparison_set_valid,
    )

    state_path = tmp_path / "measurements.json"
    topology = _topology()
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }

    with_bundle = start_active_comparison_set(
        topology,
        profile_context_id="protected-profile",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
        bundle_session_id="abc123def456",
        state_path=state_path,
        now="2026-07-11T12:00:00Z",
    )

    assert with_bundle["bundle_session_id"] == "abc123def456"
    assert comparison_set_valid(with_bundle) is True
    assert load_measurement_state(topology, state_path=state_path)[
        "active_comparison_set"
    ] == with_bundle

    # bundle_session_id sits outside _COMPARISON_SET_CORE_KEYS: changing it
    # (or removing it) on the SAME comparison set must not move the
    # fingerprint comparison_set_fingerprint recomputes.
    mutated = {**with_bundle, "bundle_session_id": "a-totally-different-id"}
    assert comparison_set_fingerprint(mutated) == with_bundle["fingerprint"]
    dropped = {k: v for k, v in with_bundle.items() if k != "bundle_session_id"}
    assert comparison_set_fingerprint(dropped) == with_bundle["fingerprint"]

    without_bundle = start_active_comparison_set(
        topology,
        profile_context_id="protected-profile",
        setup_sha256="a" * 64,
        device_sha256="b" * 64,
        calibration_id="",
        driver_level_locks=driver_level_locks,
        state_path=state_path,
        now="2026-07-11T12:05:00Z",
    )

    assert "bundle_session_id" not in without_bundle


def test_driver_measurement_records_optional_bundle_ref(tmp_path: Path) -> None:
    """A recorded driver measurement carries the optional bundle join key
    ({session_id, artifact_path}) verbatim when a bundle is open, and stores
    None (not an absent key) when it is not — old state files without the
    key still round-trip through load_measurement_state."""

    topology = _topology(tweeter_output=1)
    state_path = tmp_path / "measurements.json"
    bundle_ref = {"session_id": "sess-1", "artifact_path": "captures/x.wav"}

    with_bundle = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "silent",
        },
        bundle_ref=bundle_ref,
        state_path=state_path,
        now="2026-07-11T12:00:00Z",
    )
    record_with_bundle = with_bundle["driver_measurements"][-1]
    assert record_with_bundle["bundle"] == bundle_ref

    without_bundle = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": "silent",
        },
        state_path=state_path,
        now="2026-07-11T12:01:00Z",
    )
    record_without_bundle = without_bundle["driver_measurements"][-1]
    assert record_without_bundle["bundle"] is None

    # Round-trips through a fresh load, including the pre-existing record
    # that has no "bundle" key at all in this state file's prior shape.
    reloaded = load_measurement_state(topology, state_path=state_path)
    assert reloaded["driver_measurements"][-2]["bundle"] == bundle_ref
    assert reloaded["driver_measurements"][-1]["bundle"] is None


def test_summed_validation_records_optional_bundle_ref(tmp_path: Path) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    _record_summed_test(topology, state_path)
    bundle_ref = {"session_id": "sess-2", "artifact_path": "summed/y.wav"}

    state = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "operator_listening_check": True,
            "summed_test_id": "summed-playback-1",
        },
        bundle_ref=bundle_ref,
        state_path=state_path,
        now="2026-06-14T12:03:00Z",
    )

    record = state["summed_validations"][-1]
    assert record["bundle"] == bundle_ref


def test_recorded_driver_record_is_the_repeat_aggregate(tmp_path: Path) -> None:
    """Step 2: when commissioning_capture.aggregate_driver_repeats decided a
    winner across N repeat captures, the SINGLE driver measurement recorded
    is that winner -- with the SC-4 repeats summary attached -- and the
    latest-wins pointer (summary.latest_driver_measurements) resolves to
    THAT aggregate record. Per-repeat evidence beyond the compact
    per_repeat[] summary lives only in the bundle, never duplicated into
    measurement state."""

    from jasper.active_speaker.commissioning_capture import aggregate_driver_repeats

    topology = _topology(tweeter_output=1)
    state_path = tmp_path / "measurements.json"

    def repeat(level: float, path: str) -> dict:
        return {
            "verdict": "heard_correct_driver",
            "acoustic": {"observed_mic_dbfs": level, "mic_clipping": False},
            "artifact_path": path,
        }

    repeats = [
        repeat(-30.0, "repeat_captures/r0.wav"),
        repeat(-30.2, "repeat_captures/r1.wav"),
        repeat(-29.9, "repeat_captures/r2.wav"),
    ]
    aggregate = aggregate_driver_repeats(repeats)
    assert aggregate["accepted"] == 3
    winner = aggregate["aggregate_repeat"]
    assert winner is not None

    state = record_driver_measurement(
        topology,
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "outcome": winner["verdict"],
            "acoustic": winner["acoustic"],
            "playback_id": "play-1",
            "repeats": aggregate,
        },
        safe_session=_safe_session(
            role="woofer", output_index=0, playback_id="play-1"
        ),
        state_path=state_path,
        now="2026-07-11T12:00:00Z",
    )

    record = state["driver_measurements"][-1]
    assert record["repeats"]["repeat_group_id"] == aggregate["repeat_group_id"]
    assert record["repeats"]["accepted"] == 3
    assert record["repeats"]["confidence"] == "normal"
    assert len(record["repeats"]["per_repeat"]) == 3

    # The latest-wins pointer resolves to this exact aggregate record.
    latest = state["summary"]["latest_driver_measurements"]["mono:woofer"]
    assert latest is record
    assert latest["repeats"]["repeat_group_id"] == aggregate["repeat_group_id"]

    # Per-repeat evidence is index-only (verdict/accepted/reject_reason/
    # artifact_path/...) -- no full acoustic curves for the non-winning
    # repeats leak into measurement state; that lives only in the bundle.
    for entry in record["repeats"]["per_repeat"]:
        assert "acoustic" not in entry
        assert "fr_curve" not in entry
        assert set(entry) == {
            "index",
            "verdict",
            "accepted",
            "reject_reason",
            "artifact_path",
            "estimated_snr_db",
            "clipping",
            "above_validity_floor",
            "level_dbfs",
        }


# --- lifecycle events (lane E, docs/active-crossover-information-design.md
# "Structured events") -------------------------------------------------------


def test_start_active_comparison_set_emits_session_started_event(
    tmp_path: Path, caplog,
) -> None:
    state_path = tmp_path / "measurements.json"
    topology = _topology()
    driver_level_locks = {
        target["target_id"]: {
            "target_id": target["target_id"],
            "speaker_group_id": target["speaker_group_id"],
            "role": target["role"],
            "tone_frequency_hz": 250.0 if target["role"] == "woofer" else 6250.0,
            "tone_peak_dbfs": -12.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        }
        for target in active_driver_targets(topology)
    }

    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.measurement",
    ):
        comparison_set = start_active_comparison_set(
            topology,
            profile_context_id="protected-profile",
            setup_sha256="a" * 64,
            device_sha256="b" * 64,
            calibration_id="cal-1",
            driver_level_locks=driver_level_locks,
            state_path=state_path,
            now="2026-07-11T12:00:00Z",
        )

    started = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("event=correction.crossover_session_started")
    ]
    assert len(started) == 1
    message = started[0]
    # group(s) via topology: _topology() has exactly one active group, "mono".
    assert "group=mono" in message
    assert "calibration_id=cal-1" in message
    assert f"comparison_set_fingerprint={comparison_set['fingerprint']}" in message
    # No bundle exists yet (SC-4 lands in a later lane), so session is omitted
    # rather than rendered as a literal "session=null".
    assert "session=" not in message


def test_start_active_comparison_set_raises_before_persisting_emits_no_event(
    tmp_path: Path, caplog,
) -> None:
    # Incomplete driver level locks raise before the state is ever persisted;
    # no event should fire for a call that never actually started a session.
    state_path = tmp_path / "measurements.json"
    topology = _topology()

    with caplog.at_level(
        logging.INFO, logger="jasper.active_speaker.measurement",
    ):
        with pytest.raises(ValueError, match="incomplete"):
            start_active_comparison_set(
                topology,
                profile_context_id="protected-profile",
                setup_sha256="a" * 64,
                device_sha256="b" * 64,
                calibration_id="",
                driver_level_locks={},
                state_path=state_path,
            )

    assert not any(
        r.getMessage().startswith("event=correction.crossover_session_started")
        for r in caplog.records
    )


# --- Paired summed evidence (lane E, Slice 2: "Retain both normal- and
# reverse-polarity summed evidence per crossover region") --------------------


def _summed_acoustic(
    *,
    null_depth_db: float,
    expect_null: bool,
    calibrated: bool = True,
) -> dict:
    return {
        "verdict": "blend_ok",
        "null_depth_db": null_depth_db,
        "expect_null": expect_null,
        "calibrated": calibrated,
    }


def test_reverse_capture_does_not_overwrite_in_phase_latest(tmp_path: Path) -> None:
    """The overwrite-bug regression this lane fixes: before pairing existed,
    ``latest_summed_by_group`` kept only ONE record per group regardless of
    polarity, so a reverse-polarity capture recorded after an in-phase one
    silently replaced it (both can read outcome='blend_ok'/validated=True --
    a formed reverse null IS the pass for a reverse capture). Both are now
    retained distinctly in latest_summed_pairs_by_group, while
    latest_summed_by_group / latest_summed_validations keep resolving to the
    IN-PHASE record specifically."""
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    _record_summed_test(topology, state_path, playback_id="summed-playback-1")

    in_phase = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "summed_test_id": "summed-playback-1",
            "polarity": "normal",
            "delay_ms": 0.0,
            "acoustic": _summed_acoustic(null_depth_db=2.0, expect_null=False),
        },
        state_path=state_path,
        driver_target_proof_complete=True,
        now="2026-07-11T12:00:00Z",
    )
    in_phase_record = in_phase["summed_validations"][-1]
    assert in_phase_record["validated"] is True

    reverse = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -55,
            "summed_test_id": "summed-playback-1",
            "polarity": "normal",
            "delay_ms": 0.0,
            "acoustic": _summed_acoustic(null_depth_db=22.0, expect_null=True),
        },
        state_path=state_path,
        driver_target_proof_complete=True,
        now="2026-07-11T12:01:00Z",
    )
    reverse_record = reverse["summed_validations"][-1]
    # Same shape as the bug this pins: both read validated=True.
    assert reverse_record["validated"] is True

    # The fix: latest_summed_by_group (== summary's latest_summed_validations,
    # the SAME object) still resolves to the IN-PHASE record.
    assert (
        reverse["latest_summed_by_group"]["mono"]["validation_id"]
        == in_phase_record["validation_id"]
    )
    assert (
        reverse["summary"]["latest_summed_validations"]["mono"]["validation_id"]
        == in_phase_record["validation_id"]
    )

    # Both polarities are retained, distinctly, in the paired evidence (2-way
    # legacy-fallback region key, since neither raw dict stamped "region").
    pair = reverse["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert pair["in_phase"]["validation_id"] == in_phase_record["validation_id"]
    assert pair["reverse"]["validation_id"] == reverse_record["validation_id"]

    # Reloading from disk re-derives the same (fresh-computed, not stored)
    # summary shape.
    reloaded = load_measurement_state(topology, state_path=state_path)
    assert (
        reloaded["latest_summed_by_group"]["mono"]["validation_id"]
        == in_phase_record["validation_id"]
    )
    reloaded_pair = reloaded["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert reloaded_pair["reverse"]["validation_id"] == reverse_record["validation_id"]


def test_in_phase_capture_after_reverse_does_not_lose_the_reverse_pair_slot(
    tmp_path: Path,
) -> None:
    """Symmetric to the above: capturing in-phase AFTER reverse must not
    clear the already-recorded reverse evidence out of the pair."""
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    _record_summed_test(topology, state_path, playback_id="summed-playback-1")

    reverse = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -55,
            "summed_test_id": "summed-playback-1",
            "acoustic": _summed_acoustic(null_depth_db=22.0, expect_null=True),
        },
        state_path=state_path,
        driver_target_proof_complete=True,
        now="2026-07-11T12:00:00Z",
    )
    reverse_record = reverse["summed_validations"][-1]
    # Before an in-phase capture exists at all, latest_summed_by_group has
    # nothing usable for this group yet -- a reverse-only capture never
    # counts as the flat "latest" slot.
    assert "mono" not in reverse["latest_summed_by_group"]

    in_phase = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "summed_test_id": "summed-playback-1",
            "acoustic": _summed_acoustic(null_depth_db=2.0, expect_null=False),
        },
        state_path=state_path,
        driver_target_proof_complete=True,
        now="2026-07-11T12:01:00Z",
    )
    in_phase_record = in_phase["summed_validations"][-1]

    assert (
        in_phase["latest_summed_by_group"]["mono"]["validation_id"]
        == in_phase_record["validation_id"]
    )
    pair = in_phase["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert pair["in_phase"]["validation_id"] == in_phase_record["validation_id"]
    assert pair["reverse"]["validation_id"] == reverse_record["validation_id"]


def test_summed_validation_persists_valid_region(tmp_path: Path) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    _record_summed_test(topology, state_path, playback_id="summed-playback-1")

    state = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "summed_test_id": "summed-playback-1",
            "acoustic": _summed_acoustic(null_depth_db=2.0, expect_null=False),
            "region": {
                "lower_role": "woofer",
                "upper_role": "tweeter",
                "fc_hz": 1600.0,
            },
        },
        state_path=state_path,
        driver_target_proof_complete=True,
    )

    record = state["summed_validations"][-1]
    assert record["region"] == {
        "lower_role": "woofer",
        "upper_role": "tweeter",
        "fc_hz": 1600.0,
    }
    pair = state["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert pair["in_phase"]["region"] == record["region"]


@pytest.mark.parametrize(
    "malformed_region",
    [
        None,
        {},
        "not a mapping",
        {"lower_role": "woofer"},  # missing upper_role/fc_hz
        {"lower_role": "woofer", "upper_role": "tweeter", "fc_hz": 0},  # non-positive
        {"lower_role": "woofer", "upper_role": "tweeter", "fc_hz": "nan"},
        {"lower_role": "", "upper_role": "tweeter", "fc_hz": 1600.0},  # empty role
        {"lower_role": "woofer", "upper_role": 5, "fc_hz": 1600.0},  # non-string role
    ],
)
def test_summed_validation_rejects_malformed_region(
    tmp_path: Path, malformed_region,
) -> None:
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    _record_summed_test(topology, state_path, playback_id="summed-playback-1")

    state = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "summed_test_id": "summed-playback-1",
            "acoustic": _summed_acoustic(null_depth_db=2.0, expect_null=False),
            "region": malformed_region,
        },
        state_path=state_path,
        driver_target_proof_complete=True,
    )

    record = state["summed_validations"][-1]
    assert record["region"] is None
    # Still pairs -- via the 2-way legacy fallback, since a malformed region
    # is treated exactly like an absent one.
    pair = state["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert pair["in_phase"]["validation_id"] == record["validation_id"]


def test_legacy_region_less_record_on_three_way_stays_out_of_pairs(
    tmp_path: Path,
) -> None:
    """A region-less record (no ``region`` stamped -- e.g. saved before this
    migration) has no unambiguous home on a 3-way (two candidate regions), so
    it is excluded from latest_summed_pairs_by_group entirely. It still
    counts toward latest_summed_by_group candidacy when in-phase, unchanged
    from prior behavior."""
    topology = _three_way_topology()
    state_path = tmp_path / "measurements.json"
    state = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -40,
            "acoustic": _summed_acoustic(null_depth_db=2.0, expect_null=False),
        },
        state_path=state_path,
        driver_target_proof_complete=True,
    )

    record = state["summed_validations"][-1]
    assert record["region"] is None
    assert state["latest_summed_by_group"]["mono"]["validation_id"] == (
        record["validation_id"]
    )
    # No region key can be inferred on a 3-way -- no pairs entry at all.
    assert state["latest_summed_pairs_by_group"].get("mono", {}) == {}


def test_operator_only_record_counts_as_in_phase_but_never_pairs(
    tmp_path: Path,
) -> None:
    """A pure operator-listening-check record (no acoustic block at all) has
    no polarity kind: it stays eligible for latest_summed_by_group (a
    validated blend with no null evidence, unchanged from before pairing
    existed) but can never contribute to a region's in-phase/reverse pair."""
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    _record_summed_test(topology, state_path, playback_id="summed-playback-1")

    state = record_summed_validation(
        topology,
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "summed_test_id": "summed-playback-1",
            "operator_listening_check": True,
        },
        state_path=state_path,
        driver_target_proof_complete=True,
    )

    record = state["summed_validations"][-1]
    assert record["acoustic"] is None
    assert record["validated"] is True
    assert state["latest_summed_by_group"]["mono"]["validation_id"] == (
        record["validation_id"]
    )
    assert state["latest_summed_pairs_by_group"].get("mono", {}) == {}
