# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Watcher-orchestration + CLI tests for the USB-combo runtime fallback
(``jasper.fanin.coupling_reconcile.run_health_check`` + the ``--health`` /
``--auto`` marker lifecycle, defect 2026-07-10). Hardware-free: fan-in STATUS and
the reconcile disarm are injected."""

from __future__ import annotations

from jasper.fanin import combo_health as ch
from jasper.fanin import coupling_reconcile as cr


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


def _run(tmp_path, status, *, reconcile=None):
    calls = {"reconcile": 0}

    def _reconcile():
        calls["reconcile"] += 1
        return (reconcile or _fake_auto)()

    res = cr.run_health_check(
        reason="t",
        tick_state_path=str(tmp_path / "tick.json"),
        marker_path=str(tmp_path / "fallback.json"),
        read_fanin_status=lambda: (status, ""),
        run_reconcile=_reconcile,
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
    marker = ch.read_fallback_marker(str(tmp_path / "fallback.json"))
    assert marker is not None
    assert "consecutive" in marker.reason
    # Tick state reset after disarm so a residual can't immediately re-fire.
    assert ch.read_tick_state(str(tmp_path / "tick.json")).consecutive_broken == 0


def test_reopen_churn_disarms_across_two_ticks(tmp_path):
    # Two consecutive ticks with the zombie reopen counter climbing = sustained.
    _run(tmp_path, _status(health="idle", reopens=1))  # baseline (no prev)
    r2, _ = _run(tmp_path, _status(health="idle", reopens=2))  # churn -> broken 1
    assert r2.broken is True and r2.disarmed is False
    r3, calls = _run(tmp_path, _status(health="idle", reopens=3))  # churn -> disarm
    assert r3.disarmed is True
    assert calls["reconcile"] == 1


def test_disarm_failure_surfaces_not_ok(tmp_path):
    _run(tmp_path, _status(health="broken"))  # tick 1
    res, _ = _run(tmp_path, _status(health="broken"), reconcile=lambda: _fake_auto(ok=False))
    assert res.disarmed is True
    assert res.ok is False


def test_read_error_is_silent_noop(tmp_path):
    # fan-in socket down -> None status -> not watched, silent, no marker.
    res = cr.run_health_check(
        reason="t",
        tick_state_path=str(tmp_path / "tick.json"),
        marker_path=str(tmp_path / "fallback.json"),
        read_fanin_status=lambda: (None, "connection refused"),
        run_reconcile=lambda: _fake_auto(),
    )
    assert res.watched is False
    assert res.ok is True


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
    import pytest

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
