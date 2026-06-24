# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Doctor checks for the /var/lib/jasper persisted-state files.

`check_supervisor_reboot_state` (resilience) and `check_mux_mode_state`
(renderers) surface state files whose runtime readers are deliberately
fail-open — the daemons silently treat missing/corrupt as "default
behaviour", which is right at runtime but means a corrupt file or a
dropped manual pin is invisible without these doctor lines. The tests
drive the path-parameterized classifiers directly with tmp files.
"""
from __future__ import annotations

import json
import time

from jasper.cli.doctor.env import _classify_state_group_write
from jasper.cli.doctor.renderers import _classify_mux_mode
from jasper.cli.doctor.resilience import (
    _REBOOT_STATE_FUTURE_SKEW_SEC,
    _classify_reboot_state,
    _classify_supervisor_snapshots,
    check_bootloop_guard,
    check_supervisor_runtime_snapshots,
)
from jasper.music_sources import MUSIC_SOURCES


# ---- supervisor reboot state ----------------------------------------

def test_reboot_state_missing_is_ok(tmp_path):
    res = _classify_reboot_state(tmp_path / "absent.json")
    assert res.status == "ok"
    assert "no supervisor reboot recorded" in res.detail


def test_reboot_state_corrupt_warns(tmp_path):
    p = tmp_path / "reboot.json"
    p.write_text("{ not json", encoding="utf-8")
    res = _classify_reboot_state(p)
    assert res.status == "warn"
    assert "corrupt" in res.detail
    assert str(p) in res.detail  # actionable: tells the operator what to delete


def test_reboot_state_wrong_shape_warns(tmp_path):
    p = tmp_path / "reboot.json"
    p.write_text(json.dumps({"last_reboot_at": "nope"}), encoding="utf-8")
    assert _classify_reboot_state(p).status == "warn"


def test_reboot_state_recent_past_is_ok(tmp_path):
    p = tmp_path / "reboot.json"
    now = time.time()
    p.write_text(json.dumps({"last_reboot_at": now - 7200}), encoding="utf-8")
    res = _classify_reboot_state(p, now=now)
    assert res.status == "ok"
    assert "2.0h ago" in res.detail


def test_reboot_state_small_future_skew_is_ok(tmp_path):
    """fake-hwclock + NTP routinely produce small negative ages at boot;
    those must not warn."""
    p = tmp_path / "reboot.json"
    now = time.time()
    p.write_text(json.dumps({"last_reboot_at": now + 60}), encoding="utf-8")
    assert _classify_reboot_state(p, now=now).status == "ok"


def test_reboot_state_large_future_skew_warns(tmp_path):
    p = tmp_path / "reboot.json"
    now = time.time()
    p.write_text(
        json.dumps({"last_reboot_at": now + _REBOOT_STATE_FUTURE_SKEW_SEC * 2}),
        encoding="utf-8",
    )
    res = _classify_reboot_state(p, now=now)
    assert res.status == "warn"
    assert "future-dated" in res.detail


# ---- boot-loop guard marker ------------------------------------------

def _bootloop_marker(monkeypatch, tmp_path, payload) -> None:
    p = tmp_path / "bootloop-state.json"
    monkeypatch.setenv("JASPER_BOOTLOOP_MARKER_FILE", str(p))
    if payload is not None:
        p.write_text(payload, encoding="utf-8")


def test_bootloop_guard_missing_marker_is_ok_armed(monkeypatch, tmp_path):
    """No marker = guard never ran this boot (dev host, fresh install).
    Escalation is in its default armed state — not a warning."""
    _bootloop_marker(monkeypatch, tmp_path, None)
    res = check_bootloop_guard()
    assert res.status == "ok"
    assert "guard armed" in res.detail


def test_bootloop_guard_untripped_marker_is_ok_armed(monkeypatch, tmp_path):
    _bootloop_marker(monkeypatch, tmp_path, json.dumps({
        "tripped": False, "boots_in_window": 1, "threshold": 3,
        "window_sec": 3600, "checked_at": 1000, "reason": "systemd",
        "units": ["jasper-camilla.service"],
    }))
    res = check_bootloop_guard()
    assert res.status == "ok"
    assert "guard armed" in res.detail
    assert "1 boot(s)" in res.detail


def test_bootloop_guard_reload_failure_warns(monkeypatch, tmp_path):
    _bootloop_marker(monkeypatch, tmp_path, json.dumps({
        "tripped": False, "reload_ok": False, "boots_in_window": 3,
        "threshold": 3, "window_sec": 3600, "checked_at": 1000,
        "reason": "systemd", "units": ["jasper-camilla.service"],
    }))
    res = check_bootloop_guard()
    assert res.status == "warn"
    assert "daemon-reload" in res.detail
    assert "jasper-bootloop-guard --reason manual" in res.detail
    assert "run `systemctl daemon-reload`" not in res.detail
    assert "jasper-camilla.service" in res.detail


def test_bootloop_guard_tripped_warns_with_units_and_remediation(
    monkeypatch, tmp_path,
):
    _bootloop_marker(monkeypatch, tmp_path, json.dumps({
        "tripped": True, "boots_in_window": 3, "threshold": 3,
        "window_sec": 3600, "checked_at": 1000, "reason": "systemd",
        "units": ["jasper-camilla.service", "jasper-voice.service"],
    }))
    res = check_bootloop_guard()
    assert res.status == "warn"
    assert "jasper-camilla.service" in res.detail
    assert "jasper-voice.service" in res.detail
    # Remediation matches the true StartLimitAction=none semantics:
    # the sick unit parks failed; reset-failed + start recovers it.
    assert "systemctl reset-failed" in res.detail
    assert "parks failed" in res.detail


def test_bootloop_guard_corrupt_marker_is_ok_armed(monkeypatch, tmp_path):
    """The reader is fail-soft ({'ran': False}); the guard itself is
    fail-open, so a torn marker reads as 'never ran' — armed."""
    _bootloop_marker(monkeypatch, tmp_path, "{torn")
    res = check_bootloop_guard()
    assert res.status == "ok"
    assert "guard armed" in res.detail


def test_bootloop_guard_registered_in_doctor_run():
    from jasper.cli.doctor import registered_checks

    names = {c.func.__name__ for c in registered_checks()}
    assert "check_bootloop_guard" in names


# ---- supervisor runtime snapshots ------------------------------------

def test_supervisor_snapshots_quiet_is_ok():
    res = _classify_supervisor_snapshots({
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
    })
    assert res.status == "ok"
    assert "quiet" in res.detail


def test_supervisor_snapshots_warn_on_non_converging_grouping():
    res = _classify_supervisor_snapshots({
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
    })
    assert res.status == "warn"
    assert "grouping lane starved consecutive=4" in res.detail
    assert "grouping reconciler kicks=2" in res.detail
    assert "binding repair failures=1" in res.detail
    assert "connection refused" in res.detail


def test_supervisor_snapshots_check_skips_when_state_unavailable(monkeypatch):
    import jasper.cli.doctor.resilience as resilience

    monkeypatch.setattr(resilience, "_read_resilience_state", lambda: None)
    res = check_supervisor_runtime_snapshots()
    assert res.status == "ok"
    assert "unavailable" in res.detail


def test_supervisor_snapshots_check_registered_in_doctor_run():
    from jasper.cli.doctor import registered_checks

    names = {c.func.__name__ for c in registered_checks()}
    assert "check_supervisor_runtime_snapshots" in names


# ---- shared-state group-writability (env) ----------------------------

def test_state_group_write_no_files_is_ok(tmp_path):
    res = _classify_state_group_write(tmp_path / "usage.db")
    assert res.status == "ok"
    assert "no shared state files yet" in res.detail


def test_state_group_write_flags_group_unwritable(tmp_path):
    usage = tmp_path / "usage.db"
    usage.write_text("x", encoding="utf-8")
    usage.chmod(0o644)  # group can't write — the readonly-DB outage condition
    res = _classify_state_group_write(usage)
    assert res.status == "warn"
    assert "usage.db" in res.detail


def test_state_group_write_checks_conversation_history_db(
    tmp_path, monkeypatch,
):
    import grp
    import types as _types

    usage = tmp_path / "usage.db"
    history = tmp_path / "conversation_history.db"
    history.write_text("x", encoding="utf-8")
    history.chmod(0o640)  # readable by /chat, not writable by jasper-voice
    monkeypatch.setattr(
        grp, "getgrgid", lambda _gid: _types.SimpleNamespace(gr_name="jasper"),
    )

    res = _classify_state_group_write(usage)

    assert res.status == "warn"
    assert "conversation_history.db" in res.detail


def test_state_group_write_ok_when_group_jasper_and_writable(tmp_path, monkeypatch):
    import grp
    import types as _types

    usage = tmp_path / "usage.db"
    usage.write_text("x", encoding="utf-8")
    usage.chmod(0o660)  # group-writable
    # CI has no `jasper` group; pretend the file's gid resolves to it.
    monkeypatch.setattr(
        grp, "getgrgid", lambda _gid: _types.SimpleNamespace(gr_name="jasper"),
    )
    res = _classify_state_group_write(usage)
    assert res.status == "ok"
    assert "group-`jasper`-writable" in res.detail


def test_state_group_write_check_registered_in_doctor_run():
    from jasper.cli.doctor import registered_checks

    names = {c.func.__name__ for c in registered_checks()}
    assert "check_state_dir_group_writable" in names


# ---- mux mode state --------------------------------------------------

def test_mux_mode_missing_is_ok_auto(tmp_path):
    res = _classify_mux_mode(tmp_path / "absent.json")
    assert res.status == "ok"
    assert "auto" in res.detail


def test_mux_mode_corrupt_warns(tmp_path):
    p = tmp_path / "mux_mode.json"
    p.write_text("{ not json", encoding="utf-8")
    res = _classify_mux_mode(p)
    assert res.status == "warn"
    assert str(p) in res.detail


def test_mux_mode_auto_is_ok(tmp_path):
    p = tmp_path / "mux_mode.json"
    p.write_text(json.dumps({"mode": "auto"}), encoding="utf-8")
    res = _classify_mux_mode(p)
    assert res.status == "ok"
    assert "auto" in res.detail


def test_mux_mode_manual_valid_source_is_ok(tmp_path):
    source = next(iter(MUSIC_SOURCES))  # any selectable source
    p = tmp_path / "mux_mode.json"
    p.write_text(
        json.dumps({"mode": "manual", "selected_source": source.value}),
        encoding="utf-8",
    )
    res = _classify_mux_mode(p)
    assert res.status == "ok"
    assert f"manual pin: {source.value}" in res.detail


def test_mux_mode_manual_unknown_source_warns(tmp_path):
    p = tmp_path / "mux_mode.json"
    p.write_text(
        json.dumps({"mode": "manual", "selected_source": "betamax"}),
        encoding="utf-8",
    )
    res = _classify_mux_mode(p)
    assert res.status == "warn"
    assert "betamax" in res.detail
