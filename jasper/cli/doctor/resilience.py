# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
import time
from pathlib import Path
from typing import Any

from ...control.bootloop_guard_state import snapshot as _bootloop_guard_snapshot
from ...control.system_supervisor import DEFAULT_REBOOT_STATE_PATH
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    _RUNTIME_STATE_UNITS,
    _installed_units,
    _service_runtime_states,
    _systemctl_show_property,
)

# Expected recovery policy per critical daemon. Most critical daemons keep
# the T5.1 direct clean-reboot ladder. Camilla owns a narrower recovery
# contract after the 2026-06-25 JTS5 incident: restart-limit exhaustion runs
# a forensics/recovery oneshot instead of rebooting immediately, because its
# observed failure class is often ALSA ownership churn where holder evidence
# matters and the Pi should remain reachable if the graph cannot converge.
_EXPECTED_START_LIMIT_POLICY = {
    "jasper-outputd": {"action": "reboot"},
    "jasper-camilla": {
        "action": "none",
        "on_failure": "jasper-camilla-recover.service",
    },
    "jasper-aec-bridge": {"action": "reboot"},
    "jasper-voice": {"action": "reboot"},
    "jasper-control": {"action": "reboot"},
}

@doctor_check(order=39, group="resilience")
def check_start_limit_action() -> CheckResult:
    """Verify the restart-burst recovery policy for critical daemons.

    Most units use the T5.1 ``StartLimitAction=reboot`` directive. Camilla
    uses ``StartLimitAction=none`` plus ``OnFailure=jasper-camilla-recover``
    so ALSA-busy graph failures preserve forensics and keep the Pi reachable.
    Drift here means a Debian/RPi-OS update edited the unit, or someone
    manually disabled the escalation/recovery path. Doctor surfaces this —
    without these policies we're back to Tier 5's "PID 1 alive but userspace
    dead" gap, or to a Camilla reboot loop with no holder evidence. See
    docs/HANDOFF-tier5-watchdog-liveness.md."""
    expected = _EXPECTED_START_LIMIT_POLICY
    # Verify only the daemons this profile installs (a streambox omits the
    # voice/AEC stack), then read the directive for those in one batched
    # systemctl call — same shape as check_oom_score_adj.
    installed = _installed_units(list(expected))
    if installed is None:
        return CheckResult(
            "StartLimitAction", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    units = [u for u in expected if u in installed]
    if not units:
        return CheckResult(
            "StartLimitAction", "ok", "no managed critical daemons installed",
        )
    actions = _systemctl_show_property("StartLimitAction", units)
    if actions is None:
        return CheckResult(
            "StartLimitAction", "ok",
            "systemctl unavailable — skipped (not Linux?)",
        )
    on_failure_units = [
        unit for unit in units if expected[unit].get("on_failure")
    ]
    on_failure: dict[str, str] = {}
    if on_failure_units:
        values = _systemctl_show_property("OnFailure", on_failure_units)
        if values is None:
            return CheckResult(
                "StartLimitAction", "ok",
                "systemctl unavailable — skipped (not Linux?)",
            )
        on_failure = dict(zip(on_failure_units, values))
    drift = []
    for unit, raw in zip(units, actions):
        got = (raw or "").strip().lower() or "none"
        want = expected[unit]["action"]
        if got != want:
            drift.append(f"{unit}={got} (want {want})")
        want_on_failure = expected[unit].get("on_failure")
        if want_on_failure:
            got_on_failure = on_failure.get(unit, "").strip() or "none"
            if want_on_failure not in got_on_failure.split():
                drift.append(
                    f"{unit} OnFailure={got_on_failure} "
                    f"(want {want_on_failure})"
                )
    if drift:
        return CheckResult(
            "StartLimitAction", "warn",
            "critical restart policy drift: " + ", ".join(drift) +
            ". Re-run install.sh to restore .service files.",
        )
    return CheckResult(
        "StartLimitAction", "ok",
        f"critical restart policy active on all {len(units)} "
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


def _int_field(snapshot: dict[str, Any], key: str) -> int:
    try:
        return int(snapshot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _classify_supervisor_snapshots(resilience: dict[str, Any]) -> CheckResult:
    """Classify ``/state.resilience`` supervisor snapshots for doctor.

    The supervisors already expose their counters in ``/state``; this is
    the doctor-facing reader so a non-converging repair loop is visible
    during one-shot diagnostics too.
    """
    issues: list[str] = []

    shairport = resilience.get("shairport")
    if isinstance(shairport, dict) and shairport.get("enabled") is not False:
        consecutive = _int_field(shairport, "consecutive_failures")
        restarts = _int_field(shairport, "restart_count")
        suppressed = _int_field(shairport, "suppressed_count")
        if consecutive:
            issues.append(f"shairport probe failing consecutive={consecutive}")
        if restarts:
            issues.append(f"shairport supervisor restarts={restarts}")
        if suppressed:
            issues.append(f"shairport restart suppressed={suppressed}")

    grouping = resilience.get("grouping_supervisor")
    if isinstance(grouping, dict) and grouping.get("enabled") is not False:
        consecutive = _int_field(grouping, "consecutive_starved")
        if grouping.get("last_poll_starved") is True or consecutive:
            issues.append(f"grouping lane starved consecutive={consecutive}")
        kicks = _int_field(grouping, "kick_count")
        rate_limited = _int_field(grouping, "rate_limited_count")
        if kicks:
            issues.append(f"grouping reconciler kicks={kicks}")
        if rate_limited:
            issues.append(f"grouping reconciler kick rate-limited={rate_limited}")
        binding = grouping.get("binding")
        if isinstance(binding, dict):
            failed = _int_field(binding, "failed_total")
            if failed:
                issues.append(f"grouping snapcast binding repair failures={failed}")
        reassert = grouping.get("reassert")
        if isinstance(reassert, dict):
            failed = _int_field(reassert, "failed_total")
            if failed:
                issues.append(f"grouping peer reassert failures={failed}")
            if reassert.get("last_ok") is False:
                detail = str(reassert.get("last_detail") or "failed")
                issues.append(f"grouping peer reassert last failed: {detail}")

    system = resilience.get("system_supervisor")
    if isinstance(system, dict) and system.get("enabled") is not False:
        consecutive = _int_field(system, "consecutive_failures")
        reboots = _int_field(system, "reboot_count")
        suppressed = _int_field(system, "suppressed_count")
        failed_probe = str(system.get("last_failed_probe") or "")
        if consecutive:
            suffix = f" last_failed={failed_probe}" if failed_probe else ""
            issues.append(f"system supervisor probe failing consecutive={consecutive}{suffix}")
        if reboots:
            issues.append(f"system supervisor reboots={reboots}")
        if suppressed:
            issues.append(f"system supervisor reboot suppressed={suppressed}")

    if issues:
        return CheckResult(
            "supervisor runtime snapshots",
            "warn",
            "; ".join(issues),
        )
    return CheckResult(
        "supervisor runtime snapshots",
        "ok",
        "supervisor snapshots quiet",
    )


def _read_resilience_state() -> dict[str, Any] | None:
    try:
        from ...control import client as control

        state = control.get_state(timeout=2.0)
    except (OSError, RuntimeError, ValueError):
        return None
    resilience = state.get("resilience") if isinstance(state, dict) else None
    return resilience if isinstance(resilience, dict) else None


@doctor_check(order=40.5, group="resilience")
def check_supervisor_runtime_snapshots() -> CheckResult:
    """Surface supervisor state that otherwise only appears in ``/state``."""
    resilience = _read_resilience_state()
    if resilience is None:
        return CheckResult(
            "supervisor runtime snapshots",
            "ok",
            "skipped — jasper-control /state unavailable",
        )
    return _classify_supervisor_snapshots(resilience)


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
