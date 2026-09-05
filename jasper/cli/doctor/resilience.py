# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — resilience domain."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ...control.bootloop_guard_state import snapshot as _bootloop_guard_snapshot
from ...control.system_supervisor import DEFAULT_REBOOT_STATE_PATH
from ...install_profile import is_streambox_install_profile, read_install_profile
from ...service_units import unit_unstable
from ...voice.input_presence import voice_parked_no_mic
from ... import outputd_failure_reconcile_state
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import (
    REASON_SYSTEMCTL_UNAVAILABLE,
    CheckResult,
    _ONESHOT_RUNTIME_STATE_UNITS,
    _RUNTIME_STATE_UNITS,
)

# Machine-stable codes naming which branch of a resilience check produced a
# result (AGENTS.md: tests pin status + reason, never detail prose). A check
# that genuinely observed nothing (systemctl/jasper-control unreachable)
# reports "skipped" with a reason rather than "ok" — ADR-0233 rule 3.
REASON_UNITS_FAILED_OR_UNSTABLE = "units_failed_or_unstable"
REASON_UNITS_RESTARTED = "units_restarted"

REASON_VOICE_UNIT_NOT_FULL_PROFILE = "voice_unit_not_full_profile"
REASON_VOICE_UNIT_UNOBSERVED = "voice_unit_unobserved"
REASON_VOICE_UNIT_PARKED_NO_INPUT = "voice_unit_parked_no_voice_input"
REASON_VOICE_UNIT_INACTIVE = "voice_unit_inactive"

REASON_SUPERVISOR_ISSUES = "supervisor_issues"
REASON_CONTROL_UNAVAILABLE = "supervisor_snapshots_control_unavailable"

REASON_SNAPSHOT_UNAVAILABLE = "supply_voltage_snapshot_unavailable"
REASON_THROTTLED_BITS_UNREPORTED = "supply_voltage_throttled_bits_unreported"
REASON_UNDERVOLTAGE_NOW = "supply_voltage_undervoltage_now"
REASON_UNDERVOLTAGE_HISTORY = "supply_voltage_undervoltage_history"

REASON_REBOOT_STATE_ABSENT = "reboot_state_absent"
REASON_REBOOT_STATE_UNREADABLE = "reboot_state_unreadable"
REASON_REBOOT_STATE_CORRUPT = "reboot_state_corrupt"
REASON_REBOOT_STATE_FUTURE_DATED = "reboot_state_future_dated"
REASON_REBOOT_STATE_ARMED = "reboot_state_armed"

REASON_OUTPUTD_RECONCILE_UNOBSERVED = "outputd_failure_reconcile_unobserved"
REASON_OUTPUTD_PARK_RECORD_STALE = "outputd_park_record_stale"
REASON_OUTPUTD_UNIT_FAILED = "outputd_failed_without_park_record"
REASON_OUTPUTD_UNIT_UNSTABLE = "outputd_unstable_without_park_record"
REASON_OUTPUTD_PARKED = "outputd_failure_reconcile_parked"

REASON_BOOTLOOP_GUARD_NOT_RUN = "bootloop_guard_not_run"
REASON_BOOTLOOP_GUARD_RELOAD_FAILED = "bootloop_guard_reload_failed"
REASON_BOOTLOOP_GUARD_ARMED = "bootloop_guard_armed"
REASON_BOOTLOOP_GUARD_TRIPPED = "bootloop_guard_tripped"

