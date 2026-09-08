# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Peering config precedence, parsing and persistent peer identity."""
from __future__ import annotations

import os
import re
import uuid

import pytest

from jasper.peering.config import (
    DEFAULT_ARB_WINDOW_MS,
    DEFAULT_BREAK_THRESHOLD,
    PeeringMode,
    load_config,
    read_state,
    state_enabled,
    state_primary,
)


# ---------- mode parsing ----------


def test_default_mode_is_off(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    cfg = load_config(
        env_file=str(tmp_path / "peering.env"),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.mode is PeeringMode.OFF
    assert cfg.enabled is False


def test_load_rereads_saved_mode(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text("JASPER_PEERING=on\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.mode is PeeringMode.ON
    assert cfg.enabled is True
    env_file.write_text("JASPER_PEERING=off\n")
    assert load_config(
        env_file=str(env_file), peer_id_file=str(tmp_path / "peer_id"),
    ).enabled is False


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


@pytest.mark.parametrize("source", ["file", "override"])
@pytest.mark.parametrize(
    "key,process,value,field,expected",
    [
        ("JASPER_PEERING", "on", "off", "enabled", False),
        ("JASPER_PEERING", "off", "on", "enabled", True),
        ("JASPER_PEERING", "on", "", "enabled", False),
        ("JASPER_PEER_PRIMARY", "1", "0", "primary", False),
        ("JASPER_PEER_PRIMARY", "1", "", "primary", False),
        ("JASPER_PEER_ROOM", "old-room", "new-room", "room", "new-room"),
        ("JASPER_PEER_ARB_WINDOW_MS", "400", "200", "arb_window_ms", 200),
        ("JASPER_PEER_BREAK_THRESHOLD", "0.9", "0.8", "break_threshold", 0.8),
    ],
)
def test_config_precedence(key, process, value, field, expected, source, tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    monkeypatch.setenv(key, process)
    env_file = tmp_path / "peering.env"
    env_file.write_text(f"{key}={value if source == 'file' else process}\n")
    overrides = {key: value} if source == "override" else {}
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
        overrides=overrides,
    )
    assert getattr(cfg, field) == expected
    state = read_state(str(env_file)) | overrides
    assert cfg.enabled == state_enabled(state)
    assert cfg.primary == state_primary(state)


# ---------- numeric parsing ----------


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        pytest.param(None, DEFAULT_ARB_WINDOW_MS, id="arb_window_default"),
        pytest.param("200", 200, id="arb_window_custom"),
        # Values outside the safe range get clamped, not rejected.
        pytest.param("10000", 500, id="arb_window_clamped"),  # high clamp
        # A malformed numeric must fall through to default, not crash.
        pytest.param(
            "banana", DEFAULT_ARB_WINDOW_MS, id="arb_window_garbage_falls_through"
        ),
    ],
)
def test_arb_window(env_value, expected, tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    if env_value is not None:
        env_file.write_text(f"JASPER_PEER_ARB_WINDOW_MS={env_value}\n")
    cfg = load_config(
        env_file=str(env_file),
        peer_id_file=str(tmp_path / "peer_id"),
    )
    assert cfg.arb_window_ms == expected


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


def test_read_state_returns_plain_env_mapping(tmp_path, monkeypatch):
    """Web surfaces reuse the peering package reader instead of owning a
    second parser."""
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text(
        "JASPER_PEERING=on\n"
        "JASPER_PEER_PRIMARY=1\n"
        "MALFORMED\n"
        "JASPER_PEER_ROOM=living-room\n",
    )

    assert read_state(str(env_file)) == {
        "JASPER_PEERING": "on",
        "JASPER_PEER_PRIMARY": "1",
        "JASPER_PEER_ROOM": "living-room",
    }


def test_read_state_missing_file_is_empty(tmp_path, monkeypatch):
    """Missing peering.env means default-off, not an exception."""
    _clear_peer_env(monkeypatch)
    assert read_state(str(tmp_path / "missing.env")) == {}


def test_read_state_uses_canonical_quoted_value_semantics(tmp_path, monkeypatch):
    _clear_peer_env(monkeypatch)
    env_file = tmp_path / "peering.env"
    env_file.write_text(
        'JASPER_PEERING="on"\n'
        "JASPER_PEER_ROOM='living-room'\n",
    )

    assert read_state(str(env_file)) == {
        "JASPER_PEERING": "on",
        "JASPER_PEER_ROOM": "living-room",
    }


@pytest.mark.parametrize("file_exists", [False, True])
def test_missing_keys_use_process_fallback(tmp_path, monkeypatch, file_exists):
    _clear_peer_env(monkeypatch)
    monkeypatch.setenv("JASPER_PEERING", "on")
    monkeypatch.setenv("JASPER_PEER_PRIMARY", "1")
    env_file = tmp_path / "peering.env"
    if file_exists:
        env_file.write_text("JASPER_PEER_ROOM=living-room\n")
    cfg = load_config(
        env_file=str(env_file), peer_id_file=str(tmp_path / "peer_id"),
    )
    state = read_state(str(env_file))
    assert cfg.enabled == state_enabled(state) is True
    assert cfg.primary == state_primary(state) is True


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
