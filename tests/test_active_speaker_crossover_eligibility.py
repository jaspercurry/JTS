# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy

import pytest

from jasper.active_speaker.capture_geometry import (
    comparison_set_fingerprint,
    driver_repeat_binding,
)
from jasper.active_speaker.crossover_eligibility import (
    automatic_measurement_eligibility,
    mapping_sequence,
    repeat_progress,
)


def _evidence() -> dict:
    comparison = {
        "schema_version": 2,
        "comparison_set_id": "1" * 32,
        "created_at": "2026-07-12T12:00:00Z",
        "topology_id": "topology-1",
        "profile_context_id": "profile-1",
        "setup_sha256": "2" * 64,
        "device_sha256": "3" * 64,
        "calibration_id": "",
        "driver_level_locks": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "speaker_group_id": "mono",
                "role": "woofer",
                "tone_frequency_hz": 100.0,
                "tone_peak_dbfs": -20.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": -18.0,
            }
        },
    }
    comparison["fingerprint"] = comparison_set_fingerprint(comparison)
    target = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": "6" * 64,
    }

    def proof(policy_id: str) -> dict:
        return {
            "schema_version": 1,
            "policy_id": policy_id,
            "accepted": True,
            "confirmation_source": "relay_begin_capture",
            "acknowledgement_binding_sha256": "4" * 64,
            "relay_session_id": "relay-woofer",
            "capture_protocol_version": 2,
            "capture_page_build": "20260712.1",
            "speaker_group_id": "mono",
            "role": "woofer",
            "target_fingerprint": "6" * 64,
            "comparison_set_id": comparison["comparison_set_id"],
            "comparison_set_fingerprint": comparison["fingerprint"],
        }

    def record(geometry: str) -> dict:
        fixed = geometry == "reference_axis"
        return {
            "speaker_group_id": "mono",
            "role": "woofer",
            "target_fingerprint": "6" * 64,
            "captured": True,
            "mic_clipping": False,
            "repeats": {
                "target": 3,
                "accepted": 3,
                "admission_attempts": 3,
            },
            "acoustic": {
                "capture_geometry": geometry,
                "verdict": "present",
                "mic_clipping": False,
                "gating": {
                    "applied": fixed,
                    "exempt_reason": None if fixed else "near_field",
                    "f_valid_floor_hz": 320.0 if fixed else None,
                },
                "overlap_levels": [{
                    "above_validity_floor": True,
                    "usable": True,
                }],
            },
            "placement_proof": proof(
                "driver_reference_axis_v1" if fixed else "driver_same_distance_v1"
            ),
        }

    bindings = dict(
        driver_repeat_binding(
            speaker_group_id="mono",
            role="woofer",
            target_fingerprint="6" * 64,
            capture_geometry=geometry,
        )
        for geometry in ("near_field", "reference_axis")
    )
    return {
        "topology_id": "topology-1",
        "profile_context_id": "profile-1",
        "driver_targets": [target],
        "measurements": {
            "active_comparison_set": comparison,
            "summary": {
                "latest_driver_measurements": {
                    "mono:woofer": record("near_field")
                },
                "latest_reference_axis_driver_measurements": {
                    "mono:woofer": record("reference_axis")
                },
            },
        },
        "repeat_state": {
            "targets": {
                target_id: {
                    "status": "completed",
                    "target_fingerprint": fingerprint,
                    "attempts": 3,
                    "results": [
                        {"attempt": attempt, "accepted": True}
                        for attempt in (1, 2, 3)
                    ],
                }
                for target_id, fingerprint in bindings.items()
            }
        },
    }


def test_automatic_measurement_eligibility_accepts_only_complete_current_evidence():
    result = automatic_measurement_eligibility(**_evidence())

    assert result.ready is True
    assert result.reason is None
    assert result.missing == ()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda data: data.update(topology_id="changed"),
        lambda data: data.update(profile_context_id="changed"),
        lambda data: data["measurements"]["summary"][
            "latest_driver_measurements"
        ]["mono:woofer"].pop("mic_clipping"),
        lambda data: data["measurements"]["summary"][
            "latest_driver_measurements"
        ]["mono:woofer"]["acoustic"]["gating"].update({"applied": True}),
        lambda data: data["measurements"]["summary"][
            "latest_driver_measurements"
        ]["mono:woofer"]["acoustic"].update({"overlap_levels": "bad"}),
        lambda data: data["measurements"]["summary"][
            "latest_reference_axis_driver_measurements"
        ]["mono:woofer"]["acoustic"]["gating"].update(
            {"f_valid_floor_hz": None}
        ),
        lambda data: data["measurements"]["summary"][
            "latest_reference_axis_driver_measurements"
        ]["mono:woofer"].update({"placement_proof": {}}),
        lambda data: data.update(repeat_state={"targets": "bad"}),
        lambda data: data["repeat_state"]["targets"][
            "reference_axis/mono:woofer"
        ].update({"target_fingerprint": "f" * 64}),
    ),
)
def test_automatic_measurement_eligibility_fails_closed(mutate):
    data = copy.deepcopy(_evidence())
    mutate(data)

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert result.reason is not None
    assert result.missing


