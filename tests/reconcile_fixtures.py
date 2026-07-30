# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared process doubles for shell reconciler tests."""

from __future__ import annotations

from pathlib import Path


def fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    """Create a successful ``systemctl`` stand-in that records its argv."""
    log = tmp_path / "systemctl.log"
    executable = tmp_path / "systemctl"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def systemctl_log(tmp_path: Path) -> str:
    """Read the fake systemctl transcript, or an empty transcript."""
    log = tmp_path / "systemctl.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""
