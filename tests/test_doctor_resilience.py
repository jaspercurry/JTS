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

from jasper.cli import doctor
from jasper.cli.doctor import resilience
from jasper.cli.doctor.resilience import (
    _REBOOT_STATE_FUTURE_SKEW_SEC,
    _classify_reboot_state,
    _classify_supervisor_snapshots,
    check_bootloop_guard,
    check_supervisor_runtime_snapshots,
)

from .doctor_test_support import _registered_check_names

# ------------------------------------------------- check_service_runtime_state


def _systemctl_show(monkeypatch, stdout: str):
    monkeypatch.setattr(
        doctor._shared, "_run", lambda *a, **kw: type("R", (), {"stdout": stdout})()
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
    "blocks, status, must_name",
    [
        (
            [("librespot.service", "failed", "failed", 5)],
            "fail",
            "librespot.service state=failed/failed",
        ),
        (
            [("jasper-voice.service", "active", "running", 2)],
            "warn",
            "jasper-voice.service NRestarts=2",
        ),
        # A parked coupling oneshot leaves its evidence only in
        # `systemctl --failed` plus the journal (#1233 follow-up).
        (
            [("jasper-fanin-coupling-auto.service", "failed", "failed", 0)],
            "fail",
            "jasper-fanin-coupling-auto.service state=failed/failed",
        ),
    ],
    ids=["failed-unit", "restart-count", "failed-oneshot"],
)
def test_check_service_runtime_state_verdicts(
    monkeypatch, blocks, status, must_name
):
    _systemctl_show(monkeypatch, "\n".join(_unit_block(*b) for b in blocks))

    r = doctor.check_service_runtime_state()

    assert r.status == status
    assert must_name in r.detail


def test_check_service_runtime_state_ignores_an_in_flight_oneshot(monkeypatch):
    """`activating` is a oneshot's NORMAL mid-run state (a reconcile pass in
    flight), not the stuck-start instability it signals on a long-running
    daemon — a tick the doctor races must not read as a failure, while a daemon
    stuck in activating still does."""
    _systemctl_show(
        monkeypatch,
        _unit_block("jasper-fanin-coupling-auto.service", "activating", "start")
        + "\n"
        + _unit_block("jasper-fanin.service", "activating", "start"),
    )

    r = doctor.check_service_runtime_state()

    assert r.status == "fail"
    assert "jasper-fanin.service state=activating/start" in r.detail
    assert "jasper-fanin-coupling-auto.service" not in r.detail


def test_runtime_state_units_track_the_coupling_reconciler_oneshot():
    assert "jasper-fanin-coupling-auto.service" in doctor._RUNTIME_STATE_UNITS


# ------------------------------------------------------- supervisor reboot state


@pytest.mark.parametrize(
    "payload, offset, status, must_name",
    [
        (None, None, "ok", ""),
        # A corrupt file must name itself so the operator knows what to delete.
        ("{ not json", None, "warn", "reboot.json"),
        (json.dumps({"last_reboot_at": "nope"}), None, "warn", ""),
        (None, -7200, "ok", "2.0h ago"),
        # fake-hwclock + NTP routinely produce small negative ages at boot.
        (None, 60, "ok", ""),
        (None, _REBOOT_STATE_FUTURE_SKEW_SEC * 2, "warn", "future-dated"),
    ],
    ids=["absent", "corrupt", "wrong-shape", "recent", "small-skew", "large-skew"],
)
def test_classify_reboot_state_verdicts(
    tmp_path, payload, offset, status, must_name
):
    p = tmp_path / "reboot.json"
    now = time.time()
    if offset is not None:
        p.write_text(json.dumps({"last_reboot_at": now + offset}), encoding="utf-8")
    elif payload is not None:
        p.write_text(payload, encoding="utf-8")

    res = _classify_reboot_state(p, now=now)

    assert res.status == status
    assert must_name in res.detail


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
    "payload",
    [
        None,  # guard never ran this boot (dev host, fresh install)
        json.dumps(_ARMED),
        # The reader is fail-soft ({'ran': False}) and the guard is fail-open,
        # so a torn marker reads as "never ran" — armed, not broken.
        "{torn",
    ],
    ids=["absent", "untripped", "corrupt"],
)
def test_bootloop_guard_reports_armed(monkeypatch, tmp_path, payload):
    _bootloop_marker(monkeypatch, tmp_path, payload)

    res = check_bootloop_guard()

    assert res.status == "ok"
    assert "guard armed" in res.detail


def test_bootloop_guard_warns_on_a_reload_failure(monkeypatch, tmp_path):
    _bootloop_marker(
        monkeypatch,
        tmp_path,
        json.dumps({**_ARMED, "reload_ok": False, "boots_in_window": 3}),
    )

    res = check_bootloop_guard()

    assert res.status == "warn"
    assert "jasper-bootloop-guard --reason manual" in res.detail
    assert "jasper-camilla.service" in res.detail


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
    assert "jasper-camilla.service" in res.detail
    assert "jasper-voice.service" in res.detail
    assert "systemctl reset-failed" in res.detail


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


def test_supervisor_snapshots_warn_names_every_non_converging_signal():
    res = _classify_supervisor_snapshots(
        {
            "grouping_supervisor": {
                "enabled": True,
                "last_poll_starved": True,
                "consecutive_starved": 4,
                "kick_count": 2,
                "rate_limited_count": 1,
                "binding": {"failed_total": 1},
                "reassert": {
                    "failed_total": 1,
                    "last_ok": False,
                    "last_detail": "connection refused",
                },
            },
        }
    )

    assert res.status == "warn"
    assert "grouping lane starved consecutive=4" in res.detail
    assert "grouping reconciler kicks=2" in res.detail
    assert "binding repair failures=1" in res.detail
    assert "connection refused" in res.detail


def test_supervisor_snapshots_check_skips_when_state_unavailable(monkeypatch):
    monkeypatch.setattr(resilience, "_read_resilience_state", lambda: None)

    assert check_supervisor_runtime_snapshots().status == "ok"


@pytest.mark.parametrize(
    "check_name",
    ["check_bootloop_guard", "check_supervisor_runtime_snapshots"],
)
def test_resilience_checks_are_registered(check_name):
    assert check_name in _registered_check_names()
