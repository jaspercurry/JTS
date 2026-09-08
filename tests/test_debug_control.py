# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.control.debug_control — the write/restart/expiry side
of the debug toggle. The real systemctl restart and the threading.Timer
are stubbed so the logic is exercised without side effects.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from jasper import debug_mode
from jasper.control import debug_control
from jasper.env_file import read_env_file

from tests.control_server_fixtures import (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    _post,
    server_with_coordinator,
)

_IMPORTED_FIXTURES = (
    _explicit_passive_output_topology,
    _isolate_household_secret,
    server_with_coordinator,
)

NOW = 1_000_000.0
TTL = debug_mode.DEFAULT_TTL_SEC


class _FakeTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def dc(tmp_path, monkeypatch):
    """Isolate debug_control: temp env file, recorded restarts, stubbed
    timer factory, restored 'jasper' logger level + timer global."""
    path = str(tmp_path / "debug.env")
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", path)
    restarts: list[str] = []
    active_checks: list[str] = []
    active_units: set[str] = set()
    scheduled: list[tuple[float, object, _FakeTimer]] = []
    monkeypatch.setattr(debug_control, "_restart_unit", lambda u: restarts.append(u))
    monkeypatch.setattr(
        debug_control,
        "_unit_is_active",
        lambda u: active_checks.append(u) or u in active_units,
    )

    def fake_schedule(delay, fn):
        t = _FakeTimer()
        scheduled.append((delay, fn, t))
        return t

    monkeypatch.setattr(debug_control, "_schedule", fake_schedule)
    monkeypatch.setattr(debug_control, "_timer", None, raising=False)
    # control's toggle now routes through debug_mode.apply_for, which arms a
    # per-process self-quiet timer — stub the factory so tests don't spawn one.
    monkeypatch.setattr(
        debug_mode, "_make_timer",
        lambda d, fn: SimpleNamespace(start=lambda: None, cancel=lambda: None),
    )
    monkeypatch.setattr(debug_mode, "_self_quiet_timer", None, raising=False)
    lg = logging.getLogger("jasper")
    before = lg.level
    yield SimpleNamespace(
        path=path,
        restarts=restarts,
        active_checks=active_checks,
        active_units=active_units,
        scheduled=scheduled,
    )
    lg.setLevel(before)
    debug_control._timer = None


def _env(path) -> dict[str, str]:
    return read_env_file(path)


# --------------------------------------------------------------- set_debug


def test_set_debug_voice_writes_restarts_and_arms(dc):
    st, _restart_result = debug_control.set_debug("voice", True, now=NOW)
    env = _env(dc.path)
    assert env["JASPER_DEBUG_VOICE"] == "1"
    assert env[debug_mode.EXPIRES_KEY] == str(int(NOW + TTL))
    assert "voice" in st.active
    assert dc.restarts == ["jasper-voice.service"]
    assert len(dc.scheduled) == 1
    assert dc.scheduled[0][0] == pytest.approx(TTL)


def test_set_debug_control_is_inprocess_no_restart(dc):
    logging.getLogger("jasper").setLevel(logging.INFO)
    debug_control.set_debug("control", True, now=NOW)
    assert logging.getLogger("jasper").level == logging.DEBUG
    assert dc.restarts == []          # control never restarts itself
    assert len(dc.scheduled) == 1     # but expiry is still armed


def test_set_debug_disable_last_clears_expiry_and_cancels_timer(dc):
    debug_control.set_debug("voice", True, now=NOW)
    first_timer = dc.scheduled[0][2]
    debug_control.set_debug("voice", False, now=NOW)
    env = _env(dc.path)
    assert env["JASPER_DEBUG_VOICE"] == "0"
    assert env[debug_mode.EXPIRES_KEY] == ""
    assert first_timer.cancelled is True  # old timer cancelled on re-arm


def test_set_debug_unknown_raises(dc):
    with pytest.raises(ValueError):
        debug_control.set_debug("bogus", True, now=NOW)


# ---------------------------------------------------------------- snapshot


def test_snapshot_reflects_active_then_expired(dc):
    debug_control.set_debug("aec", True, now=NOW)
    snap = debug_control.snapshot(now=NOW)
    aec = next(s for s in snap["subsystems"] if s["id"] == "aec")
    assert aec["enabled"] is True
    assert aec["apply_policy"] == "restart"
    assert snap["any_active"] is True
    assert snap["remaining_sec"] == pytest.approx(TTL, abs=1)
    # past expiry → reads as off even though the flag is still on disk
    later = debug_control.snapshot(now=NOW + TTL + 1)
    aec2 = next(s for s in later["subsystems"] if s["id"] == "aec")
    assert aec2["enabled"] is False
    assert later["any_active"] is False


# ------------------------------------------------------------- reconcile


def test_reconcile_expired_clears_file(dc):
    debug_control._atomic_write(
        {"JASPER_DEBUG_VOICE": "1", debug_mode.EXPIRES_KEY: str(int(NOW - 1))}
    )
    debug_control.reconcile_on_startup(now=NOW)
    env = _env(dc.path)
    assert env["JASPER_DEBUG_VOICE"] == "0"
    assert env[debug_mode.EXPIRES_KEY] == ""


def test_reconcile_active_rearms_timer(dc):
    debug_control._atomic_write(
        {"JASPER_DEBUG_VOICE": "1", debug_mode.EXPIRES_KEY: str(int(NOW + 600))}
    )
    debug_control.reconcile_on_startup(now=NOW)
    assert len(dc.scheduled) == 1
    assert dc.scheduled[0][0] == pytest.approx(600, abs=1)


def test_reconcile_empty_file_is_noop(dc):
    debug_control.reconcile_on_startup(now=NOW)  # no file at all
    assert dc.scheduled == []


# ------------------------------------------------------------- expiry fire


def test_on_expiry_clears_flags_without_restart(dc):
    # Daemons self-quiet in-process (debug_mode self-quiet timer); the
    # control-side expiry only clears the debug.env SSOT — no restart.
    debug_control.set_debug("voice", True, now=NOW)
    dc.restarts.clear()
    debug_control._on_expiry()
    env = _env(dc.path)
    assert env["JASPER_DEBUG_VOICE"] == "0"
    assert env[debug_mode.EXPIRES_KEY] == ""
    assert dc.restarts == []  # the daemon quiets itself; no restart on expiry


# -------------------------------------------------------- broker plumbing


@pytest.mark.parametrize(
    "broker_ok, expected_status",
    [(False, 502), (True, 202)],
)
def test_post_debug_answers_through_the_broker_result_helper(
    monkeypatch, tmp_path, server_with_coordinator, broker_ok, expected_status,
):
    """POST /debug's restart now goes through the same broker envelope as
    every other --no-block action: refused is 502 with a code, ok is 202 —
    never the old bare-Popen claim that the restart already happened."""
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(tmp_path / "debug.env"))
    monkeypatch.setattr(debug_control, "_arm_expiry_locked", lambda *a, **k: None)
    monkeypatch.setattr(
        debug_control.restart_broker,
        "manage_units",
        lambda *units, **kwargs: {"ok": broker_ok, "units": list(units)},
    )

    base, _fake = server_with_coordinator
    status, body = _post(f"{base}/debug", {"subsystem": "voice", "enabled": True})

    assert status == expected_status
    if broker_ok:
        assert body["subsystems"]
    else:
        assert body["code"] == "debug_restart_failed"
        assert body.get("ok") is not True
        # The flag is already in debug.env, so the refusal must say so —
        # otherwise the card cannot tell "not applied" from "not saved".
        assert body["intent_saved"] is True
