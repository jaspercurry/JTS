# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
from pathlib import Path

from jasper import audio_runtime_plan as audio_plan
from jasper.audio_hardware.dac import APPLE_USB_C_DONGLE_ID
from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    AUDIO_ROUTE_PROFILE_KEY,
    FANIN_OUTPUT_BUFFER_KEY,
    FANIN_INPUT_RESAMPLER_KEY,
    FANIN_INPUT_RESAMPLER_LANE_KEY,
    MIN_FANIN_OUTPUT_BUFFER_FRAMES,
    OUTPUTD_CONTENT_BRIDGE_KEY,
    ROUTE_BITPERFECT_DECLARED,
    ROUTE_CORRECTED_48K,
    ROUTE_USB_LOW_LATENCY_48K,
    USBSINK_BLOCK_FRAMES_KEY,
    USBSINK_RING_PERIODS_KEY,
    apply_capture_precedence,
    DEFAULT_FANIN_INPUT_BUFFER_FRAMES,
    DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_PERIOD_FRAMES,
    DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES,
    OUTPUTD_CONTENT_BUFFER_KEY,
    OUTPUTD_DAC_BUFFER_KEY,
    OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER,
    OUTPUTD_PERIOD_KEY,
    build_audio_runtime_plan,
    coupling_supported_for_route,
    correction_latency_eligibility,
    correction_latency_eligibility_for_config,
    decide_source_low_latency_route,
    fanin_coupling_action,
    fanin_coupling_capture_kwargs,
    fanin_output_buffer_action,
    lean_capture_kwargs,
    low_latency_feature_flags,
    outputd_content_buffer_pair_error,
    outputd_dac_buffer_pair_error,
    outputd_env_buffer_pair_error,
    outputd_latency_floor_actions,
    resolve_audio_route_profile,
    route_owned_env_actions,
    resolve_fanin_output_buffer_target,
    transport_topology_for_coupling,
    usbsink_output_mode_action,
)
from jasper.env_load import EnvFileState
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    COUPLING_TRANSPORT_PIPE,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
    VALID_COUPLINGS,
)


ROOT = Path(__file__).resolve().parents[1]


def test_plan_uses_dac_profile_floor_as_intended_source():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
            "JASPER_OUTPUTD_PERIOD_FRAMES": "128",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "256",
        },
    )

    assert plan.setting("JASPER_CAMILLA_CHUNKSIZE").value == 256
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert plan.setting("JASPER_OUTPUTD_PERIOD_FRAMES").value == 128
    assert plan.setting("JASPER_OUTPUTD_DAC_BUFFER_FRAMES").value == 256
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").source_kind == "device_profile"
    assert plan.warnings == ()


def test_transport_pipe_plan_uses_effective_camilla_file_target():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        fanin_env={COUPLING_ENV_VAR: COUPLING_TRANSPORT_PIPE},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
            "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE": "/run/jasper-outputd/content.pipe",
        },
    )

    target = plan.setting("JASPER_CAMILLA_TARGET_LEVEL")
    assert target.value == 512
    assert target.source_kind == "route_policy"
    assert target.generated_value == "1536"
    assert "transport_pipe" in target.source
    assert any("2 x chunksize" in warning for warning in target.warnings)


def test_shm_ring_plan_uses_effective_ring_camilla_geometry():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
        },
    )

    chunksize = plan.setting("JASPER_CAMILLA_CHUNKSIZE")
    target = plan.setting("JASPER_CAMILLA_TARGET_LEVEL")
    assert chunksize.value == RING_CAMILLA_CHUNKSIZE
    assert chunksize.source_kind == "route_policy"
    assert "shm_ring" in chunksize.source
    assert any("under shm_ring" in warning for warning in chunksize.warnings)
    assert target.value == RING_CAMILLA_TARGET_LEVEL
    assert target.source_kind == "route_policy"
    assert "shm_ring" in target.source
    assert any("under shm_ring" in warning for warning in target.warnings)


def test_operator_env_wins_but_duplicate_generated_home_warns():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    setting = plan.setting("JASPER_CAMILLA_CHUNKSIZE")
    assert setting.value == 512
    assert setting.source_kind == "operator_env"
    assert any("one knob has two homes" in warning for warning in plan.warnings)


def test_lab_override_wins_over_operator_and_profile_floor():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
        overrides={"JASPER_CAMILLA_CHUNKSIZE": "384"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        override_label="/var/lib/jasper/audio_runtime_overrides.json",
    )

    setting = plan.setting("JASPER_CAMILLA_CHUNKSIZE")
    assert setting.value == 384
    assert setting.source_kind == "lab_override"
    assert setting.override_value == "384"
    assert any("lab override" in warning for warning in plan.warnings)


