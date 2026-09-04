# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.audio_hardware.dac import all_profiles
from jasper.audio_hardware.usb_port_role import (
    I2S_HAT_BLOCK_BEGIN,
    MANAGED_BLOCK_BEGIN,
    I2sHatCollision,
    UsbPortRoleState,
    configured_i2s_overlays,
    main,
    read_i2s_hat_intent,
    reconcile_boot_config,
    render_boot_config,
    render_i2s_hat_boot_config,
    resolve_usb_port_role,
    write_i2s_hat_intent,
)


ZERO = "Raspberry Pi Zero 2 W Rev 1.0"
PI5 = "Raspberry Pi 5 Model B Rev 1.0"
I2S = "[all]\ndtoverlay=hifiberry-dac8x\n"
PERIPHERAL = "[all]\ndtoverlay=dwc2,dr_mode=peripheral\n"
HOST = "[all]\ndtoverlay=dwc2,dr_mode=host\n"

I2S_PROFILES = tuple(p for p in all_profiles() if p.connection == "i2s")
I2S_PROFILE_IDS = tuple(p.id for p in I2S_PROFILES)


def _boot_paths(
    tmp_path: Path, *, model_text: str = PI5, boot_config: str = PERIPHERAL
):
    model, config, intent, udc = (
        tmp_path / name for name in ("model", "config.txt", "i2s_hat.env", "udc")
    )
    model.write_text(model_text, encoding="utf-8")
    config.write_text(boot_config, encoding="utf-8")
    udc.mkdir()
    return model, config, intent, udc


def _serialized_role(**overrides) -> dict[str, object]:
    raw: dict[str, object] = {
        "board_model": PI5,
        "board_topology": "separate_host_ports",
        "desired_role": "peripheral",
        "configured_role": "peripheral",
        "active_role": "peripheral",
        "gadget_available": True,
        "reboot_required": False,
        "reason": "available",
        "decision_reason": "dedicated_host_ports_leave_otg_available",
        "management_transport_available": True,
        "configured_i2s_overlays": [],
    }
    raw.update(overrides)
    return raw


def test_zero_without_registered_i2s_defaults_host_when_dac_is_absent() -> None:
    state = resolve_usb_port_role(
        board_model=ZERO,
        boot_config=HOST,
        active_role="host",
    )

    assert state.desired_role == "host"
    assert state.gadget_available is False
    assert state.management_transport_available is False
    assert state.reboot_required is False
    assert state.reason == "shared_otg_defaults_host_without_i2s"


def test_zero_observed_usb_dac_requires_shared_otg_host() -> None:
    state = resolve_usb_port_role(
        board_model=ZERO,
        boot_config=HOST,
        active_role="host",
        observed_output_profile_id="apple_usb_c_dongle",
    )

    assert state.desired_role == "host"
    assert state.reason == "shared_otg_usb_output_requires_host"


def test_zero_registered_i2s_allows_peripheral_even_before_card_appears() -> None:
    state = resolve_usb_port_role(
        board_model=ZERO,
        boot_config=I2S + PERIPHERAL,
        active_role="peripheral",
        observed_output_profile_id="unknown",
    )

    assert state.configured_i2s_overlays == ("hifiberry-dac8x",)
    assert state.desired_role == "peripheral"
    assert state.gadget_available is True
    assert state.management_transport_available is True
    assert state.reason == "available"


def test_pi5_separate_host_ports_allow_usb_dac_and_peripheral() -> None:
    state = resolve_usb_port_role(
        board_model=PI5,
        boot_config=PERIPHERAL,
        active_role="peripheral",
        observed_output_profile_id="apple_usb_c_dongle",
    )

    assert state.desired_role == "peripheral"
    assert state.gadget_available is True
    assert state.decision_reason == "dedicated_host_ports_leave_otg_available"


def test_legacy_zero_peripheral_role_is_pending_host_reboot() -> None:
    state = resolve_usb_port_role(
        board_model=ZERO,
        boot_config=PERIPHERAL,
        active_role="peripheral",
    )

    assert state.desired_role == "host"
    assert state.gadget_available is False
    assert state.reboot_required is True
    assert state.management_transport_available is True
    assert state.reason == "role_change_pending_reboot"


