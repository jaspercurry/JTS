# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

from jasper.cli.doctor.secret_compartments import COMPARTMENTS


ROOT = Path(__file__).resolve().parents[1]
_REDACTION_LIB = ROOT / "scripts" / "_diagnostic_redaction.sh"


@functools.lru_cache(maxsize=None)
def _secret_env_files() -> tuple[str, ...]:
    """The shared file list, read out of the bash lib itself.

    Tuple, not list: lru_cache hands every caller the same object.
    """
    proc = subprocess.run(
        ["bash", "-c", f'. "{_REDACTION_LIB}"; printf "%s\\n" "${{JASPER_SECRET_ENV_FILES[@]}}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(proc.stdout.splitlines())


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
        # tail-pi-logs.sh is not pinned here: it's one of
        # test_pi_script_shared_defaults.py's SCRIPT_NAMES, already covered
        # by that file's test_pi_target_scripts_are_valid_bash.
    ):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_secret_file_array_covers_every_compartment_env_file():
    """fetch-pi-logs.sh and pi-bundle.sh must not silently drift behind the
    doctor's own secret-compartment inventory."""
    array = set(_secret_env_files())
    # Only the KEY=value (`.env`) compartment files share the array's own
    # shape — the JSON/token-cache files in the same compartments have no
    # such shape and are excluded the same way the array itself is.
    compartment_env_files = {
        f
        for compartment in COMPARTMENTS
        for f, _ in compartment.resolved_files()
        if f.endswith(".env")
    }
    missing = compartment_env_files - array
    assert not missing, f"compartment KEY=value files missing from the array: {missing}"


def test_secret_file_array_has_no_raw_value_file():
    """Every entry must be a KEY=value file: the redactor keys on names, so
    a raw-value file (no `=`-delimited shape) would leak its whole content
    unscrubbed."""
    array = _secret_env_files()
    for entry in array:
        assert entry.endswith(".env"), f"not a KEY=value file: {entry}"


def test_tail_pi_logs_redacts_the_live_stream(tmp_path):
    """The one streaming path with no on-disk copy must still redact — on
    both stdio streams, keeping a trailing unterminated line. A fake `ssh`
    on PATH stands in for the remote journalctl -f."""
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\n"
        "printf 'voice[1]: rejected OPENAI_API_KEY=sk-live1234567890 invalid\\n'\n"
        "printf 'mux[2]: rejected GEMINI_API_KEY=sk-shadow0987654321 invalid\\n' 1>&2\n"
        "printf 'END-OF-STREAM-MARKER-NO-NEWLINE'\n"
    )
    fake_ssh.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "tail-pi-logs.sh")],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PI_HOST": "test-pi.invalid",
            "PI_USER": "test-user",
            "HOME": os.environ.get("HOME", ""),
        },
    )

    combined = proc.stdout + proc.stderr
    assert "sk-live1234567890" not in combined
    assert "sk-shadow0987654321" not in combined
    assert "OPENAI_API_KEY=<redacted>" in combined
    assert "GEMINI_API_KEY=<redacted>" in combined
    assert "END-OF-STREAM-MARKER-NO-NEWLINE" in proc.stdout
