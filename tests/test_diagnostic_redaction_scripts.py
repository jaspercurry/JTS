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


def test_diagnostic_scripts_parse_as_bash():
    for script in (
        ROOT / "scripts" / "_diagnostic_redaction.sh",
        ROOT / "scripts" / "fetch-pi-logs.sh",
        ROOT / "scripts" / "pi-bundle.sh",
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)