def test_unknown_board_is_fail_closed_and_never_requests_mutation() -> None:
    state = resolve_usb_port_role(
        board_model="Acme SBC",
        boot_config=PERIPHERAL,
        active_role="peripheral",
    )

    assert state.desired_role == "unknown"
    assert state.gadget_available is False
    assert state.reboot_required is False
    assert state.reason == "unsupported_board"
    assert render_boot_config(PERIPHERAL, state.desired_role) == PERIPHERAL


def test_i2s_overlay_parser_ignores_comments_and_non_applicable_sections() -> None:
    content = """\
# dtoverlay=hifiberry-dac8x
[cm5]
dtoverlay=hifiberry-dac8x
[all]
   dtoverlay = hifiberry-dac8x   # configured output
"""

    assert configured_i2s_overlays(content) == ("hifiberry-dac8x",)


def test_studio_dac8x_overlay_is_recognized_as_a_registered_i2s_hat() -> None:
    """A Studio-configured box must not read as "no I2S HAT present" (#2250).

    This parser intersects config.txt against the `dtoverlay` each registered
    profile declares, and USB port-role resolution consumes the result. While
    the Studio profile declared the BASE board's `hifiberry-dac8x`, a box
    correctly running the Studio's own overlay matched nothing here and looked
    like a speaker with no audio HAT at all.
    """
    content = "[all]\ndtoverlay=hifiberry-studio-dac8x\n"

    assert configured_i2s_overlays(content) == ("hifiberry-studio-dac8x",)
    # The two boards' overlays are distinct entries, not one shared string.
    assert configured_i2s_overlays(
        "[all]\ndtoverlay=hifiberry-dac8x\ndtoverlay=hifiberry-studio-dac8x\n"
    ) == ("hifiberry-dac8x", "hifiberry-studio-dac8x")
    # The PRO's overlay is deliberately NOT registered: no Pro profile exists,
    # and inventing one for hardware nobody owns is what #2250 warns against.
    assert configured_i2s_overlays(
        "[all]\ndtoverlay=hifiberry-studio-dac8x-pro\n"
    ) == ()


@pytest.mark.parametrize("profile", I2S_PROFILES, ids=I2S_PROFILE_IDS)
def test_i2s_hat_intent_round_trip(tmp_path: Path, profile) -> None:
    intent = tmp_path / "i2s_hat.env"

    assert read_i2s_hat_intent(intent) is None
    write_i2s_hat_intent(profile.id, intent)
    assert intent.read_text(encoding="utf-8") == f"JASPER_I2S_HAT_PROFILE={profile.id}\n"
    assert read_i2s_hat_intent(intent) == profile.id

    # Explicit "none" is a persisted, distinct state from the file never
    # having existed: it writes a marker, not an unlink (#i2s-hat-intent).
    write_i2s_hat_intent(None, intent)
    assert intent.is_file()
    assert intent.read_text(encoding="utf-8") == "JASPER_I2S_HAT_PROFILE=\n"
    assert read_i2s_hat_intent(intent) is None


