# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.audio_hardware.config_txt import MANAGED_BLOCK_BEGIN, render_boot_config
from jasper.audio_hardware.i2s_hat import (
    I2S_HAT_BLOCK_BEGIN,
    I2sHatCollision,
    write_i2s_hat_intent,
)
from jasper.audio_hardware.usb_port_role import (
    UsbPortRoleState,
    reconcile_boot_config,
    resolve_usb_port_role,
)
from jasper.cli.usb_port_role import main

from ._hat_eeprom import write_hat_eeprom
from ._log_events import parse_event


def _stderr_events(stderr: str, name: str) -> list[dict[str, str]]:
    """Field maps of every ``event=<name>`` line in a captured stderr stream."""
    return [
        parsed[1]
        for parsed in (parse_event(line) for line in stderr.splitlines())
        if parsed is not None and parsed[0] == name
    ]


def _stderr_event(stderr: str, name: str) -> dict[str, str]:
    """The ONE ``event=<name>`` line's fields."""
    matched = _stderr_events(stderr, name)
    assert len(matched) == 1, matched
    return matched[0]


ZERO = "Raspberry Pi Zero 2 W Rev 1.0"
PI5 = "Raspberry Pi 5 Model B Rev 1.0"
I2S = "[all]\ndtoverlay=hifiberry-dac8x\n"
PERIPHERAL = "[all]\ndtoverlay=dwc2,dr_mode=peripheral\n"
HAND_WRITTEN_BASE = "[all]\ndtoverlay=hifiberry-dac8x\ndtparam=audio=on\n"
HOST = "[all]\ndtoverlay=dwc2,dr_mode=host\n"

def _boot_paths(
    tmp_path: Path, *, model_text: str = PI5, boot_config: str = PERIPHERAL
):
    model, config, intent, hat, udc = (
        tmp_path / name
        for name in ("model", "config.txt", "i2s_hat.env", "hat", "udc")
    )
    model.write_text(model_text, encoding="utf-8")
    config.write_text(boot_config, encoding="utf-8")
    udc.mkdir()
    return model, config, intent, hat, udc


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


def test_reconcile_refuses_a_hand_written_overlay_collision(
    tmp_path: Path, capsys
) -> None:
    model, config, intent, hat, udc = _boot_paths(tmp_path)
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
        hat_dir=hat,
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
            "--hat-dir",
            str(hat),
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert _stderr_event(captured.err, "hardware.i2s_hat_boot_config_conflict") == {
        "managed_overlay": "merus-amp",
        "colliding_overlays": "merus-amp",
    }
    # A refusal wrote nothing, so the conflict stands alone: no change event.
    assert _stderr_events(captured.err, "hardware.i2s_hat_boot_config_changed") == []
    assert "event=" not in captured.out


