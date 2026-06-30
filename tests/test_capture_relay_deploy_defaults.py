# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guards for phone-mic relay deploy defaults."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_fresh_public_boxes_keep_capture_relay_disabled() -> None:
    values = _env_example_values()
    assert values["JASPER_CAPTURE_RELAY_BASE"] == ""
    assert values["JASPER_CAPTURE_ORIGIN"] == ""
    assert values["JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN"] == ""


def test_existing_box_install_migration_does_not_bake_private_relay() -> None:
    install = (ROOT / "deploy/lib/install/python-runtime.sh").read_text(encoding="utf-8")
    assert "grep -qE '^JASPER_CAPTURE_RELAY_BASE='" in install
    assert "JASPER_CAPTURE_RELAY_BASE=https://relay.jasper.tech" not in install
    assert "set_jasper_env_value JASPER_CAPTURE_RELAY_BASE" in install
    assert "grep -qE '^JASPER_CAPTURE_ORIGIN='" in install
    assert "JASPER_CAPTURE_ORIGIN=capture.jasper.tech" not in install
    assert "set_jasper_env_value JASPER_CAPTURE_ORIGIN" in install
    assert "grep -qE '^JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN='" in install
    assert "set_jasper_env_value" in install


def test_deploy_forwards_private_capture_relay_configuration() -> None:
    deploy = (ROOT / "scripts/deploy-to-pi.sh").read_text(encoding="utf-8")
    assert "JASPER_CAPTURE_RELAY_BASE" in deploy
    assert "JASPER_CAPTURE_ORIGIN" in deploy
    assert "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN" in deploy