def test_i2s_hat_intent_rejects_unsupported_profiles(tmp_path: Path) -> None:
    intent = tmp_path / "i2s_hat.env"
    non_i2s_id = next(p.id for p in all_profiles() if p.connection != "i2s")

    intent.write_text("JASPER_I2S_HAT_PROFILE=other_hat\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_i2s_hat_intent(intent)

    with pytest.raises(ValueError):
        write_i2s_hat_intent(non_i2s_id, intent)
    with pytest.raises(ValueError):
        write_i2s_hat_intent("not_a_real_profile", intent)


@pytest.mark.parametrize("profile", I2S_PROFILES, ids=I2S_PROFILE_IDS)
def test_i2s_hat_renderer_manages_only_global_overlay(profile) -> None:
    original = (
        "arm_64bit=1\n"
        "[cm5]\n"
        f"dtoverlay={profile.dtoverlay}\n"
        "[all]\n"
        "dtparam=audio=on\n"
    )

    enabled, enabled_changed, enabled_collision = render_i2s_hat_boot_config(
        original, profile.id
    )
    disabled, disabled_changed, disabled_collision = render_i2s_hat_boot_config(
        enabled, None
    )

    assert enabled.count(I2S_HAT_BLOCK_BEGIN) == 1
    assert enabled.count(f"dtoverlay={profile.dtoverlay}") == 2
    # Section-scoped ([cm5]) lines are out of the global/all overlay scan,
    # so they never collide with the managed block.
    assert enabled_changed is True
    assert enabled_collision is None
    assert "arm_64bit=1" in enabled and "dtparam=audio=on" in enabled
    assert f"[cm5]\ndtoverlay={profile.dtoverlay}" in disabled
    assert disabled.count(f"dtoverlay={profile.dtoverlay}") == 1
    assert I2S_HAT_BLOCK_BEGIN not in disabled
    assert disabled_changed is True
    assert disabled_collision is None


@pytest.mark.parametrize("profile", I2S_PROFILES, ids=I2S_PROFILE_IDS)
def test_i2s_hat_renderer_refuses_a_same_overlay_collision(profile) -> None:
    original = f"[all]\ndtoverlay={profile.dtoverlay}\ndtparam=audio=on\n"

    rendered, changed, collision = render_i2s_hat_boot_config(original, profile.id)

    # Two declarations of the same overlay is still two I2S machine drivers
    # as far as this renderer is concerned: refuse rather than compound the
    # hand-written line with a managed one.
    assert rendered == original
    assert changed is False
    assert collision == I2sHatCollision(
        managed_overlay=profile.dtoverlay,
        colliding_overlays=(profile.dtoverlay,),
    )

    # Clearing never refuses -- there is nothing to collide with when
    # removing JTS's own (nonexistent) block.
    cleared, cleared_changed, cleared_collision = render_i2s_hat_boot_config(
        original, None
    )
    assert I2S_HAT_BLOCK_BEGIN not in cleared
    assert f"dtoverlay={profile.dtoverlay}" in cleared
    assert cleared_changed is False
    assert cleared_collision is None


def test_i2s_hat_renderer_refuses_a_different_overlay_collision() -> None:
    mismatched, matching = I2S_PROFILES[0], I2S_PROFILES[1]
    original = f"[all]\ndtoverlay={mismatched.dtoverlay}\n"

    rendered, changed, collision = render_i2s_hat_boot_config(original, matching.id)

    assert rendered == original
    assert changed is False
    assert collision == I2sHatCollision(
        managed_overlay=matching.dtoverlay,
        colliding_overlays=(mismatched.dtoverlay,),
    )


def test_i2s_hat_renderer_rejects_non_i2s_and_unregistered_profiles() -> None:
    non_i2s_id = next(p.id for p in all_profiles() if p.connection != "i2s")

    with pytest.raises(ValueError):
        render_i2s_hat_boot_config("[all]\n", non_i2s_id)
    with pytest.raises(ValueError):
        render_i2s_hat_boot_config("[all]\n", "not_a_real_profile")


def test_reconcile_refuses_a_hand_written_overlay_collision(
    tmp_path: Path, capsys
) -> None:
    model, config, intent, udc = _boot_paths(tmp_path)
    config.write_text(
        "[all]\ndtoverlay=merus-amp\ndtoverlay=dwc2,dr_mode=peripheral\n",
        encoding="utf-8",
    )
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    (udc / "3f980000.usb").mkdir(parents=True)

    _, _, hat_changed, desired, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )

    # The hand-written line is never deleted or folded into a managed
    # block: two I2S machine drivers on one boot config is not a state
    # this writes, so it refuses and reports the collision instead.
    assert hat_changed is False
    assert desired == "innomaker_hifi_amp_pro"
    assert collision == I2sHatCollision(
        managed_overlay="merus-amp", colliding_overlays=("merus-amp",)
    )
    unchanged = config.read_text(encoding="utf-8")
    assert I2S_HAT_BLOCK_BEGIN not in unchanged
    assert unchanged.count("dtoverlay=merus-amp") == 1

    capsys.readouterr()
    result = main(
        [
            "--reconcile-boot",
            "--i2s-hat-intent-file",
            str(intent),
            "--model-file",
            str(model),
            "--boot-config",
            str(config),
            "--udc-class-dir",
            str(udc),
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert (
        "event=hardware.i2s_hat_boot_config_conflict "
        "managed_overlay=merus-amp colliding_overlays=merus-amp"
    ) in captured.err
    assert "i2s_hat_boot_config_conflict" not in captured.out


def test_hat_changed_and_durability_are_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    model, config, intent, udc = _boot_paths(tmp_path)
    config.write_text(PERIPHERAL, encoding="utf-8")
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    (udc / "3f980000.usb").mkdir(parents=True)

    _, changed, hat_changed, desired, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )

    assert changed is True
    assert hat_changed is True
    assert desired == "innomaker_hifi_amp_pro"
    assert collision is None
    assert I2S_HAT_BLOCK_BEGIN in config.read_text(encoding="utf-8")

    # A second reconcile with the same intent is a no-op.
    _, changed_again, hat_changed_again, _, _, collision_again = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )
    assert changed_again is False
    assert hat_changed_again is False
    assert collision_again is None

    invalid = config.read_text(encoding="utf-8").replace(
        "dtoverlay=merus-amp", "dtoverlay=merus-amp,unexpected=1"
    )
    config.write_text(invalid, encoding="utf-8")
    with pytest.raises(ValueError):
        reconcile_boot_config(
            model_path=model,
            boot_config_path=config,
            udc_class_dir=udc,
            i2s_hat_intent_path=intent,
        )
    assert config.read_text(encoding="utf-8") == invalid

    def publish_then_fail(path, text, **_kwargs):
        Path(path).write_text(text, encoding="utf-8")
        raise OSError("simulated directory fsync failure")

    config.write_text(PERIPHERAL, encoding="utf-8")
    monkeypatch.setattr(
        "jasper.audio_hardware.usb_port_role.atomic_write_text", publish_then_fail
    )
    monkeypatch.setenv("JASPER_PI_MODEL_FILE", str(model))
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(config))
    monkeypatch.setenv("JASPER_UDC_CLASS_DIR", str(udc))
    result = main(["--reconcile-boot", "--i2s-hat-intent-file", str(intent)])
    payload = json.loads(capsys.readouterr().out.splitlines()[0])

    assert result == 74
    assert payload["boot_config_published_not_durable"] is True
    assert "dtoverlay=merus-amp" in config.read_text(encoding="utf-8")