def test_invalid_lab_override_is_ignored_with_warning():
    plan = build_audio_runtime_plan(
        overrides={"JASPER_CAMILLA_TARGET_LEVEL": "bad"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert any("audio_runtime_overrides" in warning and "invalid" in warning for warning in plan.warnings)


def test_stale_generated_floor_warns_against_device_profile():
    plan = build_audio_runtime_plan(
        outputd_env={
            "JASPER_CAMILLA_TARGET_LEVEL": "1024",
        },
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert any(
        "profile floor is 1536" in warning or "profile floor for" in warning
        for warning in plan.warnings
    )


def test_outputd_latency_floor_actions_set_profile_floor_when_no_operator_env():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={},
        outputd_env={},
    )

    assert [(a.action, a.key, a.value) for a in actions] == [
        ("set", "JASPER_CAMILLA_CHUNKSIZE", "256"),
        ("set", "JASPER_CAMILLA_TARGET_LEVEL", "1536"),
        ("set", "JASPER_OUTPUTD_PERIOD_FRAMES", "128"),
        ("unset", "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES", ""),
        ("set", "JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
    ]


def test_outputd_latency_floor_actions_set_usb_route_content_buffer():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"].action == "set"
    assert by_key["JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"].value == "1536"


def test_usb_low_latency_without_dac_floor_keeps_outputd_pair_coherent():
    plan = build_audio_runtime_plan(
        profile_id="",
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
    )

    period = plan.setting(OUTPUTD_PERIOD_KEY).value
    content_buffer = plan.setting(OUTPUTD_CONTENT_BUFFER_KEY).value
    assert period == DEFAULT_OUTPUTD_PERIOD_FRAMES
    assert content_buffer == DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
    assert content_buffer >= OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER * period
    assert any("suppressing the low-latency route buffer" in w for w in plan.warnings)

    actions = outputd_latency_floor_actions(
        profile_id="",
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={},
    )
    by_key = {action.key: action for action in actions}
    assert by_key[OUTPUTD_PERIOD_KEY].action == "unset"
    assert by_key[OUTPUTD_CONTENT_BUFFER_KEY].action == "unset"


def test_usb_low_latency_with_dac_floor_keeps_shipped_pair():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={
            OUTPUTD_PERIOD_KEY: "128",
            OUTPUTD_CONTENT_BUFFER_KEY: "1536",
        },
    )

    assert plan.setting(OUTPUTD_PERIOD_KEY).value == 128
    assert (
        plan.setting(OUTPUTD_CONTENT_BUFFER_KEY).value
        == DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
    )
    assert not any("suppressing the low-latency route buffer" in w for w in plan.warnings)


def test_python_outputd_buffer_contract_matches_rust_validator():
    config_rs = (ROOT / "rust" / "jasper-outputd" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )

    assert "fn validate_buffer(" in config_rs
    assert "period_frames.saturating_mul(2)" in config_rs
    assert "minimum ALSA jitter margin" in config_rs
    assert OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER == 2
    assert outputd_content_buffer_pair_error(
        period_frames=1024,
        content_buffer_frames=1536,
    ) == (
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    assert outputd_dac_buffer_pair_error(
        period_frames=1024,
        dac_buffer_frames=1024,
    ) == (
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    assert outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={
            OUTPUTD_CONTENT_BUFFER_KEY: "1536",
        },
    ) == (
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    assert outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={
            OUTPUTD_CONTENT_BUFFER_KEY: "4096",
            OUTPUTD_DAC_BUFFER_KEY: "1024",
        },
    ) == (
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )


def test_outputd_latency_floor_actions_unset_when_operator_env_owns_key():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].action == "unset"
    assert by_key["JASPER_CAMILLA_TARGET_LEVEL"].action == "set"


def test_outputd_latency_floor_actions_unset_when_profile_has_no_floor():
    actions = outputd_latency_floor_actions(
        profile_id="hifiberry_dac8x",
        base_env={},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
        },
    )

    assert {action.action for action in actions} == {"unset"}


def test_outputd_latency_floor_actions_use_lab_override():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={},
        overrides={"JASPER_CAMILLA_CHUNKSIZE": "384"},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].action == "set"
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].value == "384"


def test_bad_operator_value_is_ignored_and_warned():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_TARGET_LEVEL": "rough-test"},
        outputd_env={"JASPER_CAMILLA_TARGET_LEVEL": "1536"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert any("rough-test" in warning and "ignored" in warning for warning in plan.warnings)


def test_fanin_env_is_the_owned_home_for_fanin_buffer_tuning():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "2048"},
        fanin_env={"JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "1024"},
        route_mode="solo",
    )

    setting = plan.setting("JASPER_FANIN_OUTPUT_BUFFER_FRAMES")
    assert setting.value == 1024
    assert setting.source_kind == "generated_env"
    assert any("reconciler-owned home" in warning for warning in plan.warnings)


def test_fanin_output_buffer_action_sets_or_unsets_owned_key():
    set_action = fanin_output_buffer_action(2048)
    unset_action = fanin_output_buffer_action(None)

    assert (set_action.action, set_action.key, set_action.value) == (
        "set",
        FANIN_OUTPUT_BUFFER_KEY,
        "2048",
    )
    assert (unset_action.action, unset_action.key, unset_action.value) == (
        "unset",
        FANIN_OUTPUT_BUFFER_KEY,
        "",
    )


def test_fanin_output_buffer_action_rejects_below_floor():
    try:
        fanin_output_buffer_action(MIN_FANIN_OUTPUT_BUFFER_FRAMES - 1)
    except ValueError as exc:
        assert "below floor" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("below-floor fan-in output buffer was accepted")


