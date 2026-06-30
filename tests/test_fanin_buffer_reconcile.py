# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.fanin.buffer_reconcile — the adaptive output-buffer arm.

Writes JASPER_FANIN_OUTPUT_BUFFER_FRAMES into the unit's wizard env file and
restarts the daemon through the broker. The broker is stubbed; the env-file
upsert is real (tmp file). Floor enforcement, SF-1 rollback on restart failure,
and SF-2 fail-soft import are pinned here.
"""
from __future__ import annotations

import pytest

from jasper.fanin import buffer_reconcile as br


@pytest.fixture
def env_path(tmp_path):
    return tmp_path / "fanin.env"


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


# ----------------------------------------------------------------- set: shrink

def test_shrink_writes_env_and_restarts(env_path, stub_broker):
    r = br.set_fanin_output_buffer(
        1024, reason="adaptive_usb_exclusive", env_path=env_path,
    )
    assert r.ok and r.changed and r.restarted
    assert r.frames == 1024
    assert env_path.read_text().strip() == "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024"
    assert stub_broker == [((br.FANIN_UNIT,), "restart", "adaptive_usb_exclusive")]


def test_shrink_unchanged_skips_restart(env_path, stub_broker):
    env_path.write_text("JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024\n")
    r = br.set_fanin_output_buffer(1024, reason="x", env_path=env_path)
    assert r.ok is True
    assert r.changed is False
    assert r.restarted is False
    # No restart for a no-op arm — must not glitch the shared audio path.
    assert stub_broker == []


def test_shrink_preserves_other_keys(env_path, stub_broker):
    env_path.write_text(
        "# operator overrides\n"
        "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096\n"
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072\n"
    )
    br.set_fanin_output_buffer(1024, reason="x", env_path=env_path)
    text = env_path.read_text()
    assert "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096" in text
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024" in text
    assert "# operator overrides" in text
    assert text.count("JASPER_FANIN_OUTPUT_BUFFER_FRAMES=") == 1


def test_shrink_no_restart_flag_writes_only(env_path, stub_broker):
    r = br.set_fanin_output_buffer(
        1024, reason="x", env_path=env_path, restart=False,
    )
    assert r.ok is True
    assert r.restarted is False
    assert stub_broker == []
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024" in env_path.read_text()


# ------------------------------------------------------------------- floor

def test_below_floor_rejected_without_write(env_path, stub_broker):
    r = br.set_fanin_output_buffer(512, reason="x", env_path=env_path)
    # 512 < MIN_OUTPUT_BUFFER_FRAMES (1024): rejected, file untouched, no
    # restart. Never persist a sub-floor buffer without hardware validation.
    assert r.ok is False
    assert "below floor" in r.detail
    assert not env_path.exists()
    assert stub_broker == []


def test_floor_value_itself_is_accepted(env_path, stub_broker):
    r = br.set_fanin_output_buffer(
        br.MIN_OUTPUT_BUFFER_FRAMES, reason="x", env_path=env_path,
    )
    assert r.ok is True


# ----------------------------------------------------------------- restore

def test_restore_strips_override_and_restarts(env_path, stub_broker):
    env_path.write_text("JASPER_FANIN_OUTPUT_BUFFER_FRAMES=2048\n")
    r = br.restore_fanin_output_buffer(reason="networked_join", env_path=env_path)
    assert r.ok and r.changed and r.restarted
    assert r.frames == br.DEFAULT_OUTPUT_BUFFER_FRAMES
    # The override line is removed; the file is unlinked (empties to nothing) so
    # the unit's Environment="...=1024" default reasserts as the only source.
    assert not env_path.exists()
    assert stub_broker == [((br.FANIN_UNIT,), "restart", "networked_join")]


def test_restore_keeps_other_keys_when_override_present(env_path, stub_broker):
    env_path.write_text(
        "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096\n"
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=2048\n"
    )
    r = br.restore_fanin_output_buffer(reason="x", env_path=env_path)
    assert r.ok and r.changed
    text = env_path.read_text()
    assert "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096" in text
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES" not in text


def test_restore_noop_when_no_override(env_path, stub_broker):
    # The common steady-state path: no override present -> nothing to do, no
    # restart of the shared daemon.
    r = br.restore_fanin_output_buffer(reason="x", env_path=env_path)
    assert r.ok is True
    assert r.changed is False
    assert r.restarted is False
    assert stub_broker == []


# ----------------------------------------------------- SF-1: restart rollback

def test_shrink_restart_failure_rolls_back_to_absent(env_path, monkeypatch):
    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *a, **k: {"ok": False, "error": "broker down"},
    )
    r = br.set_fanin_output_buffer(1024, reason="x", env_path=env_path)
    # SF-1: restart failed -> env rolled back so the persisted file never leads
    # the running daemon. File was absent before -> rollback restores absence.
    assert r.ok is False
    assert r.changed is False
    assert r.restarted is False
    assert "broker down" in r.detail
    assert not env_path.exists()


def test_shrink_restart_failure_restores_prior_content(env_path, monkeypatch):
    env_path.write_text(
        "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096\n"
        "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072\n"
    )
    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *a, **k: {"ok": False, "error": "broker down"},
    )
    r = br.set_fanin_output_buffer(1024, reason="x", env_path=env_path)
    assert r.ok is False
    text = env_path.read_text()
    # Rolled back to the prior 3072 value + the sibling key preserved; NOT 1024.
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072" in text
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024" not in text
    assert "JASPER_FANIN_INPUT_BUFFER_FRAMES=4096" in text


def test_restore_restart_failure_rolls_back_override(env_path, monkeypatch):
    env_path.write_text("JASPER_FANIN_OUTPUT_BUFFER_FRAMES=2048\n")
    monkeypatch.setattr(
        "jasper.control.restart_broker.manage_units",
        lambda *a, **k: {"ok": False, "error": "broker down"},
    )
    r = br.restore_fanin_output_buffer(reason="x", env_path=env_path)
    # Restore's restart failed -> the stripped file is rolled back to the prior
    # shrunk override (still ahead of nothing, but matches the daemon which is
    # still running the shrunk value). Caller keeps _buffer_shrunk + retries.
    assert r.ok is False
    assert "JASPER_FANIN_OUTPUT_BUFFER_FRAMES=2048" in env_path.read_text()


# ----------------------------------------------------- SF-2: import fail-soft

def test_shrink_restart_broker_import_failure_is_fail_soft(env_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "jasper.control" and "restart_broker" in (fromlist or ()):
            raise ImportError("no control package here")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = br.set_fanin_output_buffer(1024, reason="x", env_path=env_path)
    assert r.ok is False
    assert r.restarted is False
    assert "restart_broker unavailable" in r.detail
    # And the env was rolled back (no shrunk value persisted).
    assert not env_path.exists()


# --------------------------------------------------------------- read helper

def test_read_output_buffer_roundtrip(env_path, stub_broker):
    assert br.read_output_buffer(env_path) is None
    br.set_fanin_output_buffer(1024, reason="x", env_path=env_path)
    assert br.read_output_buffer(env_path) == 1024


def test_read_output_buffer_malformed_is_none(env_path):
    env_path.write_text("JASPER_FANIN_OUTPUT_BUFFER_FRAMES=not-a-number\n")
    assert br.read_output_buffer(env_path) is None


# ------------------------------------------------ shrunk_target_frames env

def test_shrunk_target_defaults_to_floor(monkeypatch):
    monkeypatch.delenv("JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES", raising=False)
    assert br.shrunk_target_frames() == br.MIN_OUTPUT_BUFFER_FRAMES


def test_shrunk_target_honors_valid_override(monkeypatch):
    monkeypatch.setenv("JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES", "2048")
    assert br.shrunk_target_frames() == 2048


def test_shrunk_target_below_floor_falls_back_to_floor(monkeypatch):
    monkeypatch.setenv("JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES", "512")
    assert br.shrunk_target_frames() == br.MIN_OUTPUT_BUFFER_FRAMES


def test_shrunk_target_malformed_falls_back_to_floor(monkeypatch):
    monkeypatch.setenv("JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES", "garbage")
    assert br.shrunk_target_frames() == br.MIN_OUTPUT_BUFFER_FRAMES
