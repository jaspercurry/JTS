"""jasper-doctor checks — resilience domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split.

``check_supervisor_reboot_state`` (order 76) was added after the
split — it surfaces the T5.2 reboot rate-limit state file the same
way the sibling resilience state files (WiFi guardian stash,
wifi-scan-repair) get doctor lines."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ...control.bootloop_guard_state import snapshot as _bootloop_guard_snapshot
from ...control.system_supervisor import DEFAULT_REBOOT_STATE_PATH
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _RUNTIME_STATE_UNITS,
    _installed_units,
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
    # Only verify daemons this install profile actually installs. The
    # expected set is the full-speaker one; a streambox does not install
    # the voice/AEC stack, so those absent units are not escalation drift.
    installed = _installed_units(list(_EXPECTED_START_LIMIT_ACTION.keys()))
    if installed is None:
        return CheckResult(
            "StartLimitAction", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    if not installed:
        return CheckResult(
            "StartLimitAction", "ok", "no managed critical daemons installed",
        )
    drift = []
    for unit, want in _EXPECTED_START_LIMIT_ACTION.items():
        if unit not in installed:
            continue
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
        f"T5.1 reboot escalation active on all {len(installed)} "
        "installed critical daemons",
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


# Wall-clock skew tolerance before a future-dated last-reboot timestamp
# is worth a warning. The Pi has no hardware RTC — fake-hwclock restores
# an old time at boot and NTP corrects it within ~a minute — so small
# negative ages are routine and harmless. Beyond this, the supervisor's
# 24h rate-limit window reads as un-elapsed and a genuinely-needed
# reboot is suppressed until the clock catches up (bounded by the skew,
# but invisible without this line).
_REBOOT_STATE_FUTURE_SKEW_SEC = 300.0


def _classify_reboot_state(path: Path, *, now: float | None = None) -> CheckResult:
    """Classify the T5.2 persisted reboot rate-limit state at `path`.

    Split from the check so tests can point it at a tmp file. Granular
    on purpose — the supervisor's own `_read_reboot_state` deliberately
    collapses missing/corrupt to None (fail-open), but the doctor's job
    is to tell the operator WHICH of those states the file is in."""
    name = "supervisor reboot state"
    now = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Normal on a fresh install or any Pi the supervisor has never
        # had to reboot. Not a warning.
        return CheckResult(name, "ok", "no supervisor reboot recorded")
    except OSError as e:
        return CheckResult(
            name, "warn",
            f"unreadable ({e.__class__.__name__}) — supervisor fails open "
            f"(rate-limit unarmed). Check permissions on {path}",
        )
    try:
        data = json.loads(raw)
        last = float(data["last_reboot_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return CheckResult(
            name, "warn",
            f"corrupt — supervisor fails open (rate-limit unarmed). "
            f"Delete to clear: {path}",
        )
    age = now - last
    if age < -_REBOOT_STATE_FUTURE_SKEW_SEC:
        return CheckResult(
            name, "warn",
            f"future-dated by {-age:.0f}s — T5.2 reboot rate-limit is "
            "suppressed until wall-clock catches up (no RTC; is NTP "
            "syncing?). Delete to re-arm: " + str(path),
        )
    return CheckResult(
        name, "ok",
        f"last supervisor reboot {age / 3600:.1f}h ago — 24h rate-limit armed",
    )


# 78.5: slots between the last sync check (order=78) and the async
# CamillaDSP websocket check (order=79), which must sort last — the
# registry invariant pinned by tests/test_doctor_registry.py.
@doctor_check(order=78.5, group="resilience")
def check_bootloop_guard() -> CheckResult:
    """Surface the boot-loop guard marker (T5.1 circuit breaker).

    The guard (deploy/bin/jasper-bootloop-guard, oneshot at boot) is
    fail-open everywhere, so a tripped state — reboot escalation
    disarmed for this boot via runtime StartLimitAction=none drop-ins
    — is otherwise only visible in the journal and on /state. Reads
    the /run marker through the same module jasper-control's /state
    uses (jasper.control.bootloop_guard_state). A missing or corrupt
    marker is normal (guard never ran this boot — dev host, fresh
    install) and reads as armed."""
    name = "boot-loop guard"
    snap = _bootloop_guard_snapshot()
    if not snap.get("ran"):
        return CheckResult(
            name, "ok",
            "no marker this boot — guard armed (T5.1 reboot escalation "
            "active)",
        )
    if snap.get("reload_ok") is False:
        units = [str(u) for u in (snap.get("units") or [])]
        return CheckResult(
            name, "warn",
            "boot-loop guard attempted to disarm reboot escalation, but "
            "`systemctl daemon-reload` failed, so the drop-ins were not "
            "confirmed active. Units with written drop-ins: " +
            (", ".join(units) or "(no units recorded)") +
            ". Check `journalctl -u jasper-bootloop-guard`; after fixing "
            "the systemd error, re-run `jasper-bootloop-guard --reason "
            "manual` (or reboot).",
        )
    if not snap.get("tripped"):
        return CheckResult(
            name, "ok",
            f"guard armed ({snap.get('boots_in_window')} boot(s) in a "
            f"{snap.get('window_sec')}s window, threshold "
            f"{snap.get('threshold')})",
        )
    units = [str(u) for u in (snap.get("units") or [])]
    return CheckResult(
        name, "warn",
        "TRIPPED — boot loop detected; reboot escalation disarmed this "
        "boot for: " + (", ".join(units) or "(no units recorded)") +
        ". A unit exhausting its restart budget parks failed instead of "
        "rebooting. Fix the failing daemon, then `systemctl reset-failed "
        "<unit> && systemctl start <unit>` (drop-ins self-clear on the "
        "next boot).",
    )


@doctor_check(order=76, group="resilience")
def check_supervisor_reboot_state() -> CheckResult:
    """Surface the T5.2 reboot rate-limit state file.

    The supervisor itself reads this file fail-open (missing/corrupt →
    rate-limit unarmed), which is the right runtime behaviour but means
    a corrupt or future-dated file is silent in normal operation. The
    doctor line makes those two states visible: corrupt → the
    reboot-loop guard is unarmed; future-dated → a genuinely-needed
    reboot is suppressed until the clock catches up."""
    return _classify_reboot_state(DEFAULT_REBOOT_STATE_PATH)
