"""Unit tests for jasper.peering.config.

Pure logic — no I/O beyond the env file the loader reads. Exercises
the precedence ladder (env-file < process env < overrides), parsing
of malformed values, and the peer_id idempotency contract.
"""
from __future__ import annotations

import os
import re
import uuid

import pytest

from jasper.peering import config as peering_config
from jasper.peering.config import (
    DEFAULT_ARB_WINDOW_MS,
    DEFAULT_BREAK_THRESHOLD,
    PeeringMode,
    load_config,
)


# ---------- mode parsing ----------


def test_default_mode_is_off(tmp_path, monkeypatch):
    """No env file, no process env → mode resolves to OFF.

    This is the load-bearing default for the whole project: a single-Pi
    household must never accidentally enable peering. If this test ever
    fails, the default flipped — likely a bug, almost certainly not
    what we want.
    """
    _clear_peer_env(monkeypatch)
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.mode is PeeringMode.OFF
    assert cfg.enabled is False


def test_env_file_mode_on(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEERING=on\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.mode is PeeringMode.ON
    assert cfg.enabled is True


@pytest.mark.parametrize(
    "value,expected_mode",
    [
        ("on", PeeringMode.ON),
        ("ON", PeeringMode.ON),
        ("true", PeeringMode.ON),
        ("1", PeeringMode.ON),
        ("yes", PeeringMode.ON),
        ("off", PeeringMode.OFF),
        ("OFF", PeeringMode.OFF),
        ("false", PeeringMode.OFF),
        ("0", PeeringMode.OFF),
        ("", PeeringMode.OFF),
        ("garbage", PeeringMode.OFF),  # malformed → OFF (fail-safe)
        ("auto", PeeringMode.OFF),     # we deliberately don't support auto
    ],
)
def test_mode_parsing(value, expected_mode, tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text(f"JASPER_PEERING={value}\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.mode is expected_mode


# ---------- precedence ladder ----------


def test_process_env_overrides_file(tmp_path, monkeypatch):
    """A wizard write to the file is the normal source of truth, but
    if the operator sets JASPER_PEERING in /etc/jasper/jasper.env, the
    process env's value reflects what systemd merged. Process env wins
    over a stale file (matches the wake-wizard precedence)."""
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEERING=off\n")
    monkeypatch.setenv("JASPER_PEERING", "on")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.mode is PeeringMode.ON


def test_overrides_arg_wins(tmp_path, monkeypatch):
    """Test injection — overrides arg beats both env file and process env."""
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEERING=on\n")
    monkeypatch.setenv("JASPER_PEERING", "on")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
        overrides={"JASPER_PEERING": "off"},
    )
    assert cfg.mode is PeeringMode.OFF


# ---------- numeric parsing ----------


def test_arb_window_default(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.arb_window_ms == DEFAULT_ARB_WINDOW_MS


def test_arb_window_custom(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEER_ARB_WINDOW_MS=200\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.arb_window_ms == 200


def test_arb_window_clamped(tmp_path, monkeypatch):
    """Values outside the safe range get clamped, not rejected."""
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEER_ARB_WINDOW_MS=10000\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.arb_window_ms == 500  # high clamp


def test_arb_window_garbage_falls_through(tmp_path, monkeypatch):
    """A malformed numeric must fall through to default, not crash."""
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEER_ARB_WINDOW_MS=banana\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.arb_window_ms == DEFAULT_ARB_WINDOW_MS


def test_break_threshold_default(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.break_threshold == DEFAULT_BREAK_THRESHOLD


# ---------- room + primary ----------


def test_room_default_fallback(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    # Hostname-derived, sanitized. We can't predict the exact value
    # but we can verify it's a non-empty string of safe chars.
    assert cfg.room
    assert re.match(r"^[a-z0-9_-]+$", cfg.room), cfg.room


def test_room_custom(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEER_ROOM=living-room\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.room == "living-room"


def test_primary_off_by_default(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.primary is False


def test_primary_on(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEER_PRIMARY=1\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.primary is True


# ---------- peer_id idempotency ----------


def test_peer_id_generated_and_persisted(tmp_path, monkeypatch):
    """First call generates a UUID and writes the file; second call
    reads it back unchanged. Critical: a Pi that restarts shouldn't
    look like a "new" device to its peers."""
    _clear_peer_env(monkeypatch)
    peer_id_file = tmp_path / "peer_id"
    cfg1 = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(peer_id_file),
    )
    assert peer_id_file.exists()
    uuid.UUID(cfg1.peer_id)  # validates it's a well-formed UUID

    # Second load — should reuse.
    cfg2 = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(peer_id_file),
    )
    assert cfg1.peer_id == cfg2.peer_id


def test_peer_id_pre_existing_respected(tmp_path, monkeypatch):
    """A peer_id file installed by install.sh (or written by the
    operator) must be respected verbatim."""
    _clear_peer_env(monkeypatch)
    peer_id_file = tmp_path / "peer_id"
    custom = "deadbeef-0000-0000-0000-000000000000"
    peer_id_file.write_text(custom + "\n")
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(peer_id_file),
    )
    assert cfg.peer_id == custom


# ---------- helpers ----------


def _clear_peer_env(monkeypatch) -> None:
    """Strip any JASPER_PEER* vars from the test process env so tests
    are deterministic regardless of how the developer's shell is set up.
    """
    for k in list(os.environ):
        if k.startswith("JASPER_PEER"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("JASPER_PEERING", raising=False)
