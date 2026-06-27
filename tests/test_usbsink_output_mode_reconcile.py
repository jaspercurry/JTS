# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.usbsink.output_mode_reconcile — the runtime FIFO arm.

Writes JASPER_USBSINK_OUTPUT_MODE into the unit's wizard env file and restarts
the daemon through the broker. The broker is stubbed; the env-file upsert is
real (tmp file).
"""
from __future__ import annotations

import pytest

from jasper.usbsink import output_mode_reconcile as omr


@pytest.fixture
def env_path(tmp_path):
    return tmp_path / "usbsink.env"


@pytest.fixture
def stub_broker(monkeypatch):
    calls: list[tuple] = []

    def fake_manage(*units, verb="restart", reason="", no_block=True, timeout=5.0):
        calls.append((units, verb, reason))
        return {"ok": True}

    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units", fake_manage,
    )
    return calls


def test_arm_fifo_writes_env_and_restarts(env_path, stub_broker):
    r = omr.set_output_mode("fifo", reason="lean_enter", env_path=env_path)
    assert r.ok and r.changed and r.restarted
    assert env_path.read_text().strip() == "JASPER_USBSINK_OUTPUT_MODE=fifo"
    # Restarted the usbsink unit.
    assert stub_broker == [((omr.USBSINK_UNIT,), "restart", "lean_enter")]


def test_arm_invalid_mode_is_rejected_without_write(env_path, stub_broker):
    r = omr.set_output_mode("bogus", reason="x", env_path=env_path)
    assert r.ok is False
    assert not env_path.exists()
    assert stub_broker == []


def test_arm_unchanged_skips_restart(env_path, stub_broker):
    env_path.write_text("JASPER_USBSINK_OUTPUT_MODE=fifo\n")
    r = omr.set_output_mode("fifo", reason="lean_enter", env_path=env_path)
    assert r.ok is True
    assert r.changed is False
    assert r.restarted is False
    # No restart for a no-op arm — must not glitch audio on a re-armed tick.
    assert stub_broker == []


def test_arm_preserves_other_keys(env_path, stub_broker):
    env_path.write_text(
        "# operator overrides\n"
        "JASPER_USBSINK_LATENCY=0.02\n"
        "JASPER_USBSINK_OUTPUT_MODE=aloop\n"
    )
    omr.set_output_mode("fifo", reason="lean_enter", env_path=env_path)
    text = env_path.read_text()
    assert "JASPER_USBSINK_LATENCY=0.02" in text
    assert "JASPER_USBSINK_OUTPUT_MODE=fifo" in text
    assert "# operator overrides" in text
    # Exactly one output-mode line.
    assert text.count("JASPER_USBSINK_OUTPUT_MODE=") == 1


def test_disarm_back_to_aloop(env_path, stub_broker):
    env_path.write_text("JASPER_USBSINK_OUTPUT_MODE=fifo\n")
    r = omr.set_output_mode("aloop", reason="lean_leave", env_path=env_path)
    assert r.ok and r.changed
    assert "JASPER_USBSINK_OUTPUT_MODE=aloop" in env_path.read_text()


def test_arm_restart_failure_reports_not_ok(env_path, monkeypatch):
    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *a, **k: {"ok": False, "error": "broker down"},
    )
    r = omr.set_output_mode("fifo", reason="lean_enter", env_path=env_path)
    # The env was written, but the restart failed -> ok=False so the caller
    # falls back to buffered.
    assert r.ok is False
    assert r.changed is True
    assert r.restarted is False
    assert "broker down" in r.detail
    assert "JASPER_USBSINK_OUTPUT_MODE=fifo" in env_path.read_text()


def test_arm_no_restart_flag_writes_only(env_path, stub_broker):
    r = omr.set_output_mode("fifo", reason="x", env_path=env_path, restart=False)
    assert r.ok is True
    assert r.restarted is False
    assert stub_broker == []
    assert "JASPER_USBSINK_OUTPUT_MODE=fifo" in env_path.read_text()


def test_read_output_mode_roundtrip(env_path, stub_broker):
    assert omr.read_output_mode(env_path) is None
    omr.set_output_mode("fifo", reason="x", env_path=env_path)
    assert omr.read_output_mode(env_path) == "fifo"
