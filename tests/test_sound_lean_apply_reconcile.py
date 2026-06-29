# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.sound.lean_apply_reconcile — the privileged lean-apply
delegation.

The non-root jasper-mux cannot write /var/lib/camilladsp/configs, so it
DELEGATES the lean enter/leave CamillaDSP config swap to the
jasper-lean-apply oneshot, started BLOCKING through jasper-control's restart
broker. The broker is stubbed; the intent-file upsert is real (tmp file). The
oneshot CLI (main) is exercised with the actual apply/restore functions
monkeypatched.
"""
from __future__ import annotations

import pytest

from jasper.sound import lean_apply_reconcile as lar


@pytest.fixture
def env_path(tmp_path):
    return tmp_path / "lean.env"


@pytest.fixture
def stub_broker(monkeypatch):
    calls: list[tuple] = []

    def fake_manage(*units, verb="restart", reason="", no_block=True, timeout=5.0):
        calls.append((units, verb, reason, no_block))
        return {"ok": True}

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units", fake_manage,
    )
    return calls


# -------------------------------------------------------------- delegate()


def test_enter_writes_intent_and_starts_oneshot_blocking(env_path, stub_broker):
    r = lar.delegate(lar.ACTION_ENTER, reason="lean_enter", env_path=env_path)
    assert r.ok and r.action == "enter"
    assert env_path.read_text().strip() == "JASPER_LEAN_ACTION=enter"
    # Delegated via the broker START verb (not restart), BLOCKING (no_block=False)
    # so the oneshot's rc reaches the caller's ladder.
    assert stub_broker == [((lar.LEAN_APPLY_UNIT,), "start", "lean_enter", False)]


def test_leave_writes_intent_and_starts_oneshot(env_path, stub_broker):
    r = lar.delegate(lar.ACTION_LEAVE, reason="lean_leave", env_path=env_path)
    assert r.ok and r.action == "leave"
    assert env_path.read_text().strip() == "JASPER_LEAN_ACTION=leave"
    assert stub_broker[0][1] == "start"


def test_invalid_action_rejected_without_write(env_path, stub_broker):
    r = lar.delegate("bogus", reason="x", env_path=env_path)
    assert r.ok is False
    assert "invalid action" in r.detail
    assert not env_path.exists()
    assert stub_broker == []


def test_oneshot_failure_falls_back_to_buffered(env_path, monkeypatch):
    # The privileged apply oneshot failed (e.g. CarrierCannotHostEq, camilla
    # down): ok=False so the mux ladder falls back to buffered.
    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *a, **k: {"ok": False, "error": "rc=1"},
    )
    r = lar.delegate(lar.ACTION_ENTER, reason="lean_enter", env_path=env_path)
    assert r.ok is False
    assert "rc=1" in r.detail
    # The intent IS written (the oneshot read it); the caller treats ok=False as
    # buffered. Unlike the usbsink arm there is no persisted-mode-vs-running
    # drift to roll back — the intent file only gates a single oneshot run.
    assert env_path.read_text().strip() == "JASPER_LEAN_ACTION=enter"


def test_broker_import_failure_is_fail_soft(env_path, monkeypatch):
    # A missing/broken control package must degrade to ok=False, never raise out
    # of delegate() into the caller's tick.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "jasper.control" and "restart_broker" in (fromlist or ()):
            raise ImportError("no control package here")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = lar.delegate(lar.ACTION_ENTER, reason="lean_enter", env_path=env_path)
    assert r.ok is False
    assert "restart_broker unavailable" in r.detail


def test_delegate_preserves_operator_lines(env_path, stub_broker):
    env_path.write_text("# operator\nJASPER_LEAN_FOO=1\nJASPER_LEAN_ACTION=leave\n")
    lar.delegate(lar.ACTION_ENTER, reason="x", env_path=env_path)
    text = env_path.read_text()
    assert "JASPER_LEAN_FOO=1" in text
    assert "# operator" in text
    assert text.count("JASPER_LEAN_ACTION=") == 1
    assert "JASPER_LEAN_ACTION=enter" in text


def test_read_action_roundtrip(env_path, stub_broker):
    assert lar.read_action(env_path) is None
    lar.delegate(lar.ACTION_LEAVE, reason="x", env_path=env_path)
    assert lar.read_action(env_path) == "leave"


# -------------------------------------------------------------- main() (oneshot)


@pytest.fixture
def stub_env_load(monkeypatch):
    monkeypatch.setattr("jasper.env_load.load_env_files", lambda *a, **k: None)


def test_main_enter_runs_apply(monkeypatch, stub_env_load):
    called: list[str] = []

    async def fake_apply():
        called.append("apply")
        return {}

    async def fake_restore():
        called.append("restore")

    monkeypatch.setattr(
        "jasper.sound.runtime.apply_lean_capture_config", fake_apply,
    )
    monkeypatch.setattr(
        "jasper.sound.runtime.restore_buffered_config", fake_restore,
    )
    rc = lar.main(["--action", "enter"])
    assert rc == 0
    assert called == ["apply"]


def test_main_leave_runs_restore(monkeypatch, stub_env_load):
    called: list[str] = []

    async def fake_apply():
        called.append("apply")
        return {}

    async def fake_restore():
        called.append("restore")

    monkeypatch.setattr(
        "jasper.sound.runtime.apply_lean_capture_config", fake_apply,
    )
    monkeypatch.setattr(
        "jasper.sound.runtime.restore_buffered_config", fake_restore,
    )
    rc = lar.main(["--action", "leave"])
    assert rc == 0
    assert called == ["restore"]


def test_main_reads_action_from_env_file(monkeypatch, tmp_path, stub_env_load):
    env = tmp_path / "lean.env"
    env.write_text("JASPER_LEAN_ACTION=enter\n")
    monkeypatch.setattr(lar, "LEAN_ENV_PATH", str(env))

    called: list[str] = []

    async def fake_apply():
        called.append("apply")
        return {}

    monkeypatch.setattr(
        "jasper.sound.runtime.apply_lean_capture_config", fake_apply,
    )
    rc = lar.main([])  # no --action override -> read lean.env
    assert rc == 0
    assert called == ["apply"]


def test_main_no_action_exits_nonzero(monkeypatch, tmp_path, stub_env_load):
    monkeypatch.setattr(lar, "LEAN_ENV_PATH", str(tmp_path / "absent.env"))
    rc = lar.main([])
    assert rc == 2


def test_main_apply_failure_exits_one(monkeypatch, stub_env_load):
    async def boom():
        raise RuntimeError("camilla down")

    monkeypatch.setattr(
        "jasper.sound.runtime.apply_lean_capture_config", boom,
    )
    # The oneshot reports failure via exit code 1 (not a traceback crash) so the
    # broker's blocking start carries the verdict back to the mux ladder.
    rc = lar.main(["--action", "enter"])
    assert rc == 1
