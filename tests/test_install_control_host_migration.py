"""Test the install.sh `migrate_control_host_bind_seed` shell helper.

The helper prunes the seeded-default `JASPER_CONTROL_HOST=0.0.0.0` line
from /etc/jasper/jasper.env. That var is the control server's *bind*
address (0.0.0.0 is already the server code default), but
jasper.control.client used to misread it as a *connect* host and send
`Host: 0.0.0.0:8780` — rejected by the management-host guard, the
2026-06-11 /system/ dashboard 403. The migration must remove exactly
the harmful seeded value and leave deliberate operator overrides alone.

Exercised by sourcing the installer lib
(deploy/lib/install/env-migrations.sh) under bash with ENV_DIR pointed
at tmp_path, mirroring tests/test_install_wifi_guardian_migration.py.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_MIGRATIONS_LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"


def _run_migration(env_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail; "
            f'ENV_DIR="$1"; STATE_DIR="$1"; source "{ENV_MIGRATIONS_LIB}"; '
            "migrate_control_host_bind_seed",
            "bash",
            str(env_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_prunes_seeded_bind_default(tmp_path):
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        "JASPER_HOSTNAME=jts3.local\n"
        "JASPER_CONTROL_HOST=0.0.0.0\n"
        "JASPER_CONTROL_PORT=8780\n"
    )
    proc = _run_migration(tmp_path)
    assert proc.returncode == 0, proc.stderr
    content = env_file.read_text()
    assert "JASPER_CONTROL_HOST" not in content
    # Neighbouring lines survive untouched.
    assert "JASPER_HOSTNAME=jts3.local\n" in content
    assert "JASPER_CONTROL_PORT=8780\n" in content
    assert "removed seeded JASPER_CONTROL_HOST" in proc.stdout


def test_preserves_operator_bind_override(tmp_path):
    env_file = tmp_path / "jasper.env"
    original = "JASPER_CONTROL_HOST=127.0.0.1\nJASPER_CONTROL_PORT=8780\n"
    env_file.write_text(original)
    proc = _run_migration(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert env_file.read_text() == original


def test_noop_when_line_absent(tmp_path):
    env_file = tmp_path / "jasper.env"
    original = "JASPER_HOSTNAME=jts.local\n"
    env_file.write_text(original)
    proc = _run_migration(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert env_file.read_text() == original
    assert "migrate_control_host_bind_seed" not in proc.stdout


def test_noop_when_env_file_missing(tmp_path):
    proc = _run_migration(tmp_path)  # no jasper.env created
    assert proc.returncode == 0, proc.stderr


def test_tolerates_trailing_cr(tmp_path):
    # A hand-edited file saved with CRLF must still match the seeded value.
    env_file = tmp_path / "jasper.env"
    env_file.write_text("JASPER_CONTROL_HOST=0.0.0.0\r\n")
    proc = _run_migration(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "JASPER_CONTROL_HOST" not in env_file.read_text()
