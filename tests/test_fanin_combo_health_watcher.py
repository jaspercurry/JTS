# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Watcher-orchestration + CLI tests for the USB-combo runtime fallback
(``jasper.fanin.coupling_reconcile.run_health_check`` + the ``--health`` /
``--auto`` marker lifecycle, defect 2026-07-10). Hardware-free: fan-in STATUS and
the reconcile disarm are injected."""

from __future__ import annotations

import pytest

from jasper.fanin import combo_health as ch
from jasper.fanin import coupling_reconcile as cr


@pytest.fixture(autouse=True)
def _entry_lock_in_tmp(tmp_path, monkeypatch):
    """Keep every main() invocation's entry flock inside the test tmp dir —
    never the real /run path — so parallel test workers can't contend."""
    monkeypatch.setattr(cr, "ENTRY_LOCK_PATH", str(tmp_path / "entry.lock"))


def _status(*, source="direct", health="capturing", reopens=0, card_gen_reopens=0):
    entry = {"label": "usbsink", "source": source, "frames_read": 100}
    if source == "direct":
        entry["direct"] = {
            "present": True,
            "health": health,
            "reopens": reopens,
            "card_gen_reopens": card_gen_reopens,
        }
    return {"inputs": [entry]}


def _fake_auto(ok=True):
    return cr.AutoResult(
        ok=ok, owned=True, coupling="loopback", gadget_present=True,
        usb_combo_changed=True, reason="", combo_armed=False, fallback_active=True,
    )


def _run(tmp_path, status, *, reconcile=None, withdraw=None):
    calls = {"reconcile": 0, "withdraw": 0, "order": []}

    def _withdraw():
        calls["withdraw"] += 1
        calls["order"].append("withdraw")
        return (withdraw or (lambda: (True, "")))()

    def _reconcile():
        calls["reconcile"] += 1
        calls["order"].append("reconcile")
        return (reconcile or _fake_auto)()

    res = cr.run_health_check(
        reason="t",
        tick_state_path=str(tmp_path / "tick.json"),
        marker_path=str(tmp_path / "fallback.json"),
        read_fanin_status=lambda: (status, ""),
        run_reconcile=_reconcile,
        withdraw_usb_audio=_withdraw,
        source_intent_env_path=str(tmp_path / "source_intent.env"),
    )
    return res, calls


def test_no_direct_lane_is_silent_noop(tmp_path):
    res, calls = _run(tmp_path, _status(source="lane"))
    assert res.watched is False
    assert res.ok is True
    assert calls["reconcile"] == 0
    # No marker written.
    assert ch.fallback_active(str(tmp_path / "fallback.json")) is False


def test_healthy_tick_is_quiet_no_disarm(tmp_path):
    res, calls = _run(tmp_path, _status(health="capturing"))
    assert res.watched is True
    assert res.broken is False
    assert res.disarmed is False
    assert res.transition == ""
    assert calls["reconcile"] == 0


def test_sustained_broken_disarms_and_writes_marker(tmp_path):
    # Tick 1: broken via health=broken (first broken; no disarm yet).
    res1, calls1 = _run(tmp_path, _status(health="broken"))
    assert res1.broken is True
    assert res1.disarmed is False
    assert res1.consecutive_broken == 1
    assert calls1["reconcile"] == 0
    # marker NOT yet written on the first broken tick.
    assert ch.fallback_active(str(tmp_path / "fallback.json")) is False
    # Tick 2: still broken -> sustained -> disarm + marker + reconcile.
    res2, calls2 = _run(tmp_path, _status(health="broken"))
    assert res2.disarmed is True
    assert res2.ok is True
    assert calls2["reconcile"] == 1
    assert calls2["withdraw"] == 1
    assert calls2["order"] == ["withdraw", "reconcile"]
    marker = ch.read_fallback_marker(str(tmp_path / "fallback.json"))
    assert marker is not None
    assert "consecutive" in marker.reason
    # Tick state reset after disarm so a residual can't immediately re-fire.
    assert ch.read_tick_state(str(tmp_path / "tick.json")).consecutive_broken == 0


