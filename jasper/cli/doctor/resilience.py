# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — resilience domain."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ...accessories import status as accessory_status
from ...control.bootloop_guard_state import snapshot as _bootloop_guard_snapshot
from ...control.restart_broker import _SELF_UNIT as _CONTROL_UNIT
from ...control.heal_supervisor import TICK_INTERVAL_SEC as _HEAL_TICK_SEC
from ...control.system_supervisor import DEFAULT_REBOOT_STATE_PATH
from ...service_units import JASPER_VOICE_SERVICE, unit_unstable, unit_uptime_sec
from ...voice.input_presence import voice_parked_no_mic
from ...voice.provider_state import read_active_provider_state
from ... import outputd_failure_reconcile_state
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import (
    REASON_VOICE_UNIT_NOT_FULL_PROFILE,
    CheckResult,
    control_signal_path,
    _nested_dict,
    _ONESHOT_RUNTIME_STATE_UNITS,
    _RUNTIME_STATE_UNITS,
    silence_unobserved,
    speaker_silence_code,
    _systemctl_unavailable_result,
)

# Machine-stable codes naming which branch of a resilience check produced a
# result (AGENTS.md: tests pin status + reason, never detail prose). A check
# that genuinely observed nothing (systemctl/jasper-control unreachable)
# reports "skipped" with a reason rather than "ok" — ADR-0233 rule 3.
REASON_UNITS_FAILED_OR_UNSTABLE = "units_failed_or_unstable"
REASON_UNITS_RESTARTED = "units_restarted"

REASON_REQUIRED_UNIT_INACTIVE = "required_unit_inactive"

REASON_ACCESSORY_BRIDGE_RESTART_LOOP = "accessory_bridge_restart_loop"
REASON_ACCESSORY_STATUS_UNAVAILABLE = "accessory_status_unavailable"
REASON_ACCESSORY_BRIDGES_NOT_CONFIGURED = "accessory_bridges_not_configured"

REASON_VOICE_UNIT_UNOBSERVED = "voice_unit_unobserved"
REASON_VOICE_UNIT_PARKED_NO_INPUT = "voice_unit_parked_no_voice_input"
REASON_VOICE_UNIT_INACTIVE = "voice_unit_inactive"
REASON_VOICE_UNIT_INACTIVE_PAIRED_REMOTE = "voice_unit_inactive_paired_remote"
REASON_VOICE_UNIT_NO_PROVIDER = "voice_unit_no_provider_configured"

REASON_SIGNAL_PATH_UNOBSERVED = "signal_path_unobserved"

REASON_SUPERVISOR_ISSUES = "supervisor_issues"
REASON_HEAL_UNOBSERVED = "heal_unobserved"
REASON_HEAL_STALE = "heal_stale"
REASON_HEAL_RECENT = "heal_recent"

# Ticks the heal supervisor may miss before its row warns.
_HEAL_STALE_TICKS = 3
REASON_CONTROL_UNAVAILABLE = "supervisor_snapshots_control_unavailable"
REASON_SUPERVISOR_COUNTERS_RESET = "supervisor_counters_reset"

# A jasper-control restart wipes every supervisor counter with no marker
# of its own; within this window afterward, a quiet row says so instead
# of reading as settled history. A nonzero counter gets no such grace.
_RESET_WINDOW_SEC = 300.0

REASON_SNAPSHOT_UNAVAILABLE = "supply_voltage_snapshot_unavailable"
REASON_SUPPLY_VOLTAGE_SAMPLER_STALE = "supply_voltage_sampler_stale"
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

@doctor_check(core=True)
def check_service_runtime_state() -> CheckResult:
    """Judge the tracked units' runtime state: `failed`, or a non-oneshot
    stuck in `activating`/`deactivating`, fails the row. A non-zero
    `NRestarts` rides in the detail but is informational only — systemd
    latches the counter until `reset-failed` or reboot, so it cannot be
    acted on."""
    states = evidence.unit_states()
    if states is None:
        return _systemctl_unavailable_result("service runtime state")
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


