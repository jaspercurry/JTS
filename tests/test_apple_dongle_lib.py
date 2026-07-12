# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared Apple dongle card-resolution and deploy contract."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy/lib/jasper-apple-dongle.sh"
SCRIPTS = (
    ROOT / "deploy/bin/jasper-dac-init",
    ROOT / "deploy/bin/jasper-headphone-monitor",
)


def _resolve(tmp_path: Path, configured: str, aplay_output: str) -> list[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    aplay = fake_bin / "aplay"
    aplay.write_text("#!/usr/bin/env bash\nprintf '%s' \"$FAKE_APLAY_OUTPUT\"\n")
    aplay.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["FAKE_APLAY_OUTPUT"] = aplay_output
    result = subprocess.run(
        [
            "bash",
            "-c",
            'CONFIGURED_CARD="$1"; source "$2"; resolve_cards',
            "_",
            configured,
            str(LIB),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=True,
    )
    return result.stdout.splitlines()


def test_configured_card_bypasses_detection(tmp_path: Path) -> None:
    assert _resolve(tmp_path, "Dongle_1", "") == ["Dongle_1"]


def test_auto_detects_every_apple_dongle_card(tmp_path: Path) -> None:
    listing = (
        "hw:CARD=AppleA,DEV=0\n    USB-C to 3.5mm Headphone Jack\n"
        "hw:CARD=Other,DEV=0\n    Other DAC\n"
        "hw:CARD=AppleB,DEV=0\n    USB-C TO 3.5MM adapter\n"
    )
    assert _resolve(tmp_path, "auto", listing) == ["AppleA", "AppleB"]


def test_both_consumers_source_the_shared_owner() -> None:
    for script in SCRIPTS:
        source = script.read_text()
        assert "jasper-apple-dongle.sh" in source
        assert 'source "$APPLE_DONGLE_LIB"' in source
        assert "detect_apple_cards()" not in source
        assert "resolve_cards()" not in source


def test_missing_library_fails_loudly_without_running_forever(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["JASPER_APPLE_DONGLE_LIB"] = str(tmp_path / "missing.sh")
    for script in SCRIPTS:
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 1
        assert "reason=common_lib_unavailable" in result.stderr


def test_full_and_streambox_installers_stage_the_library() -> None:
    installer = (ROOT / "deploy/lib/install/systemd-units.sh").read_text()
    support_start = installer.index("install_jasper_support_files() {")
    support_end = installer.index("\n}\n", support_start)
    support = installer[support_start:support_end]
    assert "deploy/lib/jasper-apple-dongle.sh" in support
    assert "/usr/local/lib/jasper/jasper-apple-dongle.sh" in support

    stream_start = installer.index("install_streambox_systemd_units() {")
    stream_end = installer.index("\n}\n", stream_start)
    stream = installer[stream_start:stream_end]
    support_pos = stream.index("install_jasper_support_files")
    assert support_pos < stream.index("install_audio_output_recovery_unit_files")
    assert support_pos < stream.index("start_streambox_runtime_units")

    full_start = installer.index("install_systemd_units() {")
    full = installer[full_start:]
    support_pos = full.index("install_jasper_support_files")
    assert support_pos < full.index(
        "/usr/local/sbin/jasper-audio-hardware-reconcile --reason install"
    )
