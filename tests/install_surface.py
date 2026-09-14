# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for assertions over deploy/install.sh plus sourced install libs."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from tests.systemd_unit_helpers import seconds_for

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "deploy" / "install.sh"
INSTALL_LIB_DIR = REPO / "deploy" / "lib" / "install"


def installer_shell_paths() -> list[Path]:
    """Return the root installer and every sourced deploy/lib/install lib."""
    return [INSTALL_SH, *sorted(INSTALL_LIB_DIR.glob("*.sh"))]


def installer_text() -> str:
    """Concatenate the install surface that can run during deploy."""
    return "\n".join(
        path.read_text(encoding="utf-8") for path in installer_shell_paths()
    )


def rendered_reconcile_timeout_dropins(tmp_path: Path) -> dict[str, float]:
    """Run render_reconcile_oneshot_timeout_dropins (systemd-units.sh) and
    return each reconciler's rendered TimeoutStartSec, in seconds, keyed by
    unit name (no `.service` suffix) -- see #4810 row R-163.

    INSTALL_DIR points at a directory with no venv so the function falls back
    to `python3` on PYTHONPATH=REPO, exercising this checkout's constants
    without needing a built `/opt/jasper` venv.
    """
    systemd_dir = tmp_path / "systemd"
    units_lib = INSTALL_LIB_DIR / "systemd-units.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(units_lib))} >/dev/null 2>&1 && "
            "render_reconcile_oneshot_timeout_dropins",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "REPO_DIR": str(REPO),
            "SYSTEMD_DIR": str(systemd_dir),
            "INSTALL_DIR": str(tmp_path / "no-venv-here"),
        },
    )
    assert result.returncode == 0, result.stderr
    rendered: dict[str, float] = {}
    for unit_dir in systemd_dir.glob("*.service.d"):
        conf = unit_dir / "10-timeout.conf"
        assert conf.is_file(), f"{unit_dir} is missing 10-timeout.conf"
        unit = unit_dir.name.removesuffix(".service.d")
        rendered[unit] = seconds_for(
            conf.read_text(encoding="utf-8"), "TimeoutStartSec"
        )
    return rendered


#: `getent`/`chgrp` stand-ins for the install helpers that gate on the shared
#: `jasper` group: CI has neither the group nor root, so both resolve to the
#: running user and the real `chmod` decides the modes under assertion.
JASPER_GROUP_STUBS = r"""
getent() {
    if [ "$1" = "passwd" ]; then
        printf 'jasper-web:x:%s:%s:::\n' "$(id -u)" "$(id -g)"
    else
        printf 'jasper:x:%s:\n' "$(id -g)"
    fi
}
chgrp() { :; }
"""