def test_unsupported_board_never_mutates_hat_boot_setting(tmp_path: Path) -> None:
    original = "[all]\ndtparam=audio=on\n"
    model, config, intent, udc = _boot_paths(
        tmp_path, model_text="Acme SBC", boot_config=original
    )
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)

    state, changed, hat_changed, _, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )

    assert state.board_topology == "unsupported"
    assert changed is False and hat_changed is False
    assert collision is None
    assert config.read_text(encoding="utf-8") == original


def test_missing_intent_file_is_the_explicit_opt_in_gate(tmp_path: Path) -> None:
    """No saved intent -> the I2S HAT boot lines are never touched.

    This is the jts3 incident's regression pin: a box with a hand-written
    `dtoverlay=` line and no `/var/lib/jasper/i2s_hat.env` must not have
    that line rewritten, added to, or removed by a reconcile pass —
    ownership is opt-in per box, not automatic for every registered
    profile (#i2s-hat-intent).
    """
    model, config, intent, udc = _boot_paths(
        tmp_path,
        boot_config="[all]\ndtoverlay=hifiberry-dac8x\ndtparam=audio=on\n",
    )
    assert not intent.exists()

    _, _, hat_changed, desired, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )

    assert desired is None
    assert hat_changed is False
    assert collision is None
    rendered = config.read_text(encoding="utf-8")
    assert I2S_HAT_BLOCK_BEGIN not in rendered
    assert "dtoverlay=hifiberry-dac8x" in rendered