def test_hat_changed_and_durability_are_reported(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    model, config, intent, hat, udc = _boot_paths(tmp_path)
    config.write_text(PERIPHERAL, encoding="utf-8")
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    (udc / "3f980000.usb").mkdir(parents=True)

    _, changed, hat_changed, desired, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
        hat_dir=hat,
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
        hat_dir=hat,
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
            hat_dir=hat,
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
    result = main(
        [
            "--reconcile-boot",
            "--env",
            "--i2s-hat-intent-file",
            str(intent),
            "--hat-dir",
            str(hat),
        ]
    )
    payload = capsys.readouterr().out

    assert result == 74
    # The exact line `reconcile_i2s_hat_boot` substring-matches to tell a
    # non-durable publish (74, keep going) from any other failure (66).
    assert "JASPER_BOOT_CONFIG_PUBLISHED_NOT_DURABLE=true\n" in payload
    assert "dtoverlay=merus-amp" in config.read_text(encoding="utf-8")


def test_unsupported_board_never_mutates_hat_boot_setting(tmp_path: Path) -> None:
    original = "[all]\ndtparam=audio=on\n"
    model, config, intent, hat, udc = _boot_paths(
        tmp_path, model_text="Acme SBC", boot_config=original
    )
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)

    state, changed, hat_changed, _, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
        hat_dir=hat,
    )

    assert state.board_topology == "unsupported"
    assert changed is False and hat_changed is False
    assert collision is None
    assert config.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("hat_product", "intent_profile", "boot_config", "desired", "block", "collision"),
    [
        # A HAT that names itself is applied with no saved intent at all.
        (
            "StudioDAC8x",
            None,
            PERIPHERAL,
            "hifiberry_dac8x_studio",
            "hifiberry-studio-dac8x",
            None,
        ),
        # Detection never compounds a hand-written line (the jts3 config):
        # it refuses and reports, leaving the file's own overlay standing.
        (
            "StudioDAC8x",
            None,
            HAND_WRITTEN_BASE,
            "hifiberry_dac8x_studio",
            None,
            I2sHatCollision(
                managed_overlay="hifiberry-studio-dac8x",
                colliding_overlays=("hifiberry-dac8x",),
            ),
        ),
        # No EEPROM to read: the saved intent is the only answer left.
        (
            None,
            "innomaker_hifi_amp_pro",
            PERIPHERAL,
            "innomaker_hifi_amp_pro",
            "merus-amp",
            None,
        ),
        # Neither -- the jts3 incident's pin: a hand-written line survives a
        # reconcile pass untouched, and no managed block appears.
        (None, None, HAND_WRITTEN_BASE, None, None, None),
        # An EEPROM product no profile claims is no evidence, not a claim.
        ("MysteryDAC8x", None, HAND_WRITTEN_BASE, None, None, None),
        # Detection outranks a saved intent naming different hardware.
        (
            "StudioDAC8x",
            "innomaker_hifi_amp_pro",
            PERIPHERAL,
            "hifiberry_dac8x_studio",
            "hifiberry-studio-dac8x",
            None,
        ),
    ],
)
def test_i2s_hat_desired_profile_resolution_order(
    tmp_path: Path,
    hat_product: str | None,
    intent_profile: str | None,
    boot_config: str,
    desired: str | None,
    block: str | None,
    collision: I2sHatCollision | None,
) -> None:
    """EEPROM first, then the saved intent, then nothing at all (ADR-0234)."""

    model, config, intent, hat, udc = _boot_paths(tmp_path, boot_config=boot_config)
    if hat_product is not None:
        write_hat_eeprom(hat, product=hat_product)
    if intent_profile is not None:
        write_i2s_hat_intent(intent_profile, intent)
    hand_written = config.read_text(encoding="utf-8").count("dtoverlay=hifiberry-dac8x")

    _, _, hat_changed, resolved, _, reported = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
        hat_dir=hat,
    )

    assert resolved == desired
    assert hat_changed is (block is not None)
    assert reported == collision
    rendered = config.read_text(encoding="utf-8")
    assert rendered.count("dtoverlay=hifiberry-dac8x") == hand_written
    if block is None:
        assert I2S_HAT_BLOCK_BEGIN not in rendered
    else:
        assert I2S_HAT_BLOCK_BEGIN in rendered
        assert f"dtoverlay={block}\n" in rendered


def test_absent_intent_file_leaves_an_existing_managed_block_alone(
    tmp_path: Path,
) -> None:
    """An absent intent file (never saved) differs from an explicit "none".

    The former must not touch a managed block that already exists in
    config.txt -- only a present-and-empty intent file
    (write_i2s_hat_intent(None)) removes it (#i2s-hat-intent).
    """
    model, config, intent, hat, udc = _boot_paths(tmp_path)
    config.write_text(PERIPHERAL, encoding="utf-8")
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    (udc / "3f980000.usb").mkdir(parents=True)
    reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
        hat_dir=hat,
    )
    with_managed_block = config.read_text(encoding="utf-8")
    assert I2S_HAT_BLOCK_BEGIN in with_managed_block
    intent.unlink()

    _, changed, hat_changed, desired, _, collision = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        i2s_hat_intent_path=intent,
        hat_dir=hat,
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
        hat_dir=hat,
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
        hat_dir=tmp_path / "hat",
    )
    first = config.read_text(encoding="utf-8")
    _, changed_again, _, _, _, _ = reconcile_boot_config(
        model_path=model,
        boot_config_path=config,
        udc_class_dir=udc,
        hat_dir=tmp_path / "hat",
    )

    assert changed is True
    assert state.desired_role == "host"
    assert first.count("dtoverlay=dwc2,dr_mode=host") == 2
    assert "[cm5]\ndtoverlay=dwc2,dr_mode=host" in first
    assert changed_again is False
    assert config.read_text(encoding="utf-8") == first


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
            hat_dir=tmp_path / "hat",
        )
    assert config.read_text(encoding="utf-8") == original