def test_usbsink_output_mode_action_sets_owned_key():
    action = usbsink_output_mode_action("FIFO")

    assert (action.action, action.key, action.value) == (
        "set",
        "JASPER_USBSINK_OUTPUT_MODE",
        "fifo",
    )


def test_usbsink_output_mode_action_rejects_unknown_mode():
    try:
        usbsink_output_mode_action("loopback")
    except ValueError as exc:
        assert "invalid usbsink output mode" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("unknown usbsink output mode was accepted")


def test_audio_route_profile_defaults_to_corrected_safe_path():
    plan = build_audio_runtime_plan(route_mode="solo")

    assert plan.route_profile.route_id == ROUTE_CORRECTED_48K
    assert plan.route_profile.low_latency_claim is False
    assert plan.route_profile.camilla_required is True
    assert plan.route_profile.outputd_final_reference_required is True
    assert plan.route_config_hash
    assert plan.to_dict()["route_profile"]["route_id"] == ROUTE_CORRECTED_48K


def test_system_plan_warns_and_uses_process_route_when_base_env_unreadable(
    monkeypatch,
    tmp_path: Path,
):
    """jasper-control must not silently downgrade /state on EACCES.

    systemd injects /etc/jasper/jasper.env before dropping privileges. If the
    later fresh file read fails, the plan may use the process copy for the base
    route keys, but it must still surface the unreadable source-of-truth file.
    """

    def fake_read(path: str) -> EnvFileState:
        if path == "base.env":
            return EnvFileState(
                path,
                {},
                "unreadable",
                "PermissionError: [Errno 13] Permission denied",
            )
        return EnvFileState(path, {}, "missing")

    monkeypatch.setattr(audio_plan, "read_env_file_state", fake_read)
    monkeypatch.setenv(AUDIO_ROUTE_PROFILE_KEY, ROUTE_USB_LOW_LATENCY_48K)

    plan = audio_plan.build_audio_runtime_plan_from_system(
        base_env_path="base.env",
        outputd_env_path="outputd.env",
        fanin_env_path="fanin.env",
        grouping_env_path=str(tmp_path / "grouping.env"),
        overrides_path=str(tmp_path / "overrides.json"),
        output_hardware_state_path=str(tmp_path / "output_hardware.json"),
    )

    assert plan.route_profile.route_id == ROUTE_USB_LOW_LATENCY_48K
    assert any(
        "unreadable audio runtime base env file base.env" in warning
        for warning in plan.warnings
    )


def test_invalid_audio_route_profile_falls_back_with_warning():
    profile = resolve_audio_route_profile({AUDIO_ROUTE_PROFILE_KEY: "fastish"})

    assert profile.route_id == ROUTE_CORRECTED_48K
    assert any("invalid" in warning for warning in profile.warnings)


def test_usb_low_latency_route_requires_rust_fanin_resampler_and_reference():
    profile = resolve_audio_route_profile(
        {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    )
    actions = route_owned_env_actions(profile)
    by_key = {action.key: action for action in actions}

    assert profile.low_latency_claim is True
    assert profile.rust_usb_audio_required is True
    assert profile.fanin_input_resampler_required is True
    assert profile.camilla_required is True
    assert profile.outputd_final_reference_required is True
    assert profile.p95_budget_ms == 40.0
    assert profile.p99_budget_ms == 60.0
    assert by_key[FANIN_INPUT_RESAMPLER_KEY].value == "enabled"
    assert by_key[FANIN_INPUT_RESAMPLER_LANE_KEY].value == "usbsink"
    assert by_key["JASPER_USBSINK_AUDIO_IMPL"].action == "unset"
    assert by_key[USBSINK_BLOCK_FRAMES_KEY].value == "256"
    assert by_key[USBSINK_RING_PERIODS_KEY].value == "3"
    assert (
        by_key["JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES"].value
        == "1536"
    )


def test_usb_low_latency_route_identity_carries_planned_bridge_and_resampler():
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()

    assert identity["dac_profile_id"] == APPLE_USB_C_DONGLE_ID
    assert identity["route_config_hash"] == plan.route_config_hash
    assert identity["fanin_resampler_config"] == {
        "enabled": True,
        "lane": "usbsink",
        "target_frames": 512,
        "max_adjust_ppm": 500,
        "warmup_cushion_frames": 1536,
        "ring_frames": 4096,
    }
    assert identity["rust_bridge_config"]["implementation"] == "rust"
    assert identity["rust_bridge_config"]["period_frames"] == 256
    assert identity["rust_bridge_config"]["ring_periods"] == 3
    assert identity["outputd_config"]["JASPER_OUTPUTD_PERIOD_FRAMES"] == 128
    assert (
        identity["outputd_config"]["JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"]
        == DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
    )
    assert identity["uac2_gadget_attrs"]["c_sync"] == "async"


def test_usb_low_latency_route_rejects_legacy_low_latency_lab_paths():
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_TRANSPORT_PIPE},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "rate_match"},
        route_mode="solo",
    )

    assert any(
        "requires JASPER_FANIN_CAMILLA_COUPLING=loopback" in error
        for error in plan.errors
    )
    assert any(
        "requires JASPER_OUTPUTD_CONTENT_BRIDGE=direct" in error
        for error in plan.errors
    )


