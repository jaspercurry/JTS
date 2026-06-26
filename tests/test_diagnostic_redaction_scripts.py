# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_redactor(sample: str) -> str:
    script = ROOT / "scripts" / "_diagnostic_redaction.sh"
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; . {script}; redact_jasper_diagnostics",
        ],
        input=sample,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_redactor_covers_current_secret_env_families():
    sample = "\n".join([
        "GEMINI_API_KEY=google-secret",
        "OPENAI_API_KEY=openai-secret",
        "XAI_API_KEY=xai-secret",
        "SPOTIFY_CLIENT_SECRET=spotify-secret",
        "GOOGLE_CLIENT_SECRET=google-oauth-secret",
        "JASPER_HA_TOKEN=ha-secret",
        "JASPER_MTA_BUSTIME_KEY=mta-secret",
        "JASPER_WIFI_PSK=wifi-secret",
        "MUSIC_APP_TOKEN=music-app-secret",
        "MUSIC_USER_TOKEN=music-user-secret",
        "JASPER_WIFI_KEY_MGMT=wpa-psk",
        "",
    ])

    out = _run_redactor(sample)

    for secret in (
        "google-secret",
        "openai-secret",
        "xai-secret",
        "spotify-secret",
        "google-oauth-secret",
        "ha-secret",
        "mta-secret",
        "wifi-secret",
        "music-app-secret",
        "music-user-secret",
    ):
        assert secret not in out
    assert "JASPER_WIFI_KEY_MGMT=wpa-psk" in out
    assert out.count("<redacted>") == 10


def test_redactor_scrubs_inline_env_assignments_in_logs():
    out = _run_redactor(
        "daemon started OPENAI_API_KEY=sk-live "
        "SPOTIFY_CLIENT_SECRET='two words' JASPER_WIFI_PSK=\"wifi words\" ok\n",
    )
    assert "sk-live" not in out
    assert "two words" not in out
    assert "wifi words" not in out
    assert "OPENAI_API_KEY=<redacted>" in out
    assert "SPOTIFY_CLIENT_SECRET=<redacted>" in out
    assert "JASPER_WIFI_PSK=<redacted>" in out


def test_redactor_scrubs_systemd_environment_assignments():
    out = _run_redactor(
        "Environment=OPENAI_API_KEY=sk-live\n"
        "Environment=\"JASPER_WIFI_PSK=two words\"\n"
        "Environment='SPOTIFY_CLIENT_SECRET=quoted secret'\n",
    )
    assert "sk-live" not in out
    assert "two words" not in out
    assert "quoted secret" not in out
    assert "Environment=OPENAI_API_KEY=<redacted>" in out
    assert 'Environment="JASPER_WIFI_PSK=<redacted>"' in out
    assert "Environment='SPOTIFY_CLIENT_SECRET=<redacted>'" in out


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
