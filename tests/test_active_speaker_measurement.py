# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.active_speaker.measurement import (
    active_driver_targets,
    active_summed_targets,
    confirmed_driver_roles,
    current_driver_floor_evidence,
    load_measurement_state,
    record_driver_measurement,
    record_summed_test_artifact,
    record_summed_validation,
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