def test_route_config_hash_includes_route_owned_env_actions(monkeypatch):
    base_env = {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    default_plan = build_audio_runtime_plan(base_env=base_env, route_mode="solo")

    monkeypatch.setattr(audio_plan, "DEFAULT_USB_LOW_LATENCY_BLOCK_FRAMES", 128)
    changed_plan = build_audio_runtime_plan(base_env=base_env, route_mode="solo")

    assert changed_plan.route_latency_identity()["rust_bridge_config"][
        "period_frames"
    ] == 128
    assert changed_plan.route_config_hash != default_plan.route_config_hash


def test_route_config_hash_includes_active_camilla_config_hash(tmp_path):
    config = tmp_path / "camilla.yml"
    base_env = {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    config.write_text("filters: {}\n", encoding="utf-8")
    first = build_audio_runtime_plan(
        base_env=base_env,
        route_mode="solo",
        correction_config_path=str(config),
    )

    config.write_text("filters:\n  peq:\n    type: Biquad\n", encoding="utf-8")
    second = build_audio_runtime_plan(
        base_env=base_env,
        route_mode="solo",
        correction_config_path=str(config),
    )

    assert first.camilla_config_hash != second.camilla_config_hash
    assert first.route_config_hash != second.route_config_hash


def test_non_low_latency_route_disarms_rust_bridge_claiming_knobs():
    actions = route_owned_env_actions(ROUTE_CORRECTED_48K)
    by_key = {action.key: action for action in actions}

    assert by_key[FANIN_INPUT_RESAMPLER_KEY].action == "unset"
    assert by_key["JASPER_USBSINK_AUDIO_IMPL"].action == "unset"
    assert by_key[USBSINK_BLOCK_FRAMES_KEY].action == "unset"
    assert by_key["JASPER_USBSINK_OUTPUT_MODE"].value == "aloop"


def test_bitperfect_route_is_declared_but_inactive_and_aec_degraded():
    profile = resolve_audio_route_profile(
        {AUDIO_ROUTE_PROFILE_KEY: ROUTE_BITPERFECT_DECLARED}
    )

    assert profile.active is False
    assert profile.bitperfect is True
    assert profile.camilla_required is False
    assert profile.aec_reference_mode == "degraded_until_final_reference_proven"
    assert "inactive" in profile.blocking_reason


def test_lean_capture_kwargs_emit_plan_owned_rawfile_shape():
    kwargs = lean_capture_kwargs()

    assert kwargs["capture_pipe_path"] == "/run/jasper-usbsink/lean.pipe"
    assert kwargs["resampler_type"] == "AsyncSinc"
    assert kwargs["resampler_profile"] == "Balanced"
    assert kwargs["enable_rate_adjust"] is True


def test_fanin_coupling_capture_kwargs_explicit_intent_beats_env(monkeypatch):
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "loopback")
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_PIPE", "/run/custom.pipe")
    monkeypatch.setenv("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE", "/run/outputd.pipe")

    kwargs = fanin_coupling_capture_kwargs(COUPLING_TRANSPORT_PIPE)

    assert kwargs["capture_pipe_path"] == "/run/custom.pipe"
    assert kwargs["playback_pipe_path"] == "/run/outputd.pipe"
    assert kwargs["resampler_type"] is None
    assert kwargs["enable_rate_adjust"] is False
    assert kwargs["transport_paced_pipe"] is True
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", COUPLING_TRANSPORT_PIPE)
    assert fanin_coupling_capture_kwargs("loopback") == {}


def test_fanin_coupling_capture_kwargs_none_reads_coupling_file_fresh(monkeypatch):
    # DEFECT 1: coupling=None resolves the TOKEN from the persisted fanin.env SSOT
    # (read_persisted_coupling), NOT from os.environ. os.environ here says
    # transport_pipe, but the file-fresh SSOT drives the result. Pipe PATH overrides
    # still come from the live env (the persisted file names WHICH coupling only).
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "transport_pipe")  # stale
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_PIPE", "/run/custom.pipe")
    monkeypatch.setenv("JASPER_OUTPUTD_LOCAL_CONTENT_PIPE", "/run/outputd.pipe")

    # SSOT says transport_pipe -> pipe kwargs, honoring the live PATH overrides.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "transport_pipe",
    )
    kwargs = fanin_coupling_capture_kwargs(None)
    assert kwargs["capture_pipe_path"] == "/run/custom.pipe"
    assert kwargs["playback_pipe_path"] == "/run/outputd.pipe"

    # SSOT says loopback -> {}, even though os.environ still says transport_pipe.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    assert fanin_coupling_capture_kwargs(None) == {}


def test_fanin_coupling_capture_kwargs_none_explicit_env_ignores_file(monkeypatch):
    # An EXPLICIT env mapping stays authoritative (the reconciler/binder path
    # passes dict(os.environ) right after pre-syncing it): the persisted file is
    # NOT read. Fails loudly if the file reader is consulted.
    def _boom(*a, **k):
        raise AssertionError("explicit-env path must not read the persisted file")

    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling", _boom
    )
    assert fanin_coupling_capture_kwargs(None, env={}) == {}
    assert (
        fanin_coupling_capture_kwargs(
            None, env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"}
        ).get("capture_device")
        == "jts_ring_capture"
    )