def test_absent_intent_file_leaves_an_existing_managed_block_alone(
    tmp_path: Path,
) -> None:
    """An absent intent file (never saved) differs from an explicit "none".

    The former must not touch a managed block that already exists in
    config.txt -- only a present-and-empty intent file
    (write_i2s_hat_intent(None)) removes it (#i2s-hat-intent).
    """
    model, config, intent, udc = _boot_paths(tmp_path)
    config.write_text(PERIPHERAL, encoding="utf-8")
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    (udc / "3f980000.usb").mkdir(parents=True)
    reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )
    with_managed_block = config.read_text(encoding="utf-8")
    assert I2S_HAT_BLOCK_BEGIN in with_managed_block
    intent.unlink()

    _, changed, hat_changed, desired, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )

    assert desired is None
    assert hat_changed is False
    assert collision is None
    assert config.read_text(encoding="utf-8") == with_managed_block

    # A present-but-explicit-none intent, in contrast, DOES remove it.
    write_i2s_hat_intent(None, intent)
    _, _, explicit_hat_changed, explicit_desired, _, _ = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
    )
    assert explicit_desired is None
    assert explicit_hat_changed is True
    assert I2S_HAT_BLOCK_BEGIN not in config.read_text(encoding="utf-8")


def test_serialized_role_rejects_board_topology_mismatch() -> None:
    raw = _serialized_role(board_model=ZERO)

    assert UsbPortRoleState.from_mapping(raw) is None


def test_serialized_shared_peripheral_requires_registered_i2s_overlay() -> None:
    raw = _serialized_role(
        board_model=ZERO,
        board_topology="shared_otg_port",
        decision_reason="registered_i2s_leaves_otg_available",
    )

    assert UsbPortRoleState.from_mapping(raw) is None
    raw["configured_i2s_overlays"] = ["unregistered-overlay"]
    assert UsbPortRoleState.from_mapping(raw) is None


def test_serialized_i2s_overlays_are_normalized_after_validation() -> None:
    raw = _serialized_role(
        board_model=ZERO,
        board_topology="shared_otg_port",
        decision_reason="registered_i2s_leaves_otg_available",
        configured_i2s_overlays=[" HiFiBerry-DAC8x "],
    )

    state = UsbPortRoleState.from_mapping(raw)

    assert state is not None
    assert state.configured_i2s_overlays == ("hifiberry-dac8x",)


def test_serialized_shared_host_rejects_i2s_evidence() -> None:
    raw = _serialized_role(
        board_model=ZERO,
        board_topology="shared_otg_port",
        desired_role="host",
        configured_role="host",
        active_role="host",
        gadget_available=False,
        reason="shared_otg_defaults_host_without_i2s",
        decision_reason="shared_otg_defaults_host_without_i2s",
        management_transport_available=False,
        configured_i2s_overlays=["hifiberry-dac8x"],
    )

    assert UsbPortRoleState.from_mapping(raw) is None


def test_render_boot_config_migrates_legacy_role_and_is_idempotent() -> None:
    legacy = """\
arm_64bit=1

# JTS install — required for the composite USB gadget (management network +
# optional audio). Old installer prose.
[all]
dtoverlay=dwc2,dr_mode=peripheral
"""

    rendered = render_boot_config(legacy, "host")

    assert "arm_64bit=1" in rendered
    assert rendered.count("dtoverlay=dwc2,dr_mode=host") == 1
    assert "dtoverlay=dwc2,dr_mode=peripheral" not in rendered
    assert rendered.count(MANAGED_BLOCK_BEGIN) == 1
    assert render_boot_config(rendered, "host") == rendered


