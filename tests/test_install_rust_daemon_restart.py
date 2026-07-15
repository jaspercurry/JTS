# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deploy coverage for the remaining Rust audio daemons and USB retirement."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUST_HELPERS = ROOT / "deploy/lib/install/rust-daemons.sh"
INSTALL = ROOT / "deploy/install.sh"
SYSTEMD_HELPERS = ROOT / "deploy/lib/install/systemd-units.sh"
UNIT_DIR = ROOT / "deploy/systemd"


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\(\) \{{\n(.*?)\n\}}", text, re.DOTALL)
    assert match is not None, f"missing {name}()"
    return match.group(1)


def _built_daemons() -> set[str]:
    return set(
        re.findall(
            r'^\s*build_install_rust_daemon "([a-z0-9-]+)" "',
            RUST_HELPERS.read_text(),
            re.MULTILINE,
        )
    )


def test_only_live_rust_audio_daemons_are_built():
    assert _built_daemons() == {"jasper-fanin", "jasper-outputd"}
    assert "build_install_jasper_usbsink_audio" not in RUST_HELPERS.read_text()
    assert not (ROOT / "rust/jasper-usbsink-audio/Cargo.toml").exists()


def test_each_built_daemon_has_one_systemd_owner():
    for daemon in _built_daemons():
        needle = f"ExecStart=/opt/jasper/bin/{daemon}"
        owners = [path.name for path in UNIT_DIR.glob("*.service") if needle in path.read_text()]
        assert len(owners) == 1, (daemon, owners)


def test_core_graph_restart_makes_built_daemons_live():
    text = SYSTEMD_HELPERS.read_text()
    assert "systemctl restart jasper-fanin.service" in text
    install_text = INSTALL.read_text()
    outputd_ready = _function_body(install_text, "require_outputd_ready")
    assert "systemctl restart jasper-outputd.service" in outputd_ready
    assert "restart_services_for_changed_rust_daemons" not in text
    assert "restart_services_for_changed_rust_daemons" not in RUST_HELPERS.read_text()


def test_installer_retires_obsolete_usb_binary_and_cache_on_both_profiles():
    helpers = RUST_HELPERS.read_text()
    retire = _function_body(helpers, "retire_jasper_usbsink_audio")
    assert "rm -f -- /opt/jasper/bin/jasper-usbsink-audio" in retire
    assert "rm -rf -- /var/cache/jasper-usbsink-audio-build" in retire

    main = _function_body(INSTALL.read_text(), "main")
    assert main.count("retire_jasper_usbsink_audio") == 2
    assert "build_install_jasper_usbsink_audio" not in main