# Units both install profiles install and enable, whose cleanly `inactive`
# state no other row reports. One down unit is one row, so a unit whose own
# check already names it stays out: nginx and jasper-control belong to
# `web.check_management_surface`, and the audio-path daemons to
# `_service_state_failure`, `check_outputd_failure_reconcile_park`,
# `renderers` and `check_voice_unit_running` below.
_REQUIRED_ACTIVE_UNITS: tuple[str, ...] = (
    "jasper-input.service",
    # A `.path` unit reads `active` while it WAITS, so a stopped one is
    # `inactive`, never `failed`.
    "jasper-accessory-reconcile.path",
    # `web.check_wizard_socket_start_limits` reads an `inactive` wizard
    # socket as "not on this profile" and owns only their `failed` state,
    # so a stopped listener is this row's. NOT jasper-system-web.socket:
    # `web.check_management_surface` already probes through it.
    "jasper-web.socket",
)


@doctor_check(core=True)
def check_required_units_active() -> CheckResult:
    """Every required unit is running.

    The gap: ``check_service_runtime_state`` judges only ``failed`` and
    stuck-mid-transition units, so a cleanly ``inactive`` one — stopped by
    hand, never enabled, an install that did not finish — produced no row.
    """
    label = "required units active"
    states = evidence.unit_states()
    if states is None:
        return _systemctl_unavailable_result(label)
    down: list[str] = []
    for unit in _REQUIRED_ACTIVE_UNITS:
        state = states.get(unit) or {}
        active = str(state.get("active_state") or "unknown")
        if active != "inactive":
            continue
        load_state = str(state.get("load_state") or "unknown")
        down.append(
            f"{unit} is {active}"
            + (f"/{load_state}" if load_state != "loaded" else "")
        )
    if down:
        return CheckResult(
            label, "fail",
            ", ".join(down)
            + " — required and stopped. Run `systemctl status <unit>`; a "
            "not-found unit means the install did not finish, so re-run "
            "install.sh.",
            reason=REASON_REQUIRED_UNIT_INACTIVE,
        )
    return CheckResult(
        label, "ok",
        f"{len(_REQUIRED_ACTIVE_UNITS)} required units are active",
    )


@doctor_check(core=True)
def check_accessory_bridges() -> CheckResult:
    """jasper-input's bridges (ADR-0225) each restart independently, so a
    bridge stuck in restart backoff never fails the unit itself —
    ``check_required_units_active`` sees jasper-input.service happily
    ``active`` while one bridge loops. ``last_error`` in the published
    status is set only while a bridge waits out its backoff, so it is the
    live "looping now" signal, not the cumulative ``restarts`` count.
    """
    label = "accessory bridges"
    snap = accessory_status.snapshot()
    if not snap["published"]:
        return CheckResult(
            label, "skipped",
            "no accessory status published — see 'required units active' "
            "if jasper-input is down",
            reason=REASON_ACCESSORY_STATUS_UNAVAILABLE,
        )
    bridges = snap["bridges"]
    if not bridges:
        return CheckResult(
            label, "skipped", "no accessory bridges configured",
            reason=REASON_ACCESSORY_BRIDGES_NOT_CONFIGURED,
        )
    looping = sorted(
        name for name, entry in bridges.items()
        if entry.get("last_error") is not None
    )
    if looping:
        return CheckResult(
            label, "warn",
            "bridges in restart backoff: " + ", ".join(
                f"{name} ({bridges[name]['last_error']})" for name in looping
            ),
            reason=REASON_ACCESSORY_BRIDGE_RESTART_LOOP,
        )
    return CheckResult(
        label, "ok", f"{len(bridges)} accessory bridges running with no restart loop",
    )


_VOICE_UNIT = JASPER_VOICE_SERVICE