def test_capture_precedence_applies_fanin_coupling_when_no_stronger_capture():
    base = {"enable_rate_adjust": True, "playback_pipe_path": None}
    coupling = {"capture_pipe_path": "/run/jasper-fanin/camilla.pipe"}

    merged = apply_capture_precedence(
        base,
        coupling,
        lean_capture_kwargs=None,
        member_kwargs=base,
    )

    assert merged["capture_pipe_path"] == "/run/jasper-fanin/camilla.pipe"
    assert base == {"enable_rate_adjust": True, "playback_pipe_path": None}


def test_capture_precedence_lean_and_grouped_sink_block_fanin_coupling():
    base = {"enable_rate_adjust": True, "playback_pipe_path": None}
    coupling = {
        "capture_pipe_path": "/run/jasper-fanin/camilla.pipe",
        "playback_pipe_path": "/run/jasper-outputd/content.pipe",
        "enable_rate_adjust": False,
        "transport_paced_pipe": True,
    }
    lean = {"capture_pipe_path": "/run/jasper-usbsink/lean.pipe"}
    grouped = {"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False}

    lean_result = apply_capture_precedence(
        base,
        coupling,
        lean_capture_kwargs=lean,
        member_kwargs=base,
    )
    grouped_result = apply_capture_precedence(
        grouped,
        coupling,
        lean_capture_kwargs=None,
        member_kwargs=grouped,
    )
    assert "capture_pipe_path" not in lean_result
    assert lean_result["playback_pipe_path"] is None
    assert "transport_paced_pipe" not in lean_result
    assert grouped_result["playback_pipe_path"] == "/run/snapfifo"
    assert "capture_pipe_path" not in grouped_result
    assert "transport_paced_pipe" not in grouped_result


def test_fanin_output_buffer_target_resolves_lab_override():
    assert resolve_fanin_output_buffer_target({}).frames == MIN_FANIN_OUTPUT_BUFFER_FRAMES
    assert (
        resolve_fanin_output_buffer_target(
            {"JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES": "2048"}
        ).frames
        == 2048
    )
    malformed = resolve_fanin_output_buffer_target(
        {"JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES": "bad"}
    )
    below = resolve_fanin_output_buffer_target(
        {"JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES": "512"}
    )
    assert malformed.frames == MIN_FANIN_OUTPUT_BUFFER_FRAMES
    assert malformed.warning_event == "fanin.adaptive_shrunk_frames_invalid"
    assert below.frames == MIN_FANIN_OUTPUT_BUFFER_FRAMES
    assert below.warning_event == "fanin.adaptive_shrunk_frames_below_floor"


def test_fanin_output_buffer_target_uses_runtime_override():
    target = resolve_fanin_output_buffer_target(
        {"JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES": "2048"},
        overrides={"JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "1536"},
    )

    assert target.frames == 1536


def test_fanin_coupling_is_transition_owned_not_lab_overrideable():
    plan = build_audio_runtime_plan(
        overrides={COUPLING_ENV_VAR: COUPLING_TRANSPORT_PIPE},
        route_mode="solo",
        override_label="/var/lib/jasper/audio_runtime_overrides.json",
    )

    setting = plan.setting(COUPLING_ENV_VAR)

    assert COUPLING_ENV_VAR not in AUDIO_RUNTIME_OVERRIDE_KEYS
    assert setting.value == COUPLING_LOOPBACK
    assert setting.source_kind == "packaged_default"
    assert setting.override_value is None
    assert any(
        "is ignored" in warning
        and "jasper-fanin-coupling-reconcile" in warning
        for warning in plan.warnings
    )


def test_plan_recognizes_shm_ring_lab_coupling_without_false_warning():
    # The Ring A lab flag: the plan reuses fanin_coupling's SSOT
    # (VALID_COUPLINGS), so setting JASPER_FANIN_CAMILLA_COUPLING=shm_ring
    # surfaces value=shm_ring and does NOT emit the spurious
    # "is not recognized; resolved to loopback" warning it used to when the plan
    # kept an independent {loopback, transport_pipe} set. This is the drift the
    # Ring A Rust half flagged: resolve_coupling recognized shm_ring while the
    # plan warned it did not.
    plan = build_audio_runtime_plan(
        route_mode="solo",
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
    )

    setting = plan.setting(COUPLING_ENV_VAR)
    assert setting.value == COUPLING_SHM_RING
    assert not any(
        "is not recognized" in warning for warning in plan.warnings
    ), plan.warnings


def test_plan_valid_couplings_is_fanin_coupling_ssot():
    # SSOT identity: the plan does not keep an independent set that can drift
    # from the resolver's recognized tokens. Both include shm_ring.
    from jasper import audio_runtime_plan

    assert audio_runtime_plan._VALID_COUPLINGS is VALID_COUPLINGS
    assert COUPLING_SHM_RING in VALID_COUPLINGS
    assert COUPLING_TRANSPORT_PIPE in VALID_COUPLINGS
    assert COUPLING_LOOPBACK in VALID_COUPLINGS


def test_transport_pipe_route_policy_blocks_active_leader_but_allows_solo():
    blocked = coupling_supported_for_route(COUPLING_TRANSPORT_PIPE, "active_leader")
    solo = coupling_supported_for_route(COUPLING_TRANSPORT_PIPE, "solo")

    assert blocked.supported is False
    assert blocked.reason == "fanin_transport_pipe_coupling_unsupported"
    assert solo.supported is True


def test_shm_ring_route_policy_blocks_every_grouping_enabled_mode():
    # BLOCKER 2: shm_ring is solo-stereo-only until ring v2 (P8). Arming it on a
    # box with grouping ENABLED (leader/follower/invalid) would strand the leader's
    # local output. The symmetric half of multiroom.reconcile's ring-armed-bond
    # gate — together they make ring ⟂ grouping fail-closed from both directions.
    for mode in ("active_leader", "active_follower", "invalid_grouping"):
        support = coupling_supported_for_route(COUPLING_SHM_RING, mode)
        assert support.supported is False, mode
        assert support.reason == "fanin_shm_ring_coupling_unsupported_while_grouped"
        assert support.detail  # a non-empty operator-facing reason
        assert support.coupling == COUPLING_SHM_RING


def test_shm_ring_route_policy_allows_solo_and_unknown():
    # solo = grouping off; unknown = a transient indeterminate grouping-config read
    # that must NOT refuse a legitimate solo arm (fail-safe direction).
    for mode in ("solo", "unknown"):
        support = coupling_supported_for_route(COUPLING_SHM_RING, mode)
        assert support.supported is True, mode


def test_transport_pipe_still_allowed_for_follower_and_invalid_grouping():
    # The shm_ring block must NOT accidentally widen the transport_pipe block:
    # transport_pipe is only refused for active_leader (its documented gap), so a
    # follower/invalid box keeps the existing transport_pipe support verdict.
    for mode in ("active_follower", "invalid_grouping", "solo", "unknown"):
        assert coupling_supported_for_route(COUPLING_TRANSPORT_PIPE, mode).supported


def test_fanin_coupling_action_blocks_shm_ring_for_grouped_route():
    action, support = fanin_coupling_action(COUPLING_SHM_RING, "active_follower")

    assert action is None
    assert support.supported is False
    assert support.reason == "fanin_shm_ring_coupling_unsupported_while_grouped"


def test_fanin_coupling_action_sets_supported_coupling():
    action, support = fanin_coupling_action(COUPLING_TRANSPORT_PIPE, "solo")

    assert support.supported is True
    assert action is not None
    assert (action.action, action.key, action.value) == (
        "set",
        "JASPER_FANIN_CAMILLA_COUPLING",
        COUPLING_TRANSPORT_PIPE,
    )


def test_fanin_coupling_action_blocks_unsupported_route():
    action, support = fanin_coupling_action(COUPLING_TRANSPORT_PIPE, "active_leader")

    assert action is None
    assert support.supported is False
    assert support.reason == "fanin_transport_pipe_coupling_unsupported"


def test_transport_topology_reports_loopback_and_transport_pipe_geometry():
    loopback = transport_topology_for_coupling("loopback").to_dict()
    pipe = transport_topology_for_coupling(
        COUPLING_TRANSPORT_PIPE,
        fanin_env={"JASPER_FANIN_CAMILLA_PIPE": "/run/custom-capture.pipe"},
        outputd_env={"JASPER_OUTPUTD_LOCAL_CONTENT_PIPE": "/run/custom-output.pipe"},
    ).to_dict()

    assert loopback["name"] == "loopback"
    assert loopback["outputd_content_source"] == "alsa"
    assert loopback["fanin_to_camilla"]["transport"] == "alsa_loopback"
    assert pipe["name"] == COUPLING_TRANSPORT_PIPE
    assert pipe["outputd_content_source"] == "local_pipe"
    assert pipe["fanin_to_camilla"]["path"] == "/run/custom-capture.pipe"
    assert pipe["camilla_to_outputd"]["path"] == "/run/custom-output.pipe"
    assert pipe["camilla_to_outputd"]["format"] == "S32_LE"
    assert pipe["camilla"]["enable_rate_adjust"] is False
    assert pipe["camilla"]["capture_resampler"] is None


def test_runtime_plan_to_dict_exposes_topology_and_correction_latency_gate():
    plan = build_audio_runtime_plan(
        fanin_env={COUPLING_ENV_VAR: COUPLING_TRANSPORT_PIPE},
        outputd_env={"JASPER_OUTPUTD_LOCAL_CONTENT_PIPE": "/run/content.pipe"},
        route_mode="solo",
    )
    payload = plan.to_dict()

    assert payload["transport_topology"]["name"] == COUPLING_TRANSPORT_PIPE
    assert payload["transport_topology"]["camilla_to_outputd"]["path"] == "/run/content.pipe"
    assert payload["correction_latency_eligibility"]["eligible"] is True
    assert (
        payload["correction_latency_eligibility"]["minimum_phase_or_iir"]
        is True
    )


def test_transport_pipe_plan_warns_when_outputd_pipe_env_missing():
    plan = build_audio_runtime_plan(
        fanin_env={COUPLING_ENV_VAR: COUPLING_TRANSPORT_PIPE},
        outputd_env={},
        outputd_env_label="/var/lib/jasper/outputd.env",
        route_mode="solo",
    )

    assert any(
        OUTPUTD_PIPE_PATH_ENV_VAR in warning
        and "jasper-fanin-coupling-reconcile transport_pipe" in warning
        for warning in plan.warnings
    )


def test_loopback_plan_warns_on_stale_outputd_pipe_env():
    plan = build_audio_runtime_plan(
        fanin_env={COUPLING_ENV_VAR: COUPLING_LOOPBACK},
        outputd_env={OUTPUTD_PIPE_PATH_ENV_VAR: "/run/jasper-outputd/content.pipe"},
        outputd_env_label="/var/lib/jasper/outputd.env",
        route_mode="solo",
    )

    assert any(
        "stale" in warning
        and OUTPUTD_PIPE_PATH_ENV_VAR in warning
        and "jasper-fanin-coupling-reconcile loopback" in warning
        for warning in plan.warnings
    )


def test_correction_latency_gate_blocks_unmeasured_or_high_delay_fir():
    iir = correction_latency_eligibility()
    minimum = correction_latency_eligibility(fir_mode="minimum_phase")
    unknown = correction_latency_eligibility(fir_mode="unknown")
    high = correction_latency_eligibility(
        fir_mode="linear_phase",
        measured_group_delay_ms=21.333,
    )
    measured_small = correction_latency_eligibility(
        fir_mode="mixed_phase",
        measured_group_delay_ms=4.0,
    )

    assert iir.eligible and iir.minimum_phase_or_iir
    assert minimum.eligible and minimum.minimum_phase_or_iir
    assert unknown.eligible is False
    assert unknown.blocking_reason == "fir_group_delay_unmeasured"
    assert high.eligible is False
    assert high.measured_group_delay_frames == 1024
    assert high.blocking_reason == "fir_group_delay_exceeds_low_latency_budget"
    assert measured_small.eligible is True
    assert measured_small.minimum_phase_or_iir is False


def test_correction_latency_gate_reads_active_fir_metadata(tmp_path):
    fir_dir = tmp_path / "fir"
    fir_dir.mkdir()
    (fir_dir / "linear.json").write_text(json.dumps({
        "mode": "linear_phase",
        "filter_group_delay_ms": 21.333,
    }))
    config = tmp_path / "correction.yml"
    config.write_text(
        "filters:\n"
        "  room_fir:\n"
        "    type: Conv\n"
        "    parameters:\n"
        "      filename: fir/linear.wav\n",
        encoding="utf-8",
    )

    verdict = correction_latency_eligibility_for_config(str(config))

    assert verdict.eligible is False
    assert verdict.measured_group_delay_frames == 1024
    assert verdict.blocking_reason == "fir_group_delay_exceeds_low_latency_budget"


def test_transport_pipe_plan_errors_on_high_latency_fir(tmp_path):
    fir_dir = tmp_path / "fir"
    fir_dir.mkdir()
    (fir_dir / "linear.json").write_text(json.dumps({
        "mode": "linear_phase",
        "filter_group_delay_ms": 21.333,
    }))
    config = tmp_path / "correction.yml"
    config.write_text(
        "filters:\n"
        "  room_fir:\n"
        "    type: Conv\n"
        "    parameters:\n"
        "      filename: fir/linear.wav\n",
        encoding="utf-8",
    )

    plan = build_audio_runtime_plan(
        fanin_env={COUPLING_ENV_VAR: COUPLING_TRANSPORT_PIPE},
        outputd_env={OUTPUTD_PIPE_PATH_ENV_VAR: "/run/jasper-outputd/content.pipe"},
        route_mode="solo",
        correction_config_path=str(config),
    )

    assert any("correction latency" in error for error in plan.errors)
    assert plan.to_dict()["correction_latency_eligibility"]["eligible"] is False


def test_correction_latency_gate_allows_peq_and_minimum_phase(tmp_path):
    peq = tmp_path / "peq.yml"
    peq.write_text("filters:\n  room_peq_1:\n    type: Biquad\n", encoding="utf-8")
    fir_dir = tmp_path / "fir"
    fir_dir.mkdir()
    (fir_dir / "minimum.json").write_text(json.dumps({
        "mode": "minimum_phase",
        "filter_group_delay_ms": 0.0,
    }))
    minimum = tmp_path / "minimum.yml"
    minimum.write_text(
        "filters:\n"
        "  room_fir:\n"
        "    type: Conv\n"
        "    parameters:\n"
        "      filename: fir/minimum.wav\n",
        encoding="utf-8",
    )

    assert correction_latency_eligibility_for_config(str(peq)).eligible is True
    verdict = correction_latency_eligibility_for_config(str(minimum))
    assert verdict.eligible is True
    assert verdict.minimum_phase_or_iir is True


def test_source_low_latency_route_is_usb_exclusive_only():
    enabled = decide_source_low_latency_route(
        active_sources=("usbsink",),
        winner="usbsink",
        enabled=True,
    )
    disabled = decide_source_low_latency_route(
        active_sources=("usbsink",),
        winner="usbsink",
        enabled=False,
    )
    mixed = decide_source_low_latency_route(
        active_sources=("airplay", "usbsink"),
        winner="usbsink",
        enabled=True,
    )

    assert (enabled.route, enabled.reason) == ("low_latency", "usb_exclusive")
    assert (disabled.route, disabled.reason) == ("buffered", "flag_off")
    assert (mixed.route, mixed.reason) == ("buffered", "not_exclusive")


def test_source_low_latency_route_reports_non_exclusive_edges():
    non_usb = decide_source_low_latency_route(
        active_sources=("airplay",),
        winner="airplay",
        enabled=True,
    )
    usb_not_winner = decide_source_low_latency_route(
        active_sources=("usbsink",),
        winner=None,
        enabled=True,
    )
    idle = decide_source_low_latency_route(
        active_sources=(),
        winner=None,
        enabled=True,
    )

    assert (non_usb.route, non_usb.reason) == ("buffered", "not_exclusive")
    assert (usb_not_winner.route, usb_not_winner.reason) == (
        "buffered",
        "non_usb_winner",
    )
    assert (idle.route, idle.reason) == ("buffered", "idle")


def test_source_low_latency_route_accepts_source_enum_values():
    from jasper.music_sources import Source

    decision = decide_source_low_latency_route(
        active_sources=[Source.USBSINK],
        winner=Source.USBSINK,
        enabled=True,
    )

    assert decision.route == "low_latency"
    assert decision.active_sources == ("usbsink",)
    assert decision.winner == "usbsink"


def test_low_latency_feature_flags_are_exact_opt_in_literals():
    on_values = ("enabled", "ENABLED", " enabled ")
    off_values = ("", "disabled", "1", "true")

    for value in on_values:
        flags = low_latency_feature_flags(
            {"JASPER_LEAN_LANE": value, "JASPER_FANIN_ADAPTIVE_BUFFER": value},
        )
        assert flags.lean_lane is True
        assert flags.adaptive_buffer is True

    for value in off_values:
        flags = low_latency_feature_flags(
            {"JASPER_LEAN_LANE": value, "JASPER_FANIN_ADAPTIVE_BUFFER": value},
        )
        assert flags.lean_lane is False
        assert flags.adaptive_buffer is False


def test_low_latency_feature_flags_default_off():
    flags = low_latency_feature_flags({})

    assert flags.lean_lane is False
    assert flags.adaptive_buffer is False


def test_packaged_systemd_defaults_match_plan_constants():
    outputd_unit = (ROOT / "deploy/systemd/jasper-outputd.service").read_text()
    fanin_unit = (ROOT / "deploy/systemd/jasper-fanin.service").read_text()

    assert _env_int(outputd_unit, "JASPER_OUTPUTD_PERIOD_FRAMES") == (
        DEFAULT_OUTPUTD_PERIOD_FRAMES
    )
    assert _env_int(outputd_unit, "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES") == (
        DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
    )
    assert _env_int(outputd_unit, "JASPER_OUTPUTD_DAC_BUFFER_FRAMES") == (
        DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES
    )
    assert _env_int(fanin_unit, "JASPER_FANIN_INPUT_BUFFER_FRAMES") == (
        DEFAULT_FANIN_INPUT_BUFFER_FRAMES
    )
    assert _env_int(fanin_unit, "JASPER_FANIN_OUTPUT_BUFFER_FRAMES") == (
        DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES
    )


def _env_int(text: str, key: str) -> int:
    match = re.search(rf'Environment="{re.escape(key)}=(\d+)"', text)
    assert match is not None, key
    return int(match.group(1))


# --- shm_ring route policy + transport topology (P2) -------------------------


def test_usb_low_latency_accepts_coherent_shm_ring_pair():
    # The coherent ring pair (Ring A + Ring B) must NOT error the plan — else a
    # ring-armed box's shipped low-latency claim goes permanently red (gap 8).
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        route_mode="solo",
    )
    assert plan.route_policy_errors == ()


def test_usb_low_latency_rejects_partial_ring_flip_fanin_only():
    # shm_ring fan-in + direct outputd = partial flip -> rejected.
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "direct"},
        route_mode="solo",
    )
    assert plan.route_policy_errors
    assert any("partial flip" in e or "shm_ring" in e for e in plan.route_policy_errors)


