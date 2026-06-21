# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — env domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import os
from pathlib import Path
from ...config import Config
from ._registry import doctor_check
from ._shared import CheckResult

@doctor_check(order=0, group="env")
def check_env_file() -> CheckResult:
    p = Path("/etc/jasper/jasper.env")
    if not p.exists():
        return CheckResult("env file", "fail", f"{p} missing — re-run install.sh")
    wizard = Path("/var/lib/jasper/voice_provider.env")
    if wizard.exists():
        return CheckResult("env file", "ok", f"{p} (+ wizard {wizard.name})")
    return CheckResult("env file", "ok", str(p))

@doctor_check(order=1, group="env")
def check_speaker_name() -> CheckResult:
    from ...speaker_name import STATE_FILE, read_state

    state = read_state()
    p = Path(STATE_FILE)
    if p.exists() and state.source != "state":
        return CheckResult(
            "speaker name",
            "warn",
            f"{p} exists but could not be parsed; using {state.name!r}",
        )
    return CheckResult(
        "speaker name",
        "ok",
        f"{state.name!r} ({state.source})",
    )

@doctor_check(order=23, group="env", label="state dir", needs_cfg=True)
def check_state_dir(cfg: Config) -> CheckResult:
    p = Path(cfg.usage_db).parent
    if not p.exists():
        return CheckResult("state dir", "warn", f"{p} missing (will be created on first run)")
    if not os.access(str(p), os.W_OK):
        return CheckResult("state dir", "fail", f"{p} not writable")
    return CheckResult("state dir", "ok", str(p))

@doctor_check(order=23.5, group="env", label="state group-write", needs_cfg=True)
def check_state_dir_group_writable(cfg: Config) -> CheckResult:
    """The shared, multi-writer state files must be group-`jasper` AND
    group-writable. jasper-voice and jasper-mux co-own StateDirectory=jasper, so
    whichever restarts last re-chowns the tree to its own user; a 0644 file the
    OTHER daemon doesn't own then can't be written ("attempt to write a readonly
    database" — the 2026-06-19 incident). UMask=0007 on the daemons + the install
    heal keep these 0660; this flags drift before it bites."""
    return _classify_state_group_write(Path(cfg.usage_db))


def _classify_state_group_write(usage_db: Path) -> CheckResult:
    """Path-parameterized core of ``check_state_dir_group_writable`` — unit
    testable with tmp files (mirrors the resilience/renderers ``_classify_*``
    doctor helpers)."""
    import grp
    import stat as _stat

    state_dir = usage_db.parent
    candidates = (
        usage_db,
        state_dir / "timers.db",
        state_dir / "wake-events" / "wake-events.sqlite3",
        state_dir / "speaker_volume.json",
    )
    bad: list[str] = []
    checked = 0
    for p in candidates:
        if not p.exists():
            continue
        checked += 1
        try:
            st = p.stat()
            grp_name = grp.getgrgid(st.st_gid).gr_name
        except (OSError, KeyError):
            bad.append(f"{p.name} (stat failed)")
            continue
        if grp_name != "jasper" or not (st.st_mode & _stat.S_IWGRP):
            bad.append(f"{p.name} ({grp_name} {oct(st.st_mode & 0o777)})")
    if not checked:
        return CheckResult(
            "state group-write", "ok",
            "no shared state files yet (created on first use)",
        )
    if bad:
        return CheckResult(
            "state group-write", "warn",
            "not group-`jasper`-writable: " + ", ".join(bad)
            + " — re-deploy to heal (daemons set UMask=0007)",
        )
    return CheckResult(
        "state group-write", "ok",
        f"{checked} shared file(s) group-`jasper`-writable",
    )
