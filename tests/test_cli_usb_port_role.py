# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from jasper.audio_hardware.i2s_hat import I2S_HAT_BLOCK_BEGIN, write_i2s_hat_intent
from jasper.cli.usb_port_role import main

from ._boot_paths import HAND_WRITTEN_BASE, PERIPHERAL, PI5, boot_paths
from ._hat_eeprom import write_hat_eeprom
from ._log_events import stderr_event, stderr_events

# The `--env` contract (ADR-0235 R2) -- the CLI's whole record, which
# `deploy/bin/jasper-audio-hardware-reconcile` evals. A rename that lands on
# only one side has to fail here rather than read as an empty string in the
# shell.
_ENV_CONTRACT_KEYS = {
    "JASPER_BOOT_BOARD_TOPOLOGY",
    "JASPER_BOOT_USB_DESIRED_ROLE",
    "JASPER_BOOT_USB_ACTIVE_ROLE",
    "JASPER_BOOT_REBOOT_REQUIRED",
    "JASPER_BOOT_CONFIG_CHANGED",
    "JASPER_BOOT_I2S_HAT_PROFILE",
    "JASPER_BOOT_I2S_HAT_CHANGED",
    "JASPER_BOOT_CONFIG_PUBLISHED_NOT_DURABLE",
    "JASPER_BOOT_I2S_HAT_COLLISION_MANAGED_OVERLAY",
    "JASPER_BOOT_I2S_HAT_COLLISION_COLLIDING_OVERLAYS",
}


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


@pytest.mark.parametrize(
    ("hat_product", "boot_config", "expected"),
    [
        pytest.param(
            None,
            PERIPHERAL,
            {
                "JASPER_BOOT_I2S_HAT_PROFILE": "",
                "JASPER_BOOT_I2S_HAT_CHANGED": "false",
                "JASPER_BOOT_I2S_HAT_COLLISION_MANAGED_OVERLAY": "",
                "JASPER_BOOT_I2S_HAT_COLLISION_COLLIDING_OVERLAYS": "",
            },
            id="no_hat",
        ),
        pytest.param(
            "StudioDAC8x",
            PERIPHERAL,
            {
                "JASPER_BOOT_I2S_HAT_PROFILE": "hifiberry_dac8x_studio",
                "JASPER_BOOT_I2S_HAT_CHANGED": "true",
                "JASPER_BOOT_I2S_HAT_COLLISION_MANAGED_OVERLAY": "",
                "JASPER_BOOT_I2S_HAT_COLLISION_COLLIDING_OVERLAYS": "",
            },
            id="detected_hat_applied",
        ),
        pytest.param(
            "StudioDAC8x",
            HAND_WRITTEN_BASE,
            {
                "JASPER_BOOT_I2S_HAT_PROFILE": "hifiberry_dac8x_studio",
                "JASPER_BOOT_I2S_HAT_CHANGED": "false",
                "JASPER_BOOT_I2S_HAT_COLLISION_MANAGED_OVERLAY": (
                    "hifiberry-studio-dac8x"
                ),
                "JASPER_BOOT_I2S_HAT_COLLISION_COLLIDING_OVERLAYS": (
                    "hifiberry-dac8x"
                ),
            },
            id="detected_hat_collision",
        ),
    ],
)
def test_env_emitter_hands_bash_the_whole_contract_and_nothing_it_must_parse(
    hat_product: str | None,
    boot_config: str,
    expected: dict[str, str],
    tmp_path: Path,
    capsys,
) -> None:
    """What Python quotes, bash evals -- the full key set, every scenario.

    A missing key reads as a `bash -u` unbound-variable failure rather than
    an empty string, so completeness is asserted through a real eval
    (ADR-0235 R2) rather than by re-reading the quoting rule.
    """
    model, config, intent, hat, udc = boot_paths(tmp_path, boot_config=boot_config)
    if hat_product is not None:
        write_hat_eeprom(hat, product=hat_product)
    (udc / "3f980000.usb").mkdir(parents=True)

    assert main(
        [
            "--reconcile-boot",
            "--env",
            "--model-file",
            str(model),
            "--boot-config",
            str(config),
            "--udc-class-dir",
            str(udc),
            "--i2s-hat-intent-file",
            str(intent),
            "--hat-dir",
            str(hat),
        ]
    ) == 0
    payload = capsys.readouterr().out

    emitted: dict[str, str] = {}
    for line in payload.splitlines():
        key, _, quoted = line.partition("=")
        parts = shlex.split(quoted)
        emitted[key] = parts[0] if parts else ""
    assert set(emitted) == _ENV_CONTRACT_KEYS
    for key, value in expected.items():
        assert emitted[key] == value, key

    keys = sorted(_ENV_CONTRACT_KEYS)
    reader = "; ".join(f'printf "%s\\n" "${{{key}}}"' for key in keys)
    seen = subprocess.run(
        ["bash", "-uc", f'eval "$1"; {reader}', "bash", payload],
        check=False,
        capture_output=True,
        text=True,
    )
    assert seen.returncode == 0, seen.stderr
    assert seen.stdout.splitlines() == [emitted[key] for key in keys]


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
