"""jasper-doctor checks — resilience domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import subprocess
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _RUNTIME_STATE_UNITS,
    _run,
    _service_runtime_states,
)

# Expected StartLimitAction= per critical daemon. T5.1 of the
# watchdog-liveness plan: a restart spiral on any of these critical
# escalates to a clean system reboot rather than waiting for the
# Tier 5 kernel hardware watchdog (which has the "PID 1 alive but
# userspace dead" blind spot documented in HANDOFF-resilience.md).
# Doctor reports drift so a Debian/RPi-OS update that removes our
# unit-file directives surfaces in the next install. See
# docs/HANDOFF-tier5-watchdog-liveness.md "Option B (T5.1)".
_EXPECTED_START_LIMIT_ACTION = {
    "jasper-outputd": "reboot",
    "jasper-camilla": "reboot",
    "jasper-aec-bridge": "reboot",
    "jasper-voice": "reboot",
    "jasper-control": "reboot",
}

def _start_limit_action_of_unit(unit: str) -> str | None:
    """Best-effort: read `StartLimitAction=` from systemd's view of
    the unit. Returns the lowercased action string, or `None` if
    systemctl isn't available (dev host) or the lookup fails."""
    try:
        out = _run(
            ["systemctl", "show", "-p", "StartLimitAction", "--value",
             f"{unit}.service"],
        ).stdout.strip().lower()
        return out or "none"
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

@doctor_check(order=39, group="resilience")
def check_start_limit_action() -> CheckResult:
    """Verify the T5.1 `StartLimitAction=reboot` directive is in
    effect on every critical daemon. Drift here means a Debian /
    RPi-OS update edited the unit, or someone manually disabled the
    escalation. Doctor surfaces this — without StartLimitAction=reboot
    we're back to Tier 5's "PID 1 alive but userspace dead" gap.
    See docs/HANDOFF-tier5-watchdog-liveness.md."""
    drift = []
    for unit, want in _EXPECTED_START_LIMIT_ACTION.items():
        got = _start_limit_action_of_unit(unit)
        if got is None:
            # systemctl unavailable (dev host) — skip cleanly
            return CheckResult(
                "StartLimitAction", "ok",
                "systemctl unavailable — skipped (not Linux?)",
            )
        if got != want:
            drift.append(f"{unit}={got} (want {want})")
    if drift:
        return CheckResult(
            "StartLimitAction", "warn",
            "T5.1 escalation drift: " + ", ".join(drift) +
            ". Re-run install.sh to restore .service files.",
        )
    return CheckResult(
        "StartLimitAction", "ok",
        f"T5.1 reboot escalation active on all {len(_EXPECTED_START_LIMIT_ACTION)} "
        "critical daemons",
    )

@doctor_check(order=40, group="resilience")
def check_service_runtime_state() -> CheckResult:
    """Surface failed units and restart-count changes in one-shot doctor.

    The dashboard shows the same fields continuously, but doctor needs
    to catch the production risk too: a unit can be start-limited or
    repeatedly restarting with no live cgroup left for the resource
    sampler to display."""
    states = _service_runtime_states()
    if states is None:
        return CheckResult(
            "service runtime state", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    failed: list[str] = []
    restarted: list[str] = []
    for unit in _RUNTIME_STATE_UNITS:
        state = states.get(unit) or {}
        active = str(state.get("active_state") or "")
        sub = str(state.get("sub_state") or "")
        result = str(state.get("result") or "")
        try:
            n_restarts = int(state.get("n_restarts") or 0)
        except (TypeError, ValueError):
            n_restarts = 0
        if active == "failed":
            failed.append(f"{unit} state=failed/{sub or '?'} result={result or '?'}")
        elif active in {"activating", "deactivating"}:
            failed.append(f"{unit} state={active}/{sub or '?'}")
        if n_restarts > 0:
            restarted.append(f"{unit} NRestarts={n_restarts}")
    if failed:
        detail = "failed or unstable units: " + ", ".join(failed)
        if restarted:
            detail += "; restarts: " + ", ".join(restarted)
        return CheckResult("service runtime state", "fail", detail)
    if restarted:
        return CheckResult(
            "service runtime state", "warn",
            "restart counts non-zero: " + ", ".join(restarted),
        )
    return CheckResult(
        "service runtime state", "ok",
        f"{len(_RUNTIME_STATE_UNITS)} tracked units have no failed state or restarts",
    )
