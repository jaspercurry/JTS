# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test the deploy/bin/jasper-identity-reconcile shell script.

The reconciler is the single writer of /var/lib/jasper/identity.env —
the snapshot of OS hostname vs Avahi's effective (post-collision-
rename) name vs the configured JASPER_HOSTNAME that the management
allowlist, /state, and doctor consume. Exercised under bash with fake
`hostname` and `busctl` executables on PATH and the file paths pointed
at tmp_path, mirroring tests/test_wifi_guardian_script.py.

Two contracts beyond the field values:

  * Journal discipline — the script runs every 5 minutes forever, so
    steady-state ticks must be SILENT (events record transitions; the
    persistent state lives in the file, /state, and the doctor).
    `--reason manual` always logs: an operator invoking it by hand is
    asking for the answer.
  * Write hygiene — atomic fixed-name tmp + rename, so a run killed
    mid-write can never accumulate unique-name litter.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from tests.install_surface import installer_text

from jasper.env_load import parse_env_file


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-identity-reconcile"


def _write_fake(bin_dir: Path, name: str, body: str) -> None:
    fake = bin_dir / name
    fake.write_text(f"#!/bin/bash\n{body}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def _set_busctl(bin_dir: Path, busctl_reply: str | None) -> None:
    """(Re)write the busctl fake. `None` simulates avahi being
    unavailable (fake exits 1). Callable between invocations so tests
    can simulate an Avahi collision rename landing mid-life."""
    if busctl_reply is None:
        _write_fake(bin_dir, "busctl", "exit 1")
    else:
        _write_fake(bin_dir, "busctl", f"echo '{busctl_reply}'")


def _setup(tmp_path: Path, *, os_hostname: str, busctl_reply: str | None,
           jasper_env: str | None) -> dict[str, str]:
    """Create the fakes + env once; reusable across invocations.
    `jasper_env=None` skips creating jasper.env."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_fake(bin_dir, "hostname", f'echo "{os_hostname}"')
    _set_busctl(bin_dir, busctl_reply)
    env_file = tmp_path / "jasper.env"
    if jasper_env is not None:
        env_file.write_text(jasper_env)
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "JASPER_IDENTITY_FILE": str(tmp_path / "identity.env"),
        "JASPER_ENV_FILE": str(env_file),
        # Use the fakes regardless of what's installed on this machine.
        "JASPER_BUSCTL": "busctl",
        "JASPER_HOSTNAME_BIN": "hostname",
    })
    return env


def _invoke(env: dict[str, str], *,
            reason: str = "test") -> tuple[subprocess.CompletedProcess, dict]:
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--reason", reason],
        capture_output=True, text=True, timeout=30, env=env,
    )
    return proc, parse_env_file(env["JASPER_IDENTITY_FILE"])


def _run(tmp_path: Path, *, os_hostname: str, busctl_reply: str | None,
         jasper_env: str | None) -> tuple[subprocess.CompletedProcess, dict]:
    """One-shot setup + invoke, for tests that only need a single run."""
    return _invoke(_setup(
        tmp_path, os_hostname=os_hostname, busctl_reply=busctl_reply,
        jasper_env=jasper_env,
    ))


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
    # First run = no previous snapshot = a transition; it logs.
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


def test_quoted_jasper_hostname_matches_python_parser(tmp_path):
    """A hand-edited `JASPER_HOSTNAME="x.local"` must parse to the same
    hostname jasper.env_load.parse_env_text sees — quotes kept on the
    bash side would flag drift forever and pollute the allowlist."""
    proc, identity = _run(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env='JASPER_HOSTNAME="jts3.local"\n',
    )
    assert proc.returncode == 0, proc.stderr
    assert identity["JASPER_IDENTITY_CONFIGURED_HOSTNAME"] == "jts3.local"
    assert identity["JASPER_IDENTITY_DRIFT"] == "0"


# ----------------------------------------------------------------------
# Journal discipline — steady ticks are silent; transitions and manual
# runs log.
# ----------------------------------------------------------------------


def test_steady_rerun_is_journal_silent(tmp_path):
    """The 5-minute timer must not write heartbeat lines: an unchanged
    identity logs NOTHING (the file is still rewritten with a fresh
    CHECKED_AT for the doctor's staleness probe)."""
    env = _setup(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    first, _ = _invoke(env)
    assert "event=identity_reconcile.steady" in first.stderr
    second, identity = _invoke(env)
    assert second.returncode == 0, second.stderr
    assert second.stderr.strip() == ""
    assert identity["JASPER_IDENTITY_CHECKED_AT"]


def test_manual_reason_always_logs(tmp_path):
    """An operator running the script by hand is asking for the answer
    — `--reason manual` logs even when nothing changed."""
    env = _setup(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    _invoke(env)
    manual, _ = _invoke(env, reason="manual")
    assert "event=identity_reconcile.steady" in manual.stderr


def test_transition_to_collision_logs_once(tmp_path):
    """A collision rename landing mid-life is a transition: the tick
    that observes it logs, the next unchanged tick is silent again."""
    bin_dir = tmp_path / "bin"
    env = _setup(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    _invoke(env)
    _set_busctl(bin_dir, 's "jts3-2.local"')
    transition, identity = _invoke(env)
    assert "event=identity_reconcile.collision" in transition.stderr
    assert identity["JASPER_IDENTITY_COLLISION"] == "1"
    settled, _ = _invoke(env)
    assert settled.stderr.strip() == ""


def test_avahi_outage_logs_on_transition_only(tmp_path):
    """A dev box (or degraded Pi) with no avahi must not write
    avahi_unavailable to the journal every 5 minutes — only when the
    availability flips."""
    bin_dir = tmp_path / "bin"
    env = _setup(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    _invoke(env)
    _set_busctl(bin_dir, None)
    outage, identity = _invoke(env)
    assert "event=identity_reconcile.avahi_unavailable" in outage.stderr
    assert identity["JASPER_IDENTITY_AVAHI_AVAILABLE"] == "0"
    still_out, _ = _invoke(env)
    assert still_out.stderr.strip() == ""


def test_no_tmp_litter_across_runs(tmp_path):
    """Atomic write uses a fixed self-overwriting `.tmp` name — repeated
    runs must leave exactly one identity file behind, never a trail of
    unique temp names."""
    env = _setup(
        tmp_path,
        os_hostname="jts3",
        busctl_reply='s "jts3.local"',
        jasper_env="JASPER_HOSTNAME=jts3.local\n",
    )
    _invoke(env)
    _invoke(env)
    leftovers = sorted(p.name for p in tmp_path.glob("identity.env*"))
    assert leftovers == ["identity.env"]


def test_install_enables_timer_with_now():
    """`systemctl enable` alone arms a timer for the NEXT boot but
    leaves it inactive until then — the enable-vs-start trap that
    shipped 2026-06-11 (timer dead until reboot; caught by hardware
    validation, with the doctor's snapshot-staleness warn as backstop).
    The installer must use `enable --now` so the 5-min re-check loop is
    live from the first deploy."""
    install_sh = installer_text()
    assert "systemctl enable --now jasper-identity-reconcile.timer" in install_sh
