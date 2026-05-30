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
from jasper.web._common import read_env_file

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
    scheduled: list[tuple[float, object, _FakeTimer]] = []
    monkeypatch.setattr(debug_control, "_restart_unit", lambda u: restarts.append(u))

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
    yield SimpleNamespace(path=path, restarts=restarts, scheduled=scheduled)
    lg.setLevel(before)
    debug_control._timer = None


def _env(path) -> dict[str, str]:
    return read_env_file(path)


# --------------------------------------------------------------- set_debug


def test_set_debug_voice_writes_restarts_and_arms(dc):
    st = debug_control.set_debug("voice", True, now=NOW)
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
    # voice/aec self-quiet in-process (debug_mode self-quiet timer); the
    # control-side expiry only clears the debug.env SSOT — no restart.
    debug_control.set_debug("voice", True, now=NOW)
    dc.restarts.clear()
    debug_control._on_expiry()
    env = _env(dc.path)
    assert env["JASPER_DEBUG_VOICE"] == "0"
    assert env[debug_mode.EXPIRES_KEY] == ""
    assert dc.restarts == []  # the daemon quiets itself; no restart on expiry