@doctor_check()
def check_service_runtime_state() -> CheckResult:
    """Judge the tracked units' runtime state in the one-shot doctor.

    ``failed`` fails; ``activating``/``deactivating`` on a non-oneshot fails
    too (a stuck start, not a transition anyone is waiting out). A non-zero
    ``NRestarts`` is reported but does NOT fail or warn: systemd keeps the
    counter until ``reset-failed`` or a reboot, so one auto-restart days ago
    would otherwise latch this row for the life of the boot. The count still
    rides in the detail — it is the only place the operator sees a unit that
    is up now but has been restarting."""
    states = evidence.unit_states()
    if states is None:
        return CheckResult(
            "service runtime state", "skipped",
            "systemctl unavailable — skipped (not Linux?)",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
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
        elif unit_unstable(state) and unit not in _ONESHOT_RUNTIME_STATE_UNITS:
            # A oneshot sits in `activating` for its whole normal run, so only
            # its `failed` end-state is a finding.
            failed.append(f"{unit} state={active}/{sub or '?'}")
        if n_restarts > 0:
            restarted.append(f"{unit} NRestarts={n_restarts}")
    if failed:
        detail = "failed or unstable units: " + ", ".join(failed)
        if restarted:
            detail += "; restarts: " + ", ".join(restarted)
        return CheckResult(
            "service runtime state", "fail", detail,
            reason=REASON_UNITS_FAILED_OR_UNSTABLE,
        )
    if restarted:
        return CheckResult(
            "service runtime state", "ok",
            "no failed or unstable units; restart counts non-zero (cumulative "
            "since the last reset-failed or reboot, not a live fault): "
            + ", ".join(restarted),
            reason=REASON_UNITS_RESTARTED,
        )
    return CheckResult(
        "service runtime state", "ok",
        f"{len(_RUNTIME_STATE_UNITS)} tracked units have no failed state or restarts",
    )


_VOICE_UNIT = "jasper-voice.service"


def _install_profile_is_streambox() -> bool:
    """True on the streambox tier. Fails toward False so an unparseable
    marker keeps the louder full-speaker row rather than silently skipping
    it."""
    try:
        return is_streambox_install_profile(read_install_profile())
    except (TypeError, ValueError, OSError):
        return False


@doctor_check()
def check_voice_unit_running() -> CheckResult:
    """jasper-voice is up on a tier whose speaker answers a wake word.

    The gap this closes: ``check_service_runtime_state`` counts only
    ``failed`` and stuck-mid-transition units, so a cleanly ``inactive``
    jasper-voice — stopped by hand, never enabled, or parked by
    ``RestartPreventExitStatus=66 78`` (no provider configured, mic
    unopenable) — produced no row at all while the speaker could not answer.

    ``speaker_silent`` is deliberately NOT set. That flag means the speaker
    emits nothing; music still plays with the voice daemon down. What is
    silent here is the ASSISTANT.

    Two states are not a fault and read ``skipped``: the streambox tier has
    no always-on wake loop, and the unit's
    ``ConditionPathExists=!/var/lib/jasper/voice-input-absent`` parks it
    ``inactive`` on a box the AEC reconciler found to have neither a local
    nor an accessory mic.
    """
    label = "voice daemon running"
    if _install_profile_is_streambox():
        return CheckResult(
            label, "skipped",
            "streambox tier — no always-on wake loop to keep running",
            reason=REASON_VOICE_UNIT_NOT_FULL_PROFILE,
        )
    state = evidence.unit_state(_VOICE_UNIT)
    if state is None or str(state.get("load_state") or "") != "loaded":
        return CheckResult(
            label, "skipped",
            f"{_VOICE_UNIT} not observable — systemctl unavailable, or the "
            "unit is not installed",
            reason=REASON_VOICE_UNIT_UNOBSERVED,
        )
    active = str(state.get("active_state") or "")
    if active != "inactive":
        return CheckResult(
            label, "ok",
            f"{_VOICE_UNIT} is {active} — a failed or stuck unit is "
            "`service runtime state`'s row",
        )
    if voice_parked_no_mic():
        return CheckResult(
            label, "skipped",
            f"{_VOICE_UNIT} parked by its voice-input gate — the AEC "
            "reconciler found neither a local nor an accessory mic",
            reason=REASON_VOICE_UNIT_PARKED_NO_INPUT,
        )
    return CheckResult(
        label, "fail",
        f"{_VOICE_UNIT} is inactive (not failed) on the full profile, so no "
        "wake word gets an answer. `systemctl status jasper-voice` names "
        "which of the parking exits it took (78 = no provider configured, "
        "66 = mic could not be opened); otherwise `systemctl start "
        "jasper-voice`.",
        reason=REASON_VOICE_UNIT_INACTIVE,
    )


def _int_field(snapshot: dict[str, Any], key: str) -> int:
    try:
        return int(snapshot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _classify_supervisor_snapshots(resilience: dict[str, Any]) -> CheckResult:
    """Classify ``/state.resilience`` supervisor snapshots, so a
    non-converging repair loop is visible during one-shot diagnostics too.
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
            reason=REASON_SUPERVISOR_ISSUES,
        )
    return CheckResult(
        "supervisor runtime snapshots",
        "ok",
        "supervisor snapshots quiet",
    )


def _nested_dict(payload: Any, *keys: str) -> dict[str, Any] | None:
    """Drill a nested dict out of a jasper-control HTTP payload along
    ``keys``, fail-soft to None on any shape mismatch."""
    for key in keys:
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload if isinstance(payload, dict) else None


def _read_resilience_state() -> dict[str, Any] | None:
    return _nested_dict(evidence.control_state().payload, "resilience")


@doctor_check()
def check_supervisor_runtime_snapshots() -> CheckResult:
    """Surface supervisor state that otherwise only appears in ``/state``."""
    resilience = _read_resilience_state()
    if resilience is None:
        return CheckResult(
            "supervisor runtime snapshots",
            "skipped",
            "jasper-control /state unavailable",
            reason=REASON_CONTROL_UNAVAILABLE,
        )
    return _classify_supervisor_snapshots(resilience)


# vcgencmd get_throttled bit layout (Pi firmware): raw bit 0 = under-voltage
# now, raw bit 16 = under-voltage since boot.
# jasper.control.system_metrics._read_throttled() splits the raw value into
# (throttled_now=raw & 0xF, throttled_history=(raw >> 16) & 0xF) before
# publishing it, so the published throttled_history is pre-shifted — its own
# bit 0 is raw bit 16.
_UNDER_VOLTAGE_NOW_BIT = 0x1
_UNDER_VOLTAGE_HISTORY_BIT = 0x1


def _read_system_metrics_current() -> dict[str, Any] | None:
    return _nested_dict(
        evidence.control_system_snapshot().payload, "metrics", "current",
    )


@doctor_check()
def check_supply_voltage() -> CheckResult:
    """Surface the Pi firmware's under-voltage flags. jasper-control's
    system-metrics sampler already polls ``vcgencmd get_throttled`` on a
    timer; doctor is a one-shot CLI and must not add a second poller.

    Under-voltage NOW fails — there is something to do about it. The
    since-boot history bit reports ``ok``: the firmware latches it until the
    next reboot, so a single marginal moment during a cold boot would warn
    for the whole uptime with nothing left to act on. The bits stay in the
    detail either way."""
    name = "Supply voltage"
    current = _read_system_metrics_current()
    if current is None:
        return CheckResult(
            name, "skipped", "jasper-control /system/snapshot unavailable",
            reason=REASON_SNAPSHOT_UNAVAILABLE,
        )
    now_bits = current.get("throttled_now")
    history_bits = current.get("throttled_history")
    if not isinstance(now_bits, int) or not isinstance(history_bits, int):
        return CheckResult(
            name, "skipped", "throttled bits not reported",
            reason=REASON_THROTTLED_BITS_UNREPORTED,
        )
    if now_bits & _UNDER_VOLTAGE_NOW_BIT:
        return CheckResult(
            name, "fail",
            f"under-voltage NOW — throttled={hex(now_bits)}, bit 0 set. "
            "Bit 0 is the Pi firmware's under-voltage-detected flag "
            "(`vcgencmd get_throttled`). Check the power supply and cable.",
            reason=REASON_UNDERVOLTAGE_NOW,
        )
    if history_bits & _UNDER_VOLTAGE_HISTORY_BIT:
        return CheckResult(
            name, "ok",
            f"under-voltage occurred since boot — throttled_history="
            f"{hex(history_bits)}, bit 0 set (raw vcgencmd bit 16 — "
            "jasper-control publishes throttled_history pre-shifted). This "
            "is the Pi firmware's under-voltage-has-occurred flag "
            "(`vcgencmd get_throttled`); not active now, but the supply was "
            "marginal at some point since the last boot.",
            reason=REASON_UNDERVOLTAGE_HISTORY,
        )
    return CheckResult(
        name, "ok",
        f"no under-voltage flags (throttled_now={hex(now_bits)}, "
        f"throttled_history={hex(history_bits)})",
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
    """Classify the persisted reboot rate-limit state at `path`.

    Split from the check so tests can point it at a tmp file. Granular on
    purpose: the supervisor's own `_read_reboot_state` collapses
    missing/corrupt to None (fail-open), but the doctor must tell the operator
    WHICH of those states the file is in."""
    name = "supervisor reboot state"
    now = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Normal on a fresh install, or any Pi the supervisor has never had
        # to reboot.
        return CheckResult(
            name, "ok", "no supervisor reboot recorded",
            reason=REASON_REBOOT_STATE_ABSENT,
        )
    except OSError as e:
        return CheckResult(
            name, "warn",
            f"unreadable ({e.__class__.__name__}) — supervisor fails open "
            f"(rate-limit unarmed). Check permissions on {path}",
            reason=REASON_REBOOT_STATE_UNREADABLE,
        )
    try:
        data = json.loads(raw)
        last = float(data["last_reboot_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return CheckResult(
            name, "warn",
            f"corrupt — supervisor fails open (rate-limit unarmed). "
            f"Delete to clear: {path}",
            reason=REASON_REBOOT_STATE_CORRUPT,
        )
    age = now - last
    if age < -_REBOOT_STATE_FUTURE_SKEW_SEC:
        return CheckResult(
            name, "warn",
            f"future-dated by {-age:.0f}s — T5.2 reboot rate-limit is "
            "suppressed until wall-clock catches up (no RTC; is NTP "
            "syncing?). Delete to re-arm: " + str(path),
            reason=REASON_REBOOT_STATE_FUTURE_DATED,
        )
    return CheckResult(
        name, "ok",
        f"last supervisor reboot {age / 3600:.1f}h ago — 24h rate-limit armed",
        reason=REASON_REBOOT_STATE_ARMED,
    )


# Wall-clock before this reads as "the clock was not set yet", not as an age:
# /run records survive no reboot, but a Pi with no RTC stamps 1970 until NTP
# lands, and "2000000000s ago" is worse than saying so.
_CLOCK_SET_EPOCH = 1577836800  # 2020-01-01T00:00:00Z


def _parked_ago(parked_at: int | None, *, now: float | None = None) -> str:
    if parked_at is None:
        return "at an unrecorded time"
    if parked_at < _CLOCK_SET_EPOCH:
        return "with the clock unset at park time"
    age = (time.time() if now is None else now) - parked_at
    return f"{age:.0f}s ago"


@doctor_check()
def check_outputd_failure_reconcile_park() -> CheckResult:
    """outputd is running, and carries no park record from its stop helper.

    Why a stop can park outputd for good: see
    deploy/bin/jasper-outputd-failure-reconcile. This check owns outputd's
    runtime state (it is deliberately not in ``_RUNTIME_STATE_UNITS``), so one
    failed outputd is one fail row — including a stuck ``activating``/
    ``deactivating`` unit, which warns rather than fails: not yet silent, but
    not settled either. ``speaker_silent`` on both fail branches: outputd owns
    the DAC write loop (docs/audio-paths.md), so with it down nothing writes
    the card and the speaker emits NOTHING.
    """
    label = "outputd failure-reconcile"
    reader = outputd_failure_reconcile_state
    unit_state = evidence.unit_state(reader.UNIT)
    state = reader.snapshot(unit_state)
    reason = state.get("reason")
    path = state.get("path")

    if reason == reader.REASON_UNOBSERVED:
        error = state.get("error")
        return CheckResult(
            label, "skipped",
            f"park record at {path} unreadable ({error})" if error
            else "systemctl unavailable — a park cannot be ruled out",
            reason=REASON_OUTPUTD_RECONCILE_UNOBSERVED,
        )
    if reason == reader.REASON_PARKED:
        return CheckResult(
            label, "fail",
            "PARKED — jasper-outputd's stop helper recorded a park "
            f"{_parked_ago(state.get('parked_at'))} "
            f"(exit_status={state.get('exit_status') or '?'}, "
            f"reason={state.get('park_reason') or '?'}) and nothing retries "
            f"it. Fix the output env, `systemctl restart jasper-outputd`, "
            f"then delete {path} if it survives.",
            speaker_silent=True,
            reason=REASON_OUTPUTD_PARKED,
        )
    if reason == reader.REASON_UNIT_FAILED:
        return CheckResult(
            label, "fail",
            f"{reader.UNIT} is failed with no park record — its stop helper "
            "did not judge this terminal, so systemd's Restart=on-failure "
            "should be retrying. Check `journalctl -u jasper-outputd`.",
            speaker_silent=True,
            reason=REASON_OUTPUTD_UNIT_FAILED,
        )
    if reason == reader.REASON_UNIT_UNSTABLE:
        active = str((unit_state or {}).get("active_state") or "?")
        sub = str((unit_state or {}).get("sub_state") or "?")
        n_restarts = (unit_state or {}).get("n_restarts")
        detail = (
            f"{reader.UNIT} is {active}/{sub} with no park record — stuck "
            "mid-transition. Check `systemctl status jasper-outputd`."
        )
        if n_restarts:
            detail += f" NRestarts={n_restarts}."
        return CheckResult(
            label, "warn", detail,
            reason=REASON_OUTPUTD_UNIT_UNSTABLE,
        )
    if reason == reader.REASON_RECORD_STALE:
        return CheckResult(
            label, "warn",
            f"park record at {path} is stale — outputd is running, so the "
            "unit's ExecStartPost removal did not fire. Delete it; a later "
            "unrelated failure would otherwise read as this park.",
            reason=REASON_OUTPUTD_PARK_RECORD_STALE,
        )
    return CheckResult(
        label, "ok", f"{reader.UNIT} is running and carries no park record",
    )


@doctor_check()
def check_bootloop_guard() -> CheckResult:
    """Surface the boot-loop guard marker.

    The guard (deploy/bin/jasper-bootloop-guard, oneshot at boot) is fail-open
    everywhere, so a tripped state — reboot escalation disarmed for this boot
    via runtime StartLimitAction=none drop-ins — is otherwise visible only in
    the journal and on /state. A missing or corrupt marker is normal (the
    guard never ran this boot) and reads as armed."""
    name = "boot-loop guard"
    snap = _bootloop_guard_snapshot()
    if not snap.get("ran"):
        return CheckResult(
            name, "ok",
            "no marker this boot — guard armed (T5.1 reboot escalation "
            "active)",
            reason=REASON_BOOTLOOP_GUARD_NOT_RUN,
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
            reason=REASON_BOOTLOOP_GUARD_RELOAD_FAILED,
        )
    if not snap.get("tripped"):
        return CheckResult(
            name, "ok",
            f"guard armed ({snap.get('boots_in_window')} boot(s) in a "
            f"{snap.get('window_sec')}s window, threshold "
            f"{snap.get('threshold')})",
            reason=REASON_BOOTLOOP_GUARD_ARMED,
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
        reason=REASON_BOOTLOOP_GUARD_TRIPPED,
    )


@doctor_check()
def check_supervisor_reboot_state() -> CheckResult:
    """Surface the reboot rate-limit state file.

    The supervisor reads it fail-open (missing/corrupt → rate-limit unarmed),
    which is the right runtime behaviour but leaves a corrupt or future-dated
    file silent. This line makes both visible: corrupt → the reboot-loop guard
    is unarmed; future-dated → a genuinely-needed reboot is suppressed until
    the clock catches up."""
    return _classify_reboot_state(DEFAULT_REBOOT_STATE_PATH)
