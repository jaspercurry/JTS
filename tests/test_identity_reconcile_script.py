"""Test the deploy/bin/jasper-identity-reconcile shell script.

The reconciler is the single writer of /var/lib/jasper/identity.env —
the snapshot of OS hostname vs Avahi's effective (post-collision-
rename) name vs the configured JASPER_HOSTNAME that the management
allowlist, /state, and doctor consume. Exercised under bash with fake
`hostname` and `busctl` executables on PATH and the file paths pointed
at tmp_path, mirroring tests/test_wifi_guardian_script.py.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from jasper.env_load import parse_env_file


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-identity-reconcile"


def _write_fake(bin_dir: Path, name: str, body: str) -> None:
    fake = bin_dir / name
    fake.write_text(f"#!/bin/bash\n{body}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, *, os_hostname: str, busctl_reply: str | None,
         jasper_env: str | None) -> tuple[subprocess.CompletedProcess, dict]:
    """Run the reconciler with fakes. `busctl_reply=None` simulates a
    missing/unavailable avahi (fake exits 1). `jasper_env=None` skips
    creating jasper.env. Returns (proc, parsed identity file)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake(bin_dir, "hostname", f'echo "{os_hostname}"')
    if busctl_reply is None:
        _write_fake(bin_dir, "busctl", "exit 1")
    else:
        _write_fake(bin_dir, "busctl", f'echo \'{busctl_reply}\'')
    env_file = tmp_path / "jasper.env"
    if jasper_env is not None:
        env_file.write_text(jasper_env)
    identity_file = tmp_path / "identity.env"
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "JASPER_IDENTITY_FILE": str(identity_file),
        "JASPER_ENV_FILE": str(env_file),
        # Use the fakes regardless of what's installed on this machine.
        "JASPER_BUSCTL": "busctl",
        "JASPER_HOSTNAME_BIN": "hostname",
    })
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--reason", "test"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    return proc, parse_env_file(str(identity_file))


def test_steady_state(tmp_path):
    proc, identity = _run(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_OS_HOSTNAME"] == "jts3"
    assert identity["JASPER_IDENTITY_AVAHI_HOSTNAME"] == "jts3.local"
    assert identity["JASPER_IDENTITY_CONFIGURED_HOSTNAME"] == "jts3.local"
    assert identity["JASPER_IDENTITY_COLLISION"] == "0"
    assert identity["JASPER_IDENTITY_DRIFT"] == "0"
    assert identity["JASPER_IDENTITY_AVAHI_AVAILABLE"] == "1"
    assert identity["JASPER_IDENTITY_CHECKED_AT"]
    assert "event=identity_reconcile.steady" in proc.stderr


def test_collision_rename_detected(tmp_path):
    """The headline case: Avahi renamed us because another device owns
    the hostname. RFC 6762 conflict resolution → jts3-2.local."""
    proc, identity = _run(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3-2.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_AVAHI_HOSTNAME"] == "jts3-2.local"
    assert identity["JASPER_IDENTITY_COLLISION"] == "1"
    assert identity["JASPER_IDENTITY_DRIFT"] == "1"
    assert "event=identity_reconcile.collision" in proc.stderr


def test_drift_without_collision(tmp_path):
    """Stale JASPER_HOSTNAME after a manual hostnamectl rename: avahi
    advertises the OS hostname fine, but the configured identity still
    points at the old name."""
    proc, identity = _run(
        tmp_path,
        os_hostname="jts4",
        busctl_reply='s "jts4.local"',
        jasper_env="JASPER_HOSTNAME=jts.local\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_COLLISION"] == "0"
    assert identity["JASPER_IDENTITY_DRIFT"] == "1"
    assert "event=identity_reconcile.drift" in proc.stderr


def test_avahi_unavailable_falls_back_to_os_hostname(tmp_path):
    proc, identity = _run(
        tmp_path,
        os_hostname="jts3",
        busctl_reply=None,
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_AVAHI_HOSTNAME"] == "jts3.local"
    assert identity["JASPER_IDENTITY_AVAHI_AVAILABLE"] == "0"
    assert identity["JASPER_IDENTITY_COLLISION"] == "0"
    assert "event=identity_reconcile.avahi_unavailable" in proc.stderr


def test_missing_jasper_env_defaults_configured_to_jts_local(tmp_path):
    proc, identity = _run(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env=None,
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_CONFIGURED_HOSTNAME"] == "jts.local"
    # jts3.local advertised vs jts.local intended → drift, surfaced.
    assert identity["JASPER_IDENTITY_DRIFT"] == "1"


def test_fqdn_os_hostname_is_shortened_and_lowercased(tmp_path):
    proc, identity = _run(
        tmp_path,
        os_hostname="JTS3.fritz.box",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_OS_HOSTNAME"] == "jts3"
    assert identity["JASPER_IDENTITY_COLLISION"] == "0"


def test_last_jasper_hostname_assignment_wins(tmp_path):
    """Mirrors systemd EnvironmentFile layering: a duplicated key's
    last assignment is the effective one."""
    proc, identity = _run(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=old.local\nJASPER_HOSTNAME=jts3.local\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_CONFIGURED_HOSTNAME"] == "jts3.local"
    assert identity["JASPER_IDENTITY_DRIFT"] == "0"