def test_capturing_reopen_churn_disarms_across_two_ticks(tmp_path):
    # Two consecutive ticks with the zombie reopen counter climbing WHILE the lane
    # is actively capturing = a sustained real break of a live stream -> disarm.
    _run(tmp_path, _status(health="capturing", reopens=1))  # baseline (no prev)
    r2, _ = _run(tmp_path, _status(health="capturing", reopens=2))  # churn -> broken 1
    assert r2.broken is True and r2.disarmed is False
    r3, calls = _run(tmp_path, _status(health="capturing", reopens=3))  # churn -> disarm
    assert r3.disarmed is True
    assert calls["reconcile"] == 1


def test_idle_reopen_churn_never_disarms(tmp_path):
    # Defect 2026-07-11 regression: replays the exact jts.local false-positive
    # shape — an IDLE box (health=idle throughout: a silence-streaming Mac + routine
    # UAC2 re-enumeration self-heal) whose reopen counters churn upward tick after
    # tick. The pre-fix watcher disarmed after 2 such ticks; the binding invariant
    # is that an idle host must NEVER trip the fallback, so NONE of these disarm.
    marker = str(tmp_path / "fallback.json")
    _run(tmp_path, _status(health="idle", reopens=0, card_gen_reopens=1))  # baseline
    # liveness-probe card_gen climbs 1->2 (the 19:11 disarm cause) — must NOT break.
    r, calls = _run(tmp_path, _status(health="idle", reopens=0, card_gen_reopens=2))
    assert r.broken is False and r.disarmed is False
    # zombie reopens climb repeatedly (the 07:48 disarm cause) — must NOT break.
    for n in (3, 5, 7, 9):
        r, calls = _run(tmp_path, _status(health="idle", reopens=n, card_gen_reopens=2))
        assert r.broken is False and r.disarmed is False
    assert calls["reconcile"] == 0
    # No marker ever written — USB audio stays available on the idle box.
    assert ch.fallback_active(marker) is False


def test_disarm_failure_surfaces_not_ok(tmp_path):
    _run(tmp_path, _status(health="broken"))  # tick 1
    res, _ = _run(tmp_path, _status(health="broken"), reconcile=lambda: _fake_auto(ok=False))
    assert res.disarmed is True
    assert res.ok is False


def test_uac2_withdraw_failure_keeps_direct_consumer_armed_for_retry(tmp_path):
    _run(tmp_path, _status(health="broken"))
    res, calls = _run(
        tmp_path,
        _status(health="broken"),
        withdraw=lambda: (False, "gadget restart failed"),
    )

    assert res.ok is False
    assert res.disarmed is False
    assert calls["withdraw"] == 1
    assert calls["reconcile"] == 0
    assert "UAC2 withdrawal failed" in res.detail
    assert ch.fallback_active(str(tmp_path / "fallback.json")) is True


def test_read_error_is_silent_noop(tmp_path):
    # fan-in socket down -> None status -> not watched, silent, no marker.
    res = cr.run_health_check(
        reason="t",
        tick_state_path=str(tmp_path / "tick.json"),
        marker_path=str(tmp_path / "fallback.json"),
        read_fanin_status=lambda: (None, "connection refused"),
        run_reconcile=lambda: _fake_auto(),
        source_intent_env_path=str(tmp_path / "source_intent.env"),
    )
    assert res.watched is False
    assert res.ok is True


def test_health_body_timeout_is_not_misreported_as_source_lock_contention(tmp_path):
    def _timed_out_status_read():
        raise TimeoutError("fan-in status deadline")

    with pytest.raises(TimeoutError, match="fan-in status deadline"):
        cr.run_health_check(
            reason="t",
            tick_state_path=str(tmp_path / "tick.json"),
            marker_path=str(tmp_path / "fallback.json"),
            read_fanin_status=_timed_out_status_read,
            source_intent_env_path=str(tmp_path / "source_intent.env"),
        )


