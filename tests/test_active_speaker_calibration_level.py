# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from jasper.active_speaker.calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    DEFAULT_TEST_LEVEL_DBFS,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
    TEST_LEVEL_STEP_DB,
    calibration_level_payload,
    clamp_test_level_dbfs,
    classify_mic_meter,
    load_calibration_level_state,
    update_calibration_level_state,
)


def test_calibration_level_defaults_to_floor() -> None:
    payload = calibration_level_payload()

    assert payload["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert payload["test_signal"]["default_level_dbfs"] == DEFAULT_TEST_LEVEL_DBFS
    assert payload["test_signal"]["normal_system_volume_untouched"] is True
    assert payload["safety"]["operator_controls_level"] is True
    assert payload["safety"]["jts_enforces_bounds"] is True
    assert payload["safety"]["audible_ramp_step_is_bounded"] is True
    assert payload["software_gain_guard"]["audible_ramp_step_db"] == (
        AUDIBLE_RAMP_STEP_DB
    )
    assert payload["mic_meter"]["status"] == "unmeasured"


def test_clamp_test_level_dbfs_enforces_commissioning_bounds() -> None:
    assert clamp_test_level_dbfs(-100) == MIN_TEST_LEVEL_DBFS
    assert clamp_test_level_dbfs(-55) == -55
    assert clamp_test_level_dbfs(-10) == -10
    assert clamp_test_level_dbfs(6) == MAX_TEST_LEVEL_DBFS
    assert clamp_test_level_dbfs("not-a-number") == DEFAULT_TEST_LEVEL_DBFS


def test_classify_mic_meter_reports_usable_capture_window() -> None:
    assert classify_mic_meter(observed_dbfs=-70)["status"] == "too_quiet"
    assert classify_mic_meter(observed_dbfs=-50)["status"] == "low"
    assert classify_mic_meter(observed_dbfs=-30)["status"] == "usable"
    assert classify_mic_meter(observed_dbfs=-10)["status"] == "too_loud"


def test_classify_mic_meter_clipping_overrides_level() -> None:
    meter = classify_mic_meter(observed_dbfs=-30, clipping=True)

    assert meter["status"] == "clipping"
    assert meter["tone"] == "danger"
    assert meter["recommendation"] == "stop_or_lower"


def test_calibration_level_state_limits_large_upward_steps(tmp_path) -> None:
    path = tmp_path / "level.json"

    first = update_calibration_level_state(
        action="set",
        requested_level_dbfs=-55,
        state_path=path,
    )
    loaded = load_calibration_level_state(state_path=path)

    assert first["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS + 1
    assert first["issues"][0]["code"] == "upward_step_limited"
    assert loaded["test_signal"]["requested_level_dbfs"] == first["test_signal"][
        "requested_level_dbfs"
    ]


def test_calibration_level_state_supports_bounded_audible_ramp(tmp_path) -> None:
    path = tmp_path / "level.json"

    first = update_calibration_level_state(action="ramp", state_path=path)
    limited = update_calibration_level_state(
        action="ramp",
        requested_level_dbfs=-55,
        state_path=path,
    )

    assert first["last_action"] == "ramp"
    assert first["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB
    )
    assert first["applied_delta_db"] == AUDIBLE_RAMP_STEP_DB
    assert limited["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + 2 * AUDIBLE_RAMP_STEP_DB
    )
    assert limited["issues"][0]["code"] == "audible_ramp_step_limited"


def test_calibration_level_state_defaults_when_payload_is_invalid(tmp_path) -> None:
    path = tmp_path / "level.json"
    path.write_text("[]", encoding="utf-8")

    loaded = load_calibration_level_state(state_path=path)

    assert loaded["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert loaded["last_action"] == "default_floor"
    assert loaded["state_path"] == str(path)


def test_calibration_level_state_allows_lower_and_reset(tmp_path) -> None:
    path = tmp_path / "level.json"
    update_calibration_level_state(action="raise", state_path=path)
    update_calibration_level_state(action="raise", state_path=path)

    lowered = update_calibration_level_state(
        action="set",
        requested_level_dbfs=MIN_TEST_LEVEL_DBFS,
        state_path=path,
    )
    raised = update_calibration_level_state(action="raise", state_path=path)
    reset = update_calibration_level_state(action="stop", state_path=path)

    assert lowered["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert raised["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS + 1
    assert reset["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS


def test_calibration_level_state_resets_on_mic_clipping(tmp_path) -> None:
    path = tmp_path / "level.json"
    update_calibration_level_state(action="raise", state_path=path)

    clipped = update_calibration_level_state(
        action="raise",
        observed_mic_dbfs=-20,
        mic_clipping=True,
        state_path=path,
    )

    assert clipped["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert clipped["mic_meter"]["status"] == "clipping"
    assert clipped["issues"][0]["code"] == "mic_clipping_reset_to_floor"


def test_calibration_level_observation_preserves_guarded_level(tmp_path) -> None:
    path = tmp_path / "level.json"
    update_calibration_level_state(action="raise", state_path=path)
    update_calibration_level_state(action="raise", state_path=path)

    observed = update_calibration_level_state(
        action="observe",
        observed_mic_dbfs=-30.2,
        state_path=path,
    )

    assert observed["last_action"] == "observe"
    assert observed["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS + 2
    assert observed["applied_delta_db"] == 0
    assert observed["mic_meter"]["status"] == "usable"
    assert observed["mic_meter"]["observed_dbfs"] == -30.2


def test_calibration_level_observation_clipping_resets_to_floor(tmp_path) -> None:
    path = tmp_path / "level.json"
    update_calibration_level_state(action="raise", state_path=path)
    update_calibration_level_state(action="raise", state_path=path)

    clipped = update_calibration_level_state(
        action="observe",
        observed_mic_dbfs=-18,
        mic_clipping=True,
        state_path=path,
    )

    assert clipped["last_action"] == "clip_reset"
    assert clipped["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert clipped["mic_meter"]["status"] == "clipping"
    assert clipped["issues"][0]["code"] == "mic_clipping_reset_to_floor"


def test_calibration_level_same_run_round_trips(tmp_path) -> None:
    path = tmp_path / "level.json"

    first = update_calibration_level_state(
        action="raise", state_path=path, run_id="run-a"
    )
    second = update_calibration_level_state(
        action="raise", state_path=path, run_id="run-a"
    )
    loaded = load_calibration_level_state(state_path=path, run_id="run-a")

    assert first["run_id"] == "run-a"
    assert second["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + 2 * TEST_LEVEL_STEP_DB
    )
    assert loaded["test_signal"]["requested_level_dbfs"] == second["test_signal"][
        "requested_level_dbfs"
    ]
    assert loaded["run_id"] == "run-a"


def test_calibration_level_different_run_id_starts_at_floor(tmp_path) -> None:
    path = tmp_path / "level.json"

    update_calibration_level_state(action="raise", state_path=path, run_id="run-a")
    update_calibration_level_state(action="raise", state_path=path, run_id="run-a")

    loaded_other_run = load_calibration_level_state(state_path=path, run_id="run-b")
    next_step = update_calibration_level_state(
        action="raise", state_path=path, run_id="run-b"
    )

    assert loaded_other_run["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS
    )
    assert loaded_other_run["last_action"] == "default_floor"
    # A fresh run's first "raise" steps up from the floor, not from run-a's
    # accumulated level, even though run-a's level is still on disk.
    assert next_step["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + TEST_LEVEL_STEP_DB
    )
    assert next_step["run_id"] == "run-b"


def test_calibration_level_legacy_stateless_file_starts_at_floor(tmp_path) -> None:
    path = tmp_path / "level.json"
    # Simulate a pre-fix statefile: a valid payload with no "run_id" key.
    legacy = update_calibration_level_state(action="raise", state_path=path)
    legacy.pop("run_id", None)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = load_calibration_level_state(state_path=path, run_id="run-new")
    unscoped = load_calibration_level_state(state_path=path)

    assert loaded["test_signal"]["requested_level_dbfs"] == MIN_TEST_LEVEL_DBFS
    assert loaded["last_action"] == "default_floor"
    # A caller that doesn't pass run_id (existing read-only call sites) still
    # sees the unscoped persisted value, preserving prior behavior.
    assert unscoped["test_signal"]["requested_level_dbfs"] == (
        MIN_TEST_LEVEL_DBFS + TEST_LEVEL_STEP_DB
    )
