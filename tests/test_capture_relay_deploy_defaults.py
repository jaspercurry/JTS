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


def test_fresh_public_boxes_use_jasper_public_capture_relay() -> None:
    values = _env_example_values()
    assert values["JASPER_CAPTURE_RELAY_BASE"] == "https://relay.jasper.tech"
    assert values["JASPER_CAPTURE_ORIGIN"] == "capture.jasper.tech"
    assert values["JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN"] == ""


def test_existing_box_install_migration_seeds_public_relay_when_missing() -> None:
    install = (ROOT / "deploy/lib/install/python-runtime.sh").read_text(encoding="utf-8")
    assert "grep -qE '^JASPER_CAPTURE_RELAY_BASE='" in install
    assert "JASPER_CAPTURE_RELAY_BASE=https://relay.jasper.tech" in install
    assert "set_jasper_env_value JASPER_CAPTURE_RELAY_BASE" in install
    assert "grep -qE '^JASPER_CAPTURE_ORIGIN='" in install
    assert "JASPER_CAPTURE_ORIGIN=capture.jasper.tech" in install
    assert "set_jasper_env_value JASPER_CAPTURE_ORIGIN" in install
    assert "grep -qE '^JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN='" in install
    assert "set_jasper_env_value" in install


def test_relay_template_explains_https_mic_requirement_and_self_hosting() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    install = (ROOT / "deploy/lib/install/python-runtime.sh").read_text(
        encoding="utf-8"
    )
    for text in (env_example, install):
        assert "publicly trusted HTTPS" in text
        assert "getUserMedia" in text
    assert "relay/README.md" in env_example
    assert "capture-page/README.md" in env_example
    assert "relay/" in install
    assert "capture-page/" in install


def test_deploy_forwards_private_capture_relay_configuration() -> None:
    deploy = (ROOT / "scripts/deploy-to-pi.sh").read_text(encoding="utf-8")
    assert "JASPER_CAPTURE_RELAY_BASE" in deploy
    assert "JASPER_CAPTURE_ORIGIN" in deploy
    assert "JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN" in deploy


def test_deploy_excludes_cloudflare_generated_state() -> None:
    deploy = (ROOT / "scripts/deploy-to-pi.sh").read_text(encoding="utf-8")
    assert "--exclude '.wrangler'" in deploy
    assert "--exclude 'dist'" in deploy