@doctor_check(core=True)
def check_voice_unit_running() -> CheckResult:
    """jasper-voice is up on a box whose speaker should be able to answer.

    The gap: ``check_service_runtime_state`` counts only ``failed`` and
    stuck-mid-transition units, so an ``inactive`` jasper-voice — including
    one parked by ``RestartPreventExitStatus=66 78`` — produced no row.

    What goes silent here is the ASSISTANT; music still plays with the voice
    daemon down, so this row never claims ``speaker_silent``.

    Severity follows the tier. A full box runs an always-on wake loop, so
    ``inactive`` fails. A streambox runs the assistant only while a
    mic-bearing remote is paired (ADR-0217): with none paired the state is
    correct and reads ``skipped``, and with one paired ``inactive`` warns —
    the remote's talk button gets no answer, but the reconciler that owns
    that lifecycle may still be mid-pass.

    Three other states are not a fault on either tier: the unit's
    ``ConditionPathExists=!/var/lib/jasper/voice-input-absent`` parks it
    ``inactive`` on a box the AEC reconciler found to have neither a local
    nor an accessory mic, a box the ``/voice`` wizard has not given a
    provider parks it on ``EX_CONFIG`` under ``RestartPreventExitStatus``,
    and a unit systemd cannot load was not observed.
    """
    label = "voice daemon running"
    streambox = evidence.install_profile_is_streambox()
    if evidence.streambox_awaiting_accessory():
        return CheckResult(
            label, "skipped",
            "streambox tier with no mic-bearing remote paired — the "
            "assistant runs only while one is",
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
    # Only a file that says nothing: an unreadable or invalid one is a
    # bad read, not a box that has yet to choose, so it falls through.
    if read_active_provider_state().status in ("unset", "missing"):
        return CheckResult(
            label, "skipped",
            f"{_VOICE_UNIT} parked with no voice provider chosen yet — "
            "visit /assistant/voice/ to pick one",
            reason=REASON_VOICE_UNIT_NO_PROVIDER,
        )
    if streambox:
        return CheckResult(
            label, "warn",
            f"{_VOICE_UNIT} is inactive while a mic-bearing remote is paired, "
            "so the remote's talk button gets no answer. "
            "`journalctl -u jasper-accessory-reconcile` names why the "
            "reconciler that owns this unit's lifecycle did not start it.",
            reason=REASON_VOICE_UNIT_INACTIVE_PAIRED_REMOTE,
        )
    return CheckResult(
        label, "fail",
        f"{_VOICE_UNIT} is inactive (not failed) on the full profile, so no "
        "wake word gets an answer. `systemctl status jasper-voice` names "
        "which of the parking exits it took (66 = mic could not be opened); "
        "otherwise `systemctl start jasper-voice`.",
        reason=REASON_VOICE_UNIT_INACTIVE,
    )


def _int_field(snapshot: dict[str, Any], key: str) -> int:
    try:
        return int(snapshot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _classify_supervisor_snapshots(
    resilience: dict[str, Any], *, control_uptime_sec: float | None = None,
) -> CheckResult:
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
    if control_uptime_sec is not None and control_uptime_sec < _RESET_WINDOW_SEC:
        return CheckResult(
            "supervisor runtime snapshots", "ok",
            f"counters started {control_uptime_sec:.0f}s ago at the jasper-control "
            "restart — history before it is not visible",
            reason=REASON_SUPERVISOR_COUNTERS_RESET,
        )
    return CheckResult(
        "supervisor runtime snapshots",
        "ok",
        "supervisor snapshots quiet",
    )


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
    # jasper-control's own uptime, already in the doctor's unit-state batch.
    control_uptime_sec = unit_uptime_sec(evidence.unit_state(_CONTROL_UNIT))
    return _classify_supervisor_snapshots(
        resilience, control_uptime_sec=control_uptime_sec,
    )


# vcgencmd get_throttled bit layout (Pi firmware): raw bit 0 = under-voltage
# now, raw bit 16 = under-voltage since boot.
# jasper.control.system_metrics._read_throttled() splits the raw value into
# (throttled_now=raw & 0xF, throttled_history=(raw >> 16) & 0xF) before
# publishing it, so the published throttled_history is pre-shifted — its own
# bit 0 is raw bit 16.
_UNDER_VOLTAGE_NOW_BIT = 0x1
_UNDER_VOLTAGE_HISTORY_BIT = 0x1


def _read_system_metrics_current() -> dict[str, Any] | None:
    return evidence.system_metrics_current()


@doctor_check()
def check_supply_voltage() -> CheckResult:
    """Surface the Pi firmware's under-voltage flags (`vcgencmd
    get_throttled`, read from jasper-control's existing poller). NOW fails;
    the since-boot history bit reports `ok` — latched until reboot, so it
    cannot be acted on. Both bits stay in the detail."""
    name = "Supply voltage"
    current = _read_system_metrics_current()
    if current is None:
        metrics = _nested_dict(evidence.control_system_snapshot().payload, "metrics")
        sampled_at = metrics.get("last_sample_at") if metrics else None
        if metrics is not None:
            age = (
                "warming up" if sampled_at is None
                else f"{time.time() - sampled_at:.0f}s old"
            )
            return CheckResult(
                name, "warn", f"jasper-control sampler stale ({age})",
                reason=REASON_SUPPLY_VOLTAGE_SAMPLER_STALE,
            )
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
def check_heal_recency() -> CheckResult:
    """The heal supervisor is still ticking (ADR-0271).

    It stamps `/state.resilience.heal.last_tick` on every tick, found or not,
    so a stale value means the supervisor's coroutine is gone while
    jasper-control still answers."""
    label = "heal recency"
    resilience = _read_resilience_state()
    if resilience is None:
        return CheckResult(
            label, "skipped", "jasper-control /state unavailable",
            reason=REASON_CONTROL_UNAVAILABLE,
        )
    heal = _nested_dict(resilience, "heal") or {}
    last_tick = heal.get("last_tick")
    if not isinstance(last_tick, (int, float)) or isinstance(last_tick, bool):
        return CheckResult(
            label, "skipped",
            "jasper-control publishes no heal supervisor tick yet",
            reason=REASON_HEAL_UNOBSERVED,
        )
    age = max(0.0, time.time() - float(last_tick))
    if age > _HEAL_STALE_TICKS * _HEAL_TICK_SEC:
        return CheckResult(
            label, "warn",
            f"last heal tick {age / 60:.0f} min ago, over {_HEAL_STALE_TICKS} "
            "ticks. `journalctl -u jasper-control | grep event=heal.`",
            reason=REASON_HEAL_STALE,
        )
    would_act = heal.get("would_act")
    case = str((would_act or {}).get("case") or "")
    return CheckResult(
        label, "ok",
        f"last heal tick {age / 60:.0f} min ago"
        + (f", case={case}" if case else ", nothing to act on"),
        reason=REASON_HEAL_RECENT,
    )


@doctor_check(core=True)
def check_speaker_silence() -> CheckResult:
    """The doctor's silence lead: jasper-control's own signal-path verdict,
    the same block the /system dashboard headline renders, so the two surfaces
    cannot disagree. ``reason`` IS the published code. ``warn``, never ``fail``
    — ``speaker_silent`` leads the summary without touching severity or exit
    code (jasper/doctor_contract.py). With no verdict published the row skips
    and this run's own unit-state rows lead instead.
    """
    label = "speaker silence"
    signal_path = control_signal_path()
    code = speaker_silence_code()
    if code:
        headline = str(signal_path.get("headline") or "").strip()
        return CheckResult(
            label, "warn",
            "jasper-control reports the speaker emitting nothing: "
            f"{headline or code}",
            speaker_silent=True, reason=code,
        )
    if silence_unobserved():
        return CheckResult(
            label, "skipped",
            "jasper-control published no signal-path verdict; silence is "
            "judged from this run's own unit-state rows",
            reason=REASON_SIGNAL_PATH_UNOBSERVED,
        )
    playing = str(signal_path.get("code") or "")
    return CheckResult(
        label, "ok",
        f"jasper-control reports audio reaching the speaker ({playing})",
        reason=playing,
    )


@doctor_check(core=True)
def check_outputd_failure_reconcile_park() -> CheckResult:
    """outputd is running, and carries no park record from its stop helper.

    Why a stop can park outputd for good: see
    deploy/bin/jasper-outputd-failure-reconcile. This check owns outputd's
    runtime state (it is deliberately not in ``_RUNTIME_STATE_UNITS``), so one
    failed outputd is one fail row — including a stuck ``activating``/
    ``deactivating`` unit, which warns rather than fails: not yet silent, but
    not settled either.
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
            speaker_silent=silence_unobserved(),
            reason=REASON_OUTPUTD_PARKED,
        )
    if reason == reader.REASON_UNIT_FAILED:
        return CheckResult(
            label, "fail",
            f"{reader.UNIT} is failed with no park record — its stop helper "
            "did not judge this terminal, so systemd's Restart=on-failure "
            "should be retrying. Check `journalctl -u jasper-outputd`.",
            speaker_silent=silence_unobserved(),
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
    last_park = state.get("last_park")
    detail = f"{reader.UNIT} is running and carries no park record"
    if isinstance(last_park, dict):
        detail += f" (last park {_parked_ago(last_park.get('parked_at'))})"
    return CheckResult(label, "ok", detail)


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
