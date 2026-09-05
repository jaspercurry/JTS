# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — env domain."""
from __future__ import annotations

from pathlib import Path
from ...config import Config
from ._registry import doctor_check
from ._shared import CheckResult, _group_writable_dir

# Machine-stable codes naming which branch of an env check produced a result
# (AGENTS.md: tests pin status + reason, never detail prose).
REASON_ENV_FILE_MISSING = "env_file_missing"
REASON_SPEAKER_NAME_UNPARSEABLE = "speaker_name_unparseable"
REASON_STATE_DIR_MISSING = "state_dir_missing"
REASON_STATE_DIR_STAT_FAILED = "state_dir_stat_failed"
REASON_STATE_DIR_NOT_WRITABLE = "state_dir_not_writable"
REASON_STATE_GROUP_WRITE_VIOLATION = "state_group_write_violation"

@doctor_check
def check_env_file() -> CheckResult:
    p = Path("/etc/jasper/jasper.env")
    if not p.exists():
        return CheckResult(
            "env file", "fail", f"{p} missing — re-run install.sh",
            reason=REASON_ENV_FILE_MISSING,
        )
    wizard = Path("/var/lib/jasper/voice_provider.env")
    if wizard.exists():
        return CheckResult("env file", "ok", f"{p} (+ wizard {wizard.name})")
    return CheckResult("env file", "ok", str(p))

@doctor_check
def check_speaker_name() -> CheckResult:
    from ...speaker_name import STATE_FILE, read_state

    state = read_state()
    p = Path(STATE_FILE)
    if p.exists() and state.source != "state":
        return CheckResult(
            "speaker name",
            "warn",
            f"{p} exists but could not be parsed; using {state.name!r}",
            reason=REASON_SPEAKER_NAME_UNPARSEABLE,
        )
    return CheckResult(
        "speaker name",
        "ok",
        f"{state.name!r} ({state.source})",
    )

@doctor_check(label="state dir", needs_cfg=True)
def check_state_dir(cfg: Config) -> CheckResult:
    """The state dir must be writable by the non-root jasper-* daemons.

    ``require_setgid=False``: ``ensure_state_dir`` (install.sh) leaves this
    dir plain ``root:jasper 0770``, and every writer already declares
    ``Group=jasper`` in its own unit, so new entries get the right group from
    the writer's identity rather than by setgid inheritance."""
    p = Path(cfg.usage_db).parent
    if not p.exists():
        return CheckResult(
            "state dir", "warn", f"{p} missing (will be created on first run)",
            reason=REASON_STATE_DIR_MISSING,
        )
    try:
        st = p.stat()
    except OSError as exc:
        return CheckResult(
            "state dir", "fail", f"{p}: {exc}", reason=REASON_STATE_DIR_STAT_FAILED,
        )
    writable, group_name = _group_writable_dir(
        st, expected_group="jasper", require_setgid=False
    )
    if not writable:
        return CheckResult(
            "state dir", "fail",
            f"{p} not writable by the non-root jasper-* daemons "
            f"(group={group_name} mode={oct(st.st_mode & 0o7777)})",
            reason=REASON_STATE_DIR_NOT_WRITABLE,
        )
    return CheckResult("state dir", "ok", str(p))

@doctor_check(label="state group-write", needs_cfg=True)
def check_state_dir_group_writable(cfg: Config) -> CheckResult:
    """The shared, multi-writer state files must be group-`jasper` AND
    group-writable. jasper-voice and jasper-mux co-own
    StateDirectory=jasper, so whichever restarts last re-chowns the tree to
    its own user, and a 0644 file the OTHER daemon does not own then cannot
    be written. UMask=0007 plus the install heal keep these 0660; this flags
    the drift before it bites."""
    return _classify_state_group_write(Path(cfg.usage_db), Path(cfg.volume_state_path))


def _classify_state_group_write(usage_db: Path, volume_state_path: Path) -> CheckResult:
    """Path-parameterized core of ``check_state_dir_group_writable``."""
    import grp
    import stat as _stat

    state_dir = usage_db.parent
    candidates = [
        usage_db,
        state_dir / "conversation_history.db",
        state_dir / "timers.db",
        state_dir / "wake-events" / "wake-events.sqlite3",
        volume_state_path,
    ]
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
            reason=REASON_STATE_GROUP_WRITE_VIOLATION,
        )
    return CheckResult(
        "state group-write", "ok",
        f"{checked} shared file(s) group-`jasper`-writable",
    )
