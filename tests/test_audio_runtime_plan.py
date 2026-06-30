# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

from jasper.audio_hardware.dac import APPLE_USB_C_DONGLE_ID
from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    FANIN_OUTPUT_BUFFER_KEY,
    MIN_FANIN_OUTPUT_BUFFER_FRAMES,
    DEFAULT_FANIN_INPUT_BUFFER_FRAMES,
    DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_PERIOD_FRAMES,
    build_audio_runtime_plan,
    coupling_supported_for_route,
    decide_source_low_latency_route,
    fanin_coupling_action,
    fanin_output_buffer_action,
    outputd_latency_floor_actions,
    resolve_fanin_output_buffer_target,
    usbsink_output_mode_action,
)
from jasper.fanin_coupling import COUPLING_ENV_VAR, COUPLING_FIFO, COUPLING_LOOPBACK


ROOT = Path(__file__).resolve().parents[1]


def test_plan_uses_dac_profile_floor_as_intended_source():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
            "JASPER_OUTPUTD_PERIOD_FRAMES": "256",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "512",
        },
    )

    assert plan.setting("JASPER_CAMILLA_CHUNKSIZE").value == 256
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert plan.setting("JASPER_OUTPUTD_PERIOD_FRAMES").value == 256
    assert plan.setting("JASPER_OUTPUTD_DAC_BUFFER_FRAMES").value == 512
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").source_kind == "device_profile"
    assert plan.warnings == ()


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
        ("set", "JASPER_OUTPUTD_PERIOD_FRAMES", "256"),
        ("set", "JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "512"),
    ]


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
        overrides={COUPLING_ENV_VAR: COUPLING_FIFO},
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


def test_fifo_route_policy_blocks_active_leader_but_allows_solo():
    blocked = coupling_supported_for_route(COUPLING_FIFO, "active_leader")
    solo = coupling_supported_for_route(COUPLING_FIFO, "solo")

    assert blocked.supported is False
    assert blocked.reason == "fanin_fifo_coupling_unsupported"
    assert solo.supported is True


def test_fanin_coupling_action_sets_supported_coupling():
    action, support = fanin_coupling_action("fifo", "solo")

    assert support.supported is True
    assert action is not None
    assert (action.action, action.key, action.value) == (
        "set",
        "JASPER_FANIN_CAMILLA_COUPLING",
        "fifo",
    )


def test_fanin_coupling_action_blocks_unsupported_route():
    action, support = fanin_coupling_action("fifo", "active_leader")

    assert action is None
    assert support.supported is False
    assert support.reason == "fanin_fifo_coupling_unsupported"


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


def test_packaged_systemd_defaults_match_plan_constants():
    outputd_unit = (ROOT / "deploy/systemd/jasper-outputd.service").read_text()
    fanin_unit = (ROOT / "deploy/systemd/jasper-fanin.service").read_text()

    assert _env_int(outputd_unit, "JASPER_OUTPUTD_PERIOD_FRAMES") == (
        DEFAULT_OUTPUTD_PERIOD_FRAMES
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