def test_health_lock_order_is_source_then_coupling_and_reverse_on_release(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from jasper import source_intent

    events = []

    @contextmanager
    def _source_lock(**_kwargs):
        events.append("source_acquired")
        try:
            yield
        finally:
            events.append("source_released")

    class _CouplingLockHandle:
        def close(self):
            events.append("coupling_released")

    def _coupling_lock(*_args, **_kwargs):
        assert events == ["source_acquired"]
        events.append("coupling_acquired")
        return cr.EntryLock(outcome="acquired", fh=_CouplingLockHandle())

    monkeypatch.setattr(source_intent, "source_reconcile_lock", _source_lock)
    monkeypatch.setattr(cr, "_acquire_entry_lock", _coupling_lock)

    result = cr.run_health_check(
        tick_state_path=str(tmp_path / "tick.json"),
        marker_path=str(tmp_path / "fallback.json"),
        source_intent_env_path=str(tmp_path / "source_intent.env"),
        read_fanin_status=lambda: (None, "not running"),
    )

    assert result.ok is True
    assert events == [
        "source_acquired",
        "coupling_acquired",
        "coupling_released",
        "source_released",
    ]


def test_concurrent_source_reconcile_and_health_cannot_deadlock(tmp_path, monkeypatch):
    """A source pass holding source must still acquire coupling while health waits.

    This recreates the former deadlock shape without timing sleeps.  If health
    took coupling first, the source-side acquisition below would contend and the
    two passes could make no progress.
    """
    from contextlib import contextmanager
    import threading

    from jasper import source_intent

    source_path = str(tmp_path / "source_intent.env")
    coupling_path = tmp_path / "entry.lock"
    real_source_lock = source_intent.source_reconcile_lock
    source_held = threading.Event()
    health_attempted_source = threading.Event()
    let_source_take_coupling = threading.Event()
    outcomes = []

    @contextmanager
    def _observed_health_source_lock(**kwargs):
        health_attempted_source.set()
        with real_source_lock(**kwargs):
            yield

    monkeypatch.setattr(
        source_intent,
        "source_reconcile_lock",
        _observed_health_source_lock,
    )

    def _source_pass():
        with real_source_lock(env_path=source_path, timeout_sec=1.0):
            source_held.set()
            assert let_source_take_coupling.wait(1.0)
            lock = cr._acquire_entry_lock(
                coupling_path,
                timeout_seconds=0.5,
                poll_seconds=0.01,
            )
            outcomes.append(("source", lock.outcome))
            if lock.fh is not None:
                lock.fh.close()

    def _health_pass():
        result = cr.run_health_check(
            tick_state_path=str(tmp_path / "tick.json"),
            marker_path=str(tmp_path / "fallback.json"),
            source_intent_env_path=source_path,
            source_lock_timeout_seconds=1.0,
            entry_lock_path=coupling_path,
            entry_lock_timeout_seconds=0.5,
            read_fanin_status=lambda: (None, "not running"),
        )
        outcomes.append(("health", result.ok))

    source_thread = threading.Thread(target=_source_pass)
    health_thread = threading.Thread(target=_health_pass)
    source_thread.start()
    assert source_held.wait(1.0)
    health_thread.start()
    assert health_attempted_source.wait(1.0)
    let_source_take_coupling.set()
    source_thread.join(2.0)
    health_thread.join(2.0)

    assert not source_thread.is_alive()
    assert not health_thread.is_alive()
    assert ("source", "acquired") in outcomes
    assert ("health", True) in outcomes


# ---- CLI: --auto clears the marker (clear-and-retry); --health mutual excl ---


def test_cli_auto_clears_fallback_marker(tmp_path, monkeypatch, capsys):
    marker_path = str(tmp_path / "fallback.json")
    ch.write_fallback_marker("prior break", marker_path)
    # Redirect the CLI's no-arg marker helpers to the tmp path (the module default
    # is frozen at def-time, so patching FALLBACK_MARKER_PATH alone won't retarget
    # them). The CLI re-imports these from the module at call time, so patching the
    # module attributes is picked up.
    _orig_clear = ch.clear_fallback_marker
    _orig_read = ch.read_fallback_marker
    monkeypatch.setattr(ch, "clear_fallback_marker", lambda p=marker_path: _orig_clear(p))
    monkeypatch.setattr(ch, "read_fallback_marker", lambda p=marker_path: _orig_read(p))
    # Stub the heavy bits: env hydrate + the actual reconcile.
    import jasper.env_load as env_load

    monkeypatch.setattr(env_load, "load_env_files", lambda: None)
    seen = {"reconcile": 0}

    def _fake_reconcile_auto(**kw):
        seen["reconcile"] += 1
        return cr.AutoResult(
            ok=True, owned=True, coupling="loopback", gadget_present=False,
            usb_combo_changed=False, reason="",
        )

    monkeypatch.setattr(cr, "reconcile_auto", _fake_reconcile_auto)
    rc = cr.main(["--auto", "--reason", "systemd"])
    assert rc == 0
    assert seen["reconcile"] == 1
    # The marker was cleared before the reconcile (clear-and-retry).
    assert ch.fallback_active(marker_path) is False


def test_cli_health_and_auto_mutually_exclusive(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        cr.main(["--auto", "--health"])
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_cli_health_dispatches_to_run_health_check(monkeypatch, capsys):
    import jasper.env_load as env_load

    monkeypatch.setattr(env_load, "load_env_files", lambda: None)
    seen = {"health": 0}

    def _fake_health(**kw):
        seen["health"] += 1
        return cr.HealthResult(ok=True, watched=False)

    monkeypatch.setattr(cr, "run_health_check", _fake_health)
    rc = cr.main(["--health", "--reason", "systemd"])
    assert rc == 0
    assert seen["health"] == 1


def test_cli_health_recovered_transition_reaches_configured_handler(
    tmp_path, monkeypatch, capsys
):
    """'Only real transitions log' requires the INFO ``recovered`` line to
    actually emit: pre-fix, main() configured no logging handler, so the root
    logger's lastResort fallback (WARNING+) silently dropped it from the
    jasper-fanin-combo-health.service journal (observed on jts.local build
    41886ab8, 2026-07-11). Drive the REAL run_health_check through
    ``main(["--health"])`` across a broken->healthy tick pair and assert the
    recovered ``event=`` line reaches the handler main() configures."""
    import logging

    import jasper.env_load as env_load

    monkeypatch.setattr(env_load, "load_env_files", lambda: None)
    # Tick 1 (direct call): broken — seeds consecutive_broken=1 in tick state.
    _run(tmp_path, _status(health="broken"))

    # Tick 2 goes through the CLI with the real run_health_check wired to the
    # injected tmp paths/status: healthy sample + broken prev = 'recovered'.
    real = cr.run_health_check

    def _wired(**kw):
        return real(
            reason=kw.get("reason", "t"),
            apply=kw.get("apply", True),
            tick_state_path=str(tmp_path / "tick.json"),
            marker_path=str(tmp_path / "fallback.json"),
            read_fanin_status=lambda: (_status(health="capturing"), ""),
            run_reconcile=lambda: _fake_auto(),
            source_intent_env_path=str(tmp_path / "source_intent.env"),
        )

    monkeypatch.setattr(cr, "run_health_check", _wired)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    # Fresh-interpreter shape: no root handler until main() configures one.
    root.handlers.clear()
    try:
        rc = cr.main(["--health", "--reason", "systemd"])
    finally:
        for h in root.handlers[:]:
            if h not in saved_handlers:
                root.removeHandler(h)
                h.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert rc == 0
    err = capsys.readouterr().err
    assert "event=fanin.combo_health" in err
    assert "result=recovered" in err
