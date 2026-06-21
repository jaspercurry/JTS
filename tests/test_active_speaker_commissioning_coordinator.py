# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.active_speaker.commissioning_coordinator import (
    build_commissioning_view,
    summed_test_failure_message,
)

from tests.test_active_speaker_startup_load import _topology


def test_summed_test_failure_message_prioritizes_artifact_permission_failure():
    message = summed_test_failure_message([
        {
            "severity": "blocker",
            "code": "tone_backend_failed",
            "message": "Permission denied",
        },
        {
            "severity": "blocker",
            "code": "summed_test_output_mismatch",
            "message": "wrong outputs",
        },
    ])

    assert "could not prepare the combined test tone" in message
    assert "Confirm outputs" not in message


def test_commissioning_view_exposes_combined_test_as_next_action():
    view = build_commissioning_view(
        _topology(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {},
                "latest_summed_validations": {},
            },
        },
    )

    assert view["status"] == "needs_combined_check"
    assert view["next_action"]["id"] == "start_combined_test"
    group = view["combined_groups"][0]
    assert group["status"] == "ready_to_test"
    assert group["actions"]["start_combined_test"]["enabled"] is True
    assert group["actions"]["start_combined_test"]["body"]["level_dbfs"] == -80.0


def test_commissioning_view_records_result_after_audible_combined_test():
    view = build_commissioning_view(
        _topology(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "validated_summed_group_count": 0,
                "required_summed_group_count": 1,
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": True,
                        "summed_test_id": "summed-playback-audible",
                        "tone": {"level_dbfs": -74.0},
                        "issues": [],
                    },
                },
                "latest_summed_validations": {},
            },
        },
        calibration_level={
            "test_signal": {
                "requested_level_dbfs": -68.0,
                "min_level_dbfs": -80.0,
                "max_level_dbfs": -30.0,
                "step_db": 1.0,
            },
            "software_gain_guard": {
                "upward_step_limit_db": 6.0,
            },
        },
    )

    assert view["next_action"]["id"] == "record_combined_result"
    assert view["test_level"]["requested_level_dbfs"] == -74.0
    group = view["combined_groups"][0]
    assert group["status"] == "ready_to_record"
    assert group["test_level"]["requested_level_dbfs"] == -74.0
    assert group["actions"]["record_combined_result"]["enabled"] is True
    assert group["actions"]["record_combined_result"]["body"] == {
        "speaker_group_id": "mono",
        "summed_test_id": "summed-playback-audible",
        "operator_listening_check": True,
    }


def test_commissioning_view_uses_backend_failure_copy_for_combined_group():
    view = build_commissioning_view(
        _topology(),
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "captured_driver_check_count": 2,
                "required_driver_check_count": 2,
                "summed_validation_complete": False,
                "latest_summed_tests": {
                    "mono": {
                        "captured": False,
                        "audio_emitted": False,
                        "issues": [
                            {
                                "severity": "blocker",
                                "code": "tone_backend_failed",
                                "message": "Permission denied",
                            },
                            {
                                "severity": "blocker",
                                "code": "summed_test_output_mismatch",
                                "message": "wrong outputs",
                            },
                        ],
                    },
                },
                "latest_summed_validations": {},
            },
        },
    )

    group = view["combined_groups"][0]
    assert group["status"] == "test_failed"
    assert "could not prepare the combined test tone" in group["message"]
    assert "Confirm outputs" not in group["message"]
