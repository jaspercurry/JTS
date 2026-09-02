# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared process doubles for shell reconciler tests."""

from __future__ import annotations

from pathlib import Path


def fake_systemctl(
    tmp_path: Path, *, name: str = "systemctl", witness: str | None = None
) -> tuple[Path, Path]:
    """Create a successful ``systemctl`` stand-in that records its argv.

    ``witness`` names an environment variable holding a path. When given, every
    logged line is prefixed ``present=1``/``present=0`` for whether that path
    existed at call time, which lets a test read the ORDER of a file write
    against the unit commands rather than only the end state.
    """
    log = tmp_path / f"{name}.log"
    executable = tmp_path / name
    probe = (
        ""
        if witness is None
        else f'if [[ -e "${witness}" ]]; then p=1; else p=0; fi\n'
    )
    record = (
        "printf '%s\\n' \"$*\""
        if witness is None
        else "printf 'present=%s %s\\n' \"$p\" \"$*\""
    )
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"{probe}"
        f'{record} >> "$JASPER_SYSTEMCTL_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def systemctl_log(tmp_path: Path) -> str:
    """Read the fake systemctl transcript, or an empty transcript."""
    log = tmp_path / "systemctl.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""
