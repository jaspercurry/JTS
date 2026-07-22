# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.audio_hardware.usb_port_role import (
    MANAGED_BLOCK_BEGIN,
    UsbPortRoleState,
    configured_i2s_overlays,
    main,
    reconcile_boot_config,
    render_boot_config,
    resolve_usb_port_role,
)


ZERO = "Raspberry Pi Zero 2 W Rev 1.0"
PI5 = "Raspberry Pi 5 Model B Rev 1.0"
I2S = "[all]\ndtoverlay=hifiberry-dac8x\n"
PERIPHERAL = "[all]\ndtoverlay=dwc2,dr_mode=peripheral\n"
HOST = "[all]\ndtoverlay=dwc2,dr_mode=host\n"


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

    state, changed = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
    )
    first = config.read_text(encoding="utf-8")
    _, changed_again = reconcile_boot_config(
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