def test_usb_low_latency_rejects_partial_ring_flip_outputd_only():
    # loopback fan-in + shm_ring outputd = partial flip -> rejected.
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_LOOPBACK},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        route_mode="solo",
    )
    assert plan.route_policy_errors


def test_usb_low_latency_still_accepts_default_loopback_direct():
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        route_mode="solo",
    )
    assert plan.route_policy_errors == ()


def test_transport_topology_for_shm_ring_names_both_ring_devices():
    topo = transport_topology_for_coupling(
        COUPLING_SHM_RING, fanin_env={}, outputd_env={}
    ).to_dict()
    assert topo["name"] == COUPLING_SHM_RING
    assert topo["fanin_to_camilla"]["transport"] == "shm_ring"
    assert topo["fanin_to_camilla"]["camilla_capture_device"] == "jts_ring_capture"
    assert topo["camilla_to_outputd"]["transport"] == "shm_ring"
    assert topo["camilla_to_outputd"]["camilla_playback_device"] == "jts_ring_playback"
    assert topo["camilla"]["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert topo["camilla"]["target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert topo["camilla"]["queuelimit"] == RING_CAMILLA_QUEUELIMIT
    assert topo["camilla"]["enable_rate_adjust"] is False
    assert topo["outputd_content_source"] == "shm_ring"