def _has_adjacent_empty_all_sections(text: str) -> bool:
    """True if two ``[all]`` headers appear with only blank lines between."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() != "[all]":
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and lines[cursor].strip().lower() == "[all]":
            return True
    return False


@pytest.mark.parametrize(
    "boot_config",
    [
        pytest.param("[cm4]\notg_mode=1\n\n[all]\nfoo=1\n", id="clean"),
        pytest.param(
            "[cm4]\notg_mode=1\n\n" + ("[all]\n\n" * 7) + "[all]\nfoo=1\n",
            id="stray_all_sections",
        ),
    ],
)
def test_render_boot_config_heals_stray_all_sections_and_is_idempotent(
    boot_config: str,
) -> None:
    once = render_boot_config(boot_config, "host")
    twice = render_boot_config(once, "host")

    assert twice == once
    assert once.count(MANAGED_BLOCK_BEGIN) == 1
    assert not _has_adjacent_empty_all_sections(once)


def test_render_boot_config_never_drops_a_commented_all_header() -> None:
    boot_config = "[cm4]\notg_mode=1\n\n[all]  # keep me\n\n[all]\nfoo=1\n"

    rendered = render_boot_config(boot_config, "host")

    assert "[all]  # keep me" in rendered


def test_reconcile_boot_config_preserves_unrelated_conditional_role(
    tmp_path: Path,
) -> None:
    model = tmp_path / "model"
    config = tmp_path / "config.txt"
    udc = tmp_path / "udc"
    model.write_text(ZERO, encoding="utf-8")
    config.write_text(
        "[cm5]\ndtoverlay=dwc2,dr_mode=host\n[all]\nfoo=1\n",
        encoding="utf-8",
    )
    udc.mkdir()

    state, changed, _, _, _, _ = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
    )
    first = config.read_text(encoding="utf-8")
    _, changed_again, _, _, _, _ = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
    )

    assert changed is True
    assert state.desired_role == "host"
    assert first.count("dtoverlay=dwc2,dr_mode=host") == 2
    assert "[cm5]\ndtoverlay=dwc2,dr_mode=host" in first
    assert changed_again is False
    assert config.read_text(encoding="utf-8") == first


def test_legacy_migration_never_consumes_intervening_hardware_directives() -> None:
    legacy = """\
# JTS install — required for the composite USB gadget (management network +
# optional audio). Old installer prose.
[all]
dtparam=i2c_arm=on
dtoverlay=hifiberry-dac8x
dtoverlay=dwc2,dr_mode=peripheral
"""

    rendered = render_boot_config(legacy, "host")

    assert "dtparam=i2c_arm=on" in rendered
    assert "dtoverlay=hifiberry-dac8x" in rendered
    assert rendered.count("dtoverlay=dwc2,dr_mode=host") == 1


def test_unbalanced_managed_block_fails_without_mutating_boot_config(
    tmp_path: Path,
) -> None:
    import pytest

    model = tmp_path / "model"
    config = tmp_path / "config.txt"
    udc = tmp_path / "udc"
    original = (
        f"[all]\n{MANAGED_BLOCK_BEGIN}\n"
        "# JTS hardware reconciliation: host mode.\n"
        "dtoverlay=dwc2,dr_mode=host\n"
    )
    model.write_text(ZERO, encoding="utf-8")
    config.write_text(original, encoding="utf-8")
    udc.mkdir()

    with pytest.raises(ValueError, match="missing its end marker"):
        reconcile_boot_config(
            model_path=model,
            boot_config_path=config,
            udc_class_dir=udc,
        )
    assert config.read_text(encoding="utf-8") == original


def test_bare_dwc2_is_migrated_but_unknown_parameters_fail_loudly() -> None:
    import pytest

    rendered = render_boot_config("[all]\ndtoverlay=dwc2\nfoo=1\n", "host")
    assert rendered.count("dtoverlay=dwc2,dr_mode=host") == 1
    assert "\ndtoverlay=dwc2\n" not in rendered
    assert "foo=1" in rendered

    with pytest.raises(ValueError, match="ambiguous.*dwc2"):
        render_boot_config(
            "[all]\ndtoverlay=dwc2,dr_mode=peripheral,g-rx-fifo-size=512\n",
            "host",
        )


def test_cli_config_normalization_does_not_claim_same_role_needs_reboot(
    tmp_path: Path,
    capsys,
) -> None:
    model = tmp_path / "model"
    config = tmp_path / "config.txt"
    udc = tmp_path / "udc"
    model.write_text(PI5, encoding="utf-8")
    config.write_text(PERIPHERAL, encoding="utf-8")
    (udc / "3f980000.usb").mkdir(parents=True)

    assert main(
        [
            "--reconcile-boot",
            "--model-file",
            str(model),
            "--boot-config",
            str(config),
            "--udc-class-dir",
            str(udc),
        ]
    ) == 0

    assert (
        "event=hardware.boot_config_changed reboot_required=0"
        in capsys.readouterr().out
    )
