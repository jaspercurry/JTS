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
