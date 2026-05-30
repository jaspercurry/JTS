"""Unit tests for jasper.debug_mode — the runtime debug-logging SSOT.

Covers the pure resolver, the auto-expiry semantics, the pure
env-update computation the /debug endpoint uses, and the best-effort
apply path that must never break daemon startup.
"""
from __future__ import annotations

import logging

import pytest

from jasper import debug_mode as dm

NOW = 1_000_000.0


# --------------------------------------------------------------- resolve


def test_resolve_configured_and_active():
    env = {"JASPER_DEBUG_VOICE": "1", "JASPER_DEBUG_AEC": "0"}
    st = dm.resolve_debug_state(env, now=NOW)
    assert st.configured == frozenset({"voice"})
    assert st.active == frozenset({"voice"})  # no expiry set → not expired
    assert not st.expired


def test_resolve_expiry_in_future_keeps_active():
    env = {"JASPER_DEBUG_VOICE": "1", dm.EXPIRES_KEY: str(NOW + 600)}
    st = dm.resolve_debug_state(env, now=NOW)
    assert st.active == frozenset({"voice"})
    assert st.remaining_sec == pytest.approx(600)


def test_resolve_expired_makes_active_empty():
    env = {"JASPER_DEBUG_VOICE": "1", dm.EXPIRES_KEY: str(NOW - 1)}
    st = dm.resolve_debug_state(env, now=NOW)
    assert st.configured == frozenset({"voice"})  # flag still set on disk
    assert st.active == frozenset()               # but expired → inactive
    assert st.expired
    assert st.remaining_sec == 0.0


def test_resolve_malformed_expiry_treated_as_none():
    env = {"JASPER_DEBUG_VOICE": "1", dm.EXPIRES_KEY: "not-a-number"}
    st = dm.resolve_debug_state(env, now=NOW)
    assert st.expires_at is None
    assert st.active == frozenset({"voice"})


def test_resolve_expiry_without_configured_is_normalized():
    env = {dm.EXPIRES_KEY: str(NOW + 600)}  # expiry but nothing on
    st = dm.resolve_debug_state(env, now=NOW)
    assert st.configured == frozenset()
    assert st.expires_at is None


# ----------------------------------------------------- compute_env_update


def test_compute_update_enable_sets_key_and_expiry():
    upd = dm.compute_env_update({}, "voice", True, now=NOW, ttl=7200)
    assert upd["JASPER_DEBUG_VOICE"] == "1"
    assert upd[dm.EXPIRES_KEY] == str(int(NOW + 7200))


def test_compute_update_disable_last_clears_expiry():
    cur = {"JASPER_DEBUG_VOICE": "1", dm.EXPIRES_KEY: str(NOW + 10)}
    upd = dm.compute_env_update(cur, "voice", False, now=NOW)
    assert upd["JASPER_DEBUG_VOICE"] == "0"
    assert upd[dm.EXPIRES_KEY] == ""  # nothing left on → expiry cleared


def test_compute_update_disable_one_of_several_keeps_expiry():
    cur = {"JASPER_DEBUG_VOICE": "1", "JASPER_DEBUG_AEC": "1"}
    upd = dm.compute_env_update(cur, "voice", False, now=NOW, ttl=7200)
    assert upd["JASPER_DEBUG_VOICE"] == "0"
    assert upd[dm.EXPIRES_KEY] == str(int(NOW + 7200))  # aec still on


def test_compute_update_unknown_subsystem_raises():
    with pytest.raises(ValueError):
        dm.compute_env_update({}, "bogus", True, now=NOW)


# ------------------------------------------------------------- read (IO)


def test_read_missing_file_is_inactive(tmp_path):
    st = dm.read_debug_state(path=str(tmp_path / "absent.env"), now=NOW)
    assert st.active == frozenset()
    assert st.expires_at is None


def test_read_roundtrip_from_file(tmp_path):
    f = tmp_path / "debug.env"
    f.write_text(f"JASPER_DEBUG_AEC=1\n{dm.EXPIRES_KEY}={int(NOW + 300)}\n")
    st = dm.read_debug_state(path=str(f), now=NOW)
    assert st.active == frozenset({"aec"})
    assert st.remaining_sec == pytest.approx(300)


# ------------------------------------------------------------- apply_for


@pytest.fixture
def restore_jasper_level():
    """Snapshot/restore the 'jasper' logger level so apply_for tests
    don't leak DEBUG into the rest of the suite."""
    lg = logging.getLogger("jasper")
    before = lg.level
    yield
    lg.setLevel(before)


def _write(tmp_path, body: str) -> str:
    f = tmp_path / "debug.env"
    f.write_text(body)
    return str(f)


def test_apply_for_active_raises_logger_to_debug(tmp_path, restore_jasper_level):
    logging.getLogger("jasper").setLevel(logging.INFO)
    path = _write(tmp_path, "JASPER_DEBUG_VOICE=1\n")
    applied = dm.apply_for("voice", now=NOW, path=path)
    assert applied is True
    assert logging.getLogger("jasper").level == logging.DEBUG


def test_apply_for_inactive_leaves_logger(tmp_path, restore_jasper_level):
    logging.getLogger("jasper").setLevel(logging.INFO)
    path = _write(tmp_path, "JASPER_DEBUG_AEC=1\n")  # aec on, not voice
    applied = dm.apply_for("voice", now=NOW, path=path)
    assert applied is False
    assert logging.getLogger("jasper").level == logging.INFO


def test_apply_for_expired_leaves_logger(tmp_path, restore_jasper_level):
    logging.getLogger("jasper").setLevel(logging.INFO)
    path = _write(
        tmp_path, f"JASPER_DEBUG_VOICE=1\n{dm.EXPIRES_KEY}={int(NOW - 1)}\n"
    )
    applied = dm.apply_for("voice", now=NOW, path=path)
    assert applied is False
    assert logging.getLogger("jasper").level == logging.INFO


def test_apply_for_unknown_subsystem_returns_false(tmp_path, restore_jasper_level):
    path = _write(tmp_path, "JASPER_DEBUG_VOICE=1\n")
    assert dm.apply_for("bogus", now=NOW, path=path) is False


def test_apply_for_missing_file_does_not_raise(restore_jasper_level):
    # Best-effort: a daemon must start even if debug.env is absent.
    assert dm.apply_for("voice", now=NOW, path="/nonexistent/debug.env") is False
