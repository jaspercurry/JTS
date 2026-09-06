# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pi_bundle_redacts_unit_files_before_packaging():
    text = (ROOT / "scripts" / "pi-bundle.sh").read_text()
    assert "redact_jasper_diagnostics < \"$src\" > \"$DIR/${unit}\"" in text
    assert 'cp /etc/systemd/system/jasper-voice.service "$DIR/"' not in text


def test_fetch_logs_does_not_capture_all_sudo_commands():
    text = (ROOT / "scripts" / "fetch-pi-logs.sh").read_text()
    # Previous-boot forensics should capture only safe operator command
    # breadcrumbs. Broad sudo/COMMAND collection can leak passwords,
    # bearer tokens, or other arbitrary command arguments into ./logs/.
    assert "power|sudo|COMMAND=" not in text
    assert "sudo\\[[0-9]+\\]:.*COMMAND=" in text
    assert "--unit=jts-diagnostic-" in text
    assert "<diagnostic-command-redacted>" in text
    assert "/home/pi/jts/scripts/" not in text


def test_fetch_logs_writes_noise_summary_artifact():
    text = (ROOT / "scripts" / "fetch-pi-logs.sh").read_text()
    assert "write_log_noise_summary()" in text
    assert "log-noise-summary-${TS}.txt" in text
    assert "top repeated message fingerprints" in text
    assert "log-noise-summary-latest.txt" in text


def test_fetch_logs_includes_audio_transport_and_reconcilers():
    text = (ROOT / "scripts" / "fetch-pi-logs.sh").read_text()
    for unit in (
        "jasper-outputd",
        "jasper-fanin",
        "jasper-aec-reconcile",
        "jasper-audio-hardware-reconcile",
    ):
        assert unit in text


def test_fetch_logs_captures_monotonic_boot_timing_context():
    text = (ROOT / "scripts" / "fetch-pi-logs.sh").read_text()
    assert "previous-boot-timeline" in text
    assert "current-boot-timeline" in text
    assert "--output=short-monotonic" in text
    assert "/proc/uptime" in text
    assert "btime_epoch" in text
    assert "timedatectl status" in text


def test_diagnostic_scripts_parse_as_bash():
    for script in (
        ROOT / "scripts" / "_diagnostic_redaction.sh",
        ROOT / "scripts" / "fetch-pi-logs.sh",
        ROOT / "scripts" / "pi-bundle.sh",
        ROOT / "scripts" / "pi-run-diagnostic.sh",
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)