def test_completed_controller_cannot_authorize_two_accepted_acoustic_repeats():
    data = _evidence()
    data["measurements"]["summary"]["latest_driver_measurements"][
        "mono:woofer"
    ]["repeats"]["accepted"] = 2

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert "near_field:mono:woofer" in result.missing


def test_completed_controller_refuses_two_accepted_or_wrong_target():
    data = _evidence()
    entry = data["repeat_state"]["targets"]["reference_axis/mono:woofer"]
    entry.update({"accepted": 2, "target": 4})

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert "repeat:reference_axis:mono:woofer" in result.missing


@pytest.mark.parametrize(
    ("capture_geometry", "summary_key"),
    (
        ("near_field", "latest_driver_measurements"),
        ("reference_axis", "latest_reference_axis_driver_measurements"),
    ),
)
def test_acoustic_aggregate_refuses_fabricated_four_accepted(
    capture_geometry, summary_key
):
    data = _evidence()
    data["measurements"]["summary"][summary_key]["mono:woofer"]["repeats"][
        "accepted"
    ] = 4

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert f"{capture_geometry}:mono:woofer" in result.missing


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_completed_controller_refuses_fabricated_four_accepted(
    capture_geometry,
):
    data = _evidence()
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    entry = data["repeat_state"]["targets"][target_id]
    entry.update({
        "attempts": 4,
        "accepted": 4,
        "target": 3,
        "results": [
            {"attempt": attempt, "accepted": True}
            for attempt in (1, 2, 3, 4)
        ],
    })

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert f"repeat:{capture_geometry}:mono:woofer" in result.missing


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
@pytest.mark.parametrize(
    ("attempts", "results", "expected_ready"),
    (
        (
            3,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            True,
        ),
        (
            3,
            [{"attempt": 1, "accepted": True}] * 3,
            False,
        ),
        (
            3,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
                7,
            ],
            False,
        ),
        (
            3,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            False,
        ),
        (
            4,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            False,
        ),
        (
            4,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": False},
                {"attempt": 3, "accepted": True},
                {"attempt": 4, "accepted": True},
            ],
            True,
        ),
    ),
)
def test_completed_controller_requires_exact_attempt_coverage(
    capture_geometry, attempts, results, expected_ready
):
    data = _evidence()
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    data["repeat_state"]["targets"][target_id].update(
        {
            "attempts": attempts,
            "results": results,
        }
    )

    result = automatic_measurement_eligibility(**data)

    assert result.ready is expected_ready
    missing_id = f"repeat:{capture_geometry}:mono:woofer"
    assert (missing_id in result.missing) is (not expected_ready)


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_completed_controller_refuses_inflight_reservation(capture_geometry):
    data = _evidence()
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    data["repeat_state"]["targets"][target_id]["inflight"] = "still-owned"

    result = automatic_measurement_eligibility(**data)

    assert result.ready is False
    assert f"repeat:{capture_geometry}:mono:woofer" in result.missing


def test_public_repeat_projection_preserves_completed_eligibility():
    from jasper.web.correction_crossover_backend import CrossoverLevelLease

    data = _evidence()
    store = CrossoverLevelLease()
    for target_id, entry in data["repeat_state"]["targets"].items():
        entry["target_id"] = target_id
        entry["inflight"] = None
    store.set_durable_repeat_progress(data["repeat_state"])
    data["repeat_state"] = store.repeat_snapshot()["durable"]

    result = automatic_measurement_eligibility(**data)

    assert result.ready is True


@pytest.mark.parametrize("value", (None, "bad", {}, 3, True))
def test_mapping_sequence_and_repeat_progress_reject_malformed_types(value):
    assert mapping_sequence(value) == ()

    progress = repeat_progress(
        {
            "targets": {
                "mono:woofer": {
                    "attempts": value,
                    "accepted": value,
                    "target": value,
                }
            },
            "failures": value,
        },
        "mono:woofer",
    )

    expected_count = 3 if type(value) is int else 0
    assert progress.attempts == expected_count
    assert progress.accepted == expected_count
    assert progress.target == 3
    assert progress.failure == {}
