# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — env domain."""
from __future__ import annotations

import re
from pathlib import Path
from ...config import Config
from ...env_load import env_file_path, read_env_file_state
from ...secret_redaction import SECRET_ENV_SUFFIX_RE
from ._registry import doctor_check
from ._shared import CheckResult, _group_writable_dir

# Machine-stable codes naming which branch of an env check produced a result
# (AGENTS.md: tests pin status + reason, never detail prose).
REASON_ENV_FILE_MISSING = "env_file_missing"
REASON_ENV_FILE_UNREADABLE = "env_file_unreadable"
REASON_SPEAKER_NAME_UNPARSEABLE = "speaker_name_unparseable"
REASON_STATE_DIR_MISSING = "state_dir_missing"
REASON_STATE_DIR_STAT_FAILED = "state_dir_stat_failed"
REASON_STATE_DIR_NOT_WRITABLE = "state_dir_not_writable"
REASON_STATE_GROUP_WRITE_VIOLATION = "state_group_write_violation"
REASON_SECRET_IN_ENV_FILE = "secret_in_env_file"

# The suffix rule alone, not `SECRET_ENV_NAME_RE`: that export also matches
# `JASPER_MTA_BUSTIME_KEY`, a key `jasper/web/transit_setup.py` documents as
# living in jasper.env (operator-pasted or migrated there by install.sh) —
# not a compartment escapee. `fullmatch` at the call site anchors the match
# to the whole key (an unanchored search would also flag e.g.
# "GEMINI_API_KEYWORD").
_SECRET_KEY_RE = re.compile(rf"[A-Za-z_][A-Za-z0-9_]*{SECRET_ENV_SUFFIX_RE}")

@doctor_check()
def check_env_file() -> CheckResult:
    p = Path(env_file_path())
    if not p.exists():
        return CheckResult(
            "env file", "fail", f"{p} missing — re-run install.sh",
            reason=REASON_ENV_FILE_MISSING,
        )
    wizard = Path("/var/lib/jasper/voice_provider.env")
    if wizard.exists():
        return CheckResult("env file", "ok", f"{p} (+ wizard {wizard.name})")
    return CheckResult("env file", "ok", str(p))

@doctor_check()
def check_env_file_secrets() -> CheckResult:
    """`/etc/jasper/jasper.env` is `0640` group `jasper`, so a secret resting
    there is readable by every daemon."""
    path = env_file_path()
    state = read_env_file_state(path)
    if state.status == "missing":
        return CheckResult(
            "env file secrets", "skipped", f"{path} missing",
            reason=REASON_ENV_FILE_MISSING,
        )
    if state.status == "unreadable":
        # jasper-doctor runs as root, so "unreadable" here means undecodable
        # bytes or a real permission fault, not the ordinary absent-file
        # case — systemd still exports every key from this file to every
        # daemon regardless, so this is worth a warn, not a silent skip.
        return CheckResult(
            "env file secrets", "warn", f"can't read {path}: {state.error}",
            reason=REASON_ENV_FILE_UNREADABLE,
        )
    offenders = sorted(
        key for key, value in state.values.items()
        if value and _SECRET_KEY_RE.fullmatch(key)
    )
    if offenders:
        return CheckResult(
            "env file secrets", "fail",
            f"{path} holds a secret value for: {', '.join(offenders)} — "
            "re-run the owning wizard (or install.sh, which runs "
            "migrate_voice_keys_split / migrate_google_routes_key for the "
            "LLM and Google Routes keys) to move it to its compartment",
            reason=REASON_SECRET_IN_ENV_FILE,
        )
    return CheckResult("env file secrets", "ok", path)

@doctor_check()
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
