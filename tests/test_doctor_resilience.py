# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor resilience domain.

These checks surface state whose runtime readers are deliberately fail-open:
the daemons treat missing/corrupt as "default behaviour", which is right at
runtime but leaves a corrupt file or a parked unit invisible without a doctor
line. The tests drive the path-parameterized classifiers with tmp files.
"""
from __future__ import annotations

import json
import time

import pytest

from jasper import service_units
from jasper.cli.doctor import _evidence, _shared, resilience
from jasper.cli.doctor.resilience import (
    _REBOOT_STATE_FUTURE_SKEW_SEC,
    _classify_reboot_state,
    _classify_supervisor_snapshots,
    check_bootloop_guard,
    check_supervisor_runtime_snapshots,
    check_supply_voltage,
)

from .doctor_test_support import (
    _make_unit_states_fake,
    _registered_check_names,
)

# ------------------------------------------------- check_service_runtime_state


def _systemctl_show(monkeypatch, stdout: str):
    """Seed the evidence layer's unit-state batch from raw ``systemctl show``
    block text, reusing the real parser for fidelity."""
    parsed = service_units.parse_systemctl_show_units(stdout)
    monkeypatch.setattr(
        _evidence, "read_unit_states", lambda units, *, timeout: parsed,
    )


def _unit_block(unit: str, active: str, sub: str, restarts: int = 0) -> str:
    return (
        f"Id={unit}\n"
        "LoadState=loaded\n"
        f"ActiveState={active}\n"
        f"SubState={sub}\n"
        "Result=success\n"
        f"NRestarts={restarts}\n"
    )


@pytest.mark.parametrize(
    "blocks, status, reason",
    [
        (
            [("librespot.service", "failed", "failed", 5)],
            "fail",
            resilience.REASON_UNITS_FAILED_OR_UNSTABLE,
        ),
        (
            [("jasper-voice.service", "active", "running", 2)],
            "warn",
            resilience.REASON_UNITS_RESTARTED,
        ),
        # A parked coupling oneshot leaves its evidence only in
        # `systemctl --failed` plus the journal (#1233 follow-up).
        (
            [("jasper-fanin-coupling-auto.service", "failed", "failed", 0)],
            "fail",
            resilience.REASON_UNITS_FAILED_OR_UNSTABLE,
        ),
    ],
    ids=["failed-unit", "restart-count", "failed-oneshot"],
)
def test_check_service_runtime_state_verdicts(
    monkeypatch, blocks, status, reason
):
    _systemctl_show(monkeypatch, "\n".join(_unit_block(*b) for b in blocks))

    r = resilience.check_service_runtime_state()

    assert r.status == status
    assert r.reason == reason


def test_check_service_runtime_state_ignores_an_in_flight_oneshot(monkeypatch):
    """`activating` is a oneshot's NORMAL mid-run state (a reconcile pass in
    flight), not the stuck-start instability it signals on a long-running
    daemon — a tick the doctor races must not read as a failure."""
    _systemctl_show(
        monkeypatch,
        _unit_block("jasper-fanin-coupling-auto.service", "activating", "start"),
    )

    r = resilience.check_service_runtime_state()

    assert r.status == "ok"


def test_check_service_runtime_state_flags_a_non_oneshot_stuck_activating(
    monkeypatch,
):
    """The same `activating` state on a long-running daemon still is a
    finding — only the tracked oneshot is exempt."""
    _systemctl_show(
        monkeypatch, _unit_block("jasper-fanin.service", "activating", "start"),
    )

    r = resilience.check_service_runtime_state()

    assert r.status == "fail"
    assert r.reason == resilience.REASON_UNITS_FAILED_OR_UNSTABLE


def test_runtime_state_units_track_the_coupling_reconciler_oneshot():
    assert "jasper-fanin-coupling-auto.service" in _shared._RUNTIME_STATE_UNITS


# ------------------------------------------------------- supervisor reboot state


@pytest.mark.parametrize(
    "payload, offset, status, reason",
    [
        (None, None, "ok", resilience.REASON_REBOOT_STATE_ABSENT),
        # A corrupt file must name itself so the operator knows what to delete.
        ("{ not json", None, "warn", resilience.REASON_REBOOT_STATE_CORRUPT),
        (
            json.dumps({"last_reboot_at": "nope"}), None, "warn",
            resilience.REASON_REBOOT_STATE_CORRUPT,
        ),
        (None, -7200, "ok", resilience.REASON_REBOOT_STATE_ARMED),
        # fake-hwclock + NTP routinely produce small negative ages at boot.
        (None, 60, "ok", resilience.REASON_REBOOT_STATE_ARMED),
        (
            None, _REBOOT_STATE_FUTURE_SKEW_SEC * 2, "warn",
            resilience.REASON_REBOOT_STATE_FUTURE_DATED,
        ),
    ],
    ids=["absent", "corrupt", "wrong-shape", "recent", "small-skew", "large-skew"],
)
def test_classify_reboot_state_verdicts(
    tmp_path, payload, offset, status, reason
):
    p = tmp_path / "reboot.json"
    now = time.time()
    if offset is not None:
        p.write_text(json.dumps({"last_reboot_at": now + offset}), encoding="utf-8")
    elif payload is not None:
        p.write_text(payload, encoding="utf-8")

    res = _classify_reboot_state(p, now=now)

    assert res.status == status
    assert res.reason == reason


# ---------------------------------------------------------- boot-loop guard


def _bootloop_marker(monkeypatch, tmp_path, payload) -> None:
    p = tmp_path / "bootloop-state.json"
    monkeypatch.setenv("JASPER_BOOTLOOP_MARKER_FILE", str(p))
    if payload is not None:
        p.write_text(payload, encoding="utf-8")


_ARMED = {
    "tripped": False,
    "boots_in_window": 1,
    "threshold": 3,
    "window_sec": 3600,
    "checked_at": 1000,
    "reason": "systemd",
    "units": ["jasper-camilla.service"],
}


@pytest.mark.parametrize(
    "payload, reason",
    [
        # guard never ran this boot (dev host, fresh install)
        (None, resilience.REASON_BOOTLOOP_GUARD_NOT_RUN),
        (json.dumps(_ARMED), resilience.REASON_BOOTLOOP_GUARD_ARMED),
        # The reader is fail-soft ({'ran': False}) and the guard is fail-open,
        # so a torn marker reads as "never ran" — armed, not broken.
        ("{torn", resilience.REASON_BOOTLOOP_GUARD_NOT_RUN),
    ],
    ids=["absent", "untripped", "corrupt"],
)
def test_bootloop_guard_reports_armed(monkeypatch, tmp_path, payload, reason):
    _bootloop_marker(monkeypatch, tmp_path, payload)

    res = check_bootloop_guard()

    assert res.status == "ok"
    assert res.reason == reason


def test_bootloop_guard_warns_on_a_reload_failure(monkeypatch, tmp_path):
    _bootloop_marker(
        monkeypatch,
        tmp_path,
        json.dumps({**_ARMED, "reload_ok": False, "boots_in_window": 3}),
    )

    res = check_bootloop_guard()

    assert res.status == "warn"
    assert res.reason == resilience.REASON_BOOTLOOP_GUARD_RELOAD_FAILED


def test_bootloop_guard_tripped_names_the_units_and_the_recovery(
    monkeypatch, tmp_path
):
    """StartLimitAction=none parks the sick unit failed; reset-failed + start
    is what actually recovers it."""
    _bootloop_marker(
        monkeypatch,
        tmp_path,
        json.dumps(
            {
                **_ARMED,
                "tripped": True,
                "boots_in_window": 3,
                "units": ["jasper-camilla.service", "jasper-voice.service"],
            }
        ),
    )

    res = check_bootloop_guard()

    assert res.status == "warn"
    assert res.reason == resilience.REASON_BOOTLOOP_GUARD_TRIPPED


# ------------------------------------------------- supervisor runtime snapshots


def test_supervisor_snapshots_quiet_is_ok():
    res = _classify_supervisor_snapshots(
        {
            "shairport": {"enabled": True, "consecutive_failures": 0},
            "grouping_supervisor": {
                "enabled": True,
                "last_poll_starved": False,
                "consecutive_starved": 0,
                "kick_count": 0,
                "rate_limited_count": 0,
                "binding": {"failed_total": 0},
                "reassert": {"failed_total": 0, "last_ok": True},
            },
            "system_supervisor": {"enabled": True, "consecutive_failures": 0},
        }
    )

    assert res.status == "ok"


@pytest.mark.parametrize(
    "grouping_supervisor",
    [
        {"enabled": True, "last_poll_starved": True, "consecutive_starved": 4},
        {"enabled": True, "kick_count": 2},
        {"enabled": True, "binding": {"failed_total": 1}},
        {
            "enabled": True,
            "reassert": {
                "failed_total": 1,
                "last_ok": False,
                "last_detail": "connection refused",
            },
        },
    ],
    ids=["starved", "kicks", "binding-failed", "reassert-failed"],
)
def test_supervisor_snapshots_warn_on_every_non_converging_signal(
    grouping_supervisor,
):
    res = _classify_supervisor_snapshots(
        {"grouping_supervisor": grouping_supervisor},
    )

    assert res.status == "warn"
    assert res.reason == resilience.REASON_SUPERVISOR_ISSUES


def test_supervisor_snapshots_check_skips_when_state_unavailable(monkeypatch):
    monkeypatch.setattr(resilience, "_read_resilience_state", lambda: None)

    res = check_supervisor_runtime_snapshots()

    assert res.status == "skipped"
    assert res.reason == resilience.REASON_CONTROL_UNAVAILABLE


# ------------------------------------------------------- check_supply_voltage


@pytest.mark.parametrize(
    "current, status, reason",
    [
        # No jasper-control /system/snapshot reachable: n/a, not a failure.
        (None, "skipped", resilience.REASON_SNAPSHOT_UNAVAILABLE),
        # Bits absent/wrong-typed from a stale or malformed snapshot: n/a.
        (
            {"throttled_now": None, "throttled_history": None}, "skipped",
            resilience.REASON_THROTTLED_BITS_UNREPORTED,
        ),
        # Clean box: neither bit set. Both fields are already the shifted
        # nibbles jasper.control.system_metrics._read_throttled() publishes
        # (raw & 0xF, (raw >> 16) & 0xF) -- never a raw 0x50005-style value.
        ({"throttled_now": 0x0, "throttled_history": 0x0}, "ok", ""),
        # Bit 0 of throttled_now: under-voltage right now outranks history.
        (
            {"throttled_now": 0x5, "throttled_history": 0x5},
            "fail",
            resilience.REASON_UNDERVOLTAGE_NOW,
        ),
        # Bit 0 of throttled_history only (raw bit 16): happened since boot,
        # not now.
        (
            {"throttled_now": 0x0, "throttled_history": 0x1},
            "warn",
            resilience.REASON_UNDERVOLTAGE_HISTORY,
        ),
        # Other throttled bits set (frequency cap, temp limit) but neither
        # under-voltage bit: not this check's concern.
        ({"throttled_now": 0x2, "throttled_history": 0x2}, "ok", ""),
    ],
)
def test_check_supply_voltage_verdicts(monkeypatch, current, status, reason):
    monkeypatch.setattr(resilience, "_read_system_metrics_current", lambda: current)

    result = check_supply_voltage()

    assert result.status == status
    if reason:
        assert result.reason == reason


@pytest.mark.parametrize(
    "check_name",
    [
        "check_bootloop_guard",
        "check_supervisor_runtime_snapshots",
        "check_supply_voltage",
    ],
)
def test_resilience_checks_are_registered(check_name):
    assert check_name in _registered_check_names()


# --------------------------------------- check_outputd_failure_reconcile_park


@pytest.mark.parametrize(
    "stamp, unit_active, status, reason, silent",
    [
        (None, "active", "skipped",
         resilience.REASON_OUTPUTD_RECONCILE_UNOBSERVED, False),
        ("absent", "active", "ok",
         resilience.REASON_OUTPUTD_RECONCILE_NONE, False),
        ("garbage", "active", "warn",
         resilience.REASON_OUTPUTD_RECONCILE_UNINTELLIGIBLE, False),
        ("epoch", "active", "ok",
         resilience.REASON_OUTPUTD_RECONCILED, False),
        ("epoch", "failed", "fail",
         resilience.REASON_OUTPUTD_PARKED, True),
    ],
    ids=["no-runtime-dir", "no-reconcile", "truncated", "recovered", "parked"],
)
def test_outputd_failure_reconcile_park_branches(
    tmp_path, monkeypatch, stamp, unit_active, status, reason, silent,
):
    """outputd owns the DAC write loop, so the park branch is the one result
    here that proves silence."""
    if stamp is None:
        target = tmp_path / "gone" / "failure-reconcile.stamp"
    else:
        target = tmp_path / "failure-reconcile.stamp"
        if stamp == "garbage":
            target.write_text("truncated")
        elif stamp == "epoch":
            target.write_text(str(int(time.time()) - 10))
    monkeypatch.setenv("JASPER_OUTPUTD_CONFIG_RETRY_STATE", str(target))
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _make_unit_states_fake({"jasper-outputd.service": {
            "active_state": unit_active, "result": "exit-code",
        }}),
    )
    result = resilience.check_outputd_failure_reconcile_park()
    assert (result.status, result.reason) == (status, reason)
    assert result.speaker_silent is silent


def test_outputd_failure_reconcile_park_skips_without_systemctl(
    tmp_path, monkeypatch,
):
    stamp = tmp_path / "failure-reconcile.stamp"
    stamp.write_text(str(int(time.time())))
    monkeypatch.setenv("JASPER_OUTPUTD_CONFIG_RETRY_STATE", str(stamp))
    monkeypatch.setattr(
        _evidence, "read_unit_states", _make_unit_states_fake(unavailable=True),
    )
    result = resilience.check_outputd_failure_reconcile_park()
    assert result.status == "skipped"
    assert result.reason == resilience.REASON_OUTPUTD_RECONCILE_UNOBSERVED


def test_outputd_failure_reconcile_park_is_registered():
    assert "check_outputd_failure_reconcile_park" in _registered_check_names()
