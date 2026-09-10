# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.audio_hardware.i2s_hat import I2S_HAT_BLOCK_BEGIN, write_i2s_hat_intent
from jasper.cli.usb_port_role import main

from ._boot_paths import PERIPHERAL, PI5, boot_paths
from ._log_events import stderr_event, stderr_events


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
            "--hat-dir",
            str(tmp_path / "hat"),
        ]
    ) == 0

    captured = capsys.readouterr()
    assert stderr_event(captured.err, "hardware.boot_config_changed") == {
        "reboot_required": "0",
    }
    assert "event=" not in captured.out


def test_i2s_hat_self_heal_after_hand_deleted_managed_block_logs_changed_event(
    tmp_path: Path, capsys
) -> None:
    """A managed block removed by hand (not via intent) is rewritten today
    (G6, ADR-0235 R3): the rewrite itself must not be silent (G4)."""

    model, config, intent, hat, udc = boot_paths(tmp_path)
    config.write_text(PERIPHERAL, encoding="utf-8")
    write_i2s_hat_intent("innomaker_hifi_amp_pro", intent)
    (udc / "3f980000.usb").mkdir(parents=True)
    cli_args = [
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

    assert main(cli_args) == 0
    assert I2S_HAT_BLOCK_BEGIN in config.read_text(encoding="utf-8")

    # An operator (or another tool) edits config.txt directly and drops the
    # managed block; the intent file still names the HAT.
    config.write_text(PERIPHERAL, encoding="utf-8")
    capsys.readouterr()

    assert main(cli_args) == 0
    captured = capsys.readouterr()
    assert stderr_event(captured.err, "hardware.i2s_hat_boot_config_changed") == {
        "profile": "innomaker_hifi_amp_pro",
    }
    assert I2S_HAT_BLOCK_BEGIN in config.read_text(encoding="utf-8")

    # A pass that changes nothing is silent: the event marks the transition,
    # not the desired state.
    assert main(cli_args) == 0
    assert (
        stderr_events(
            capsys.readouterr().err, "hardware.i2s_hat_boot_config_changed"
        )
        == []
    )


@pytest.mark.parametrize(
    ("udc_present", "available", "active", "reason", "exit_code"),
    [
        (False, "false", "host", "role_change_pending_reboot", 1),
        (True, "true", "peripheral", "available", 0),
    ],
    ids=["no_gadget", "gadget_bound"],
)
def test_management_transport_probe_reports_structured_fields(
    tmp_path: Path,
    capsys,
    udc_present: bool,
    available: str,
    active: str,
    reason: str,
    exit_code: int,
) -> None:
    model, config, _intent, hat, udc = boot_paths(tmp_path)
    if udc_present:
        (udc / "3f980000.usb").mkdir(parents=True)

    assert main(
        [
            "--require-management-transport",
            "--model-file",
            str(model),
            "--boot-config",
            str(config),
            "--udc-class-dir",
            str(udc),
            "--hat-dir",
            str(hat),
        ]
    ) == exit_code

    captured = capsys.readouterr()
    assert stderr_event(captured.err, "hardware.usb_management_transport") == {
        "available": available,
        "desired": "peripheral",
        "active": active,
        "reason": reason,
    }
