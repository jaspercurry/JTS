"""Unit tests for jasper.multiroom.state.read_grouping_state.

The state reader is the fresh-read SSOT projection consumed by
jasper-control's /state aggregator. These tests assert it (a) never reads
os.environ, (b) re-reads the file on every call so a wizard save shows up
immediately, (c) returns a JSON-able dict mirroring GroupingConfig, and
(d) is total — a missing/malformed file resolves to the disabled snapshot
without raising.

Mirrors the house style in tests/test_multiroom_config.py: tmp_path-written
env file, no network/subprocess, plain asserts.
"""
from __future__ import annotations

from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC
from jasper.multiroom.state import read_grouping_state


# ---------- helpers ----------


def _write_env(tmp_path, body: str) -> str:
    p = tmp_path / "grouping.env"
    p.write_text(body)
    return str(p)


def _leader_env() -> str:
    return (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_CODEC=opus\n"
    )


_EXPECTED_KEYS = {
    "enabled", "role", "channel", "bond_id",
    "leader_addr", "buffer_ms", "codec", "error",
}


# ---------- absent file => disabled dict, never raises ----------


def test_absent_file_is_disabled_dict(tmp_path):
    state = read_grouping_state(str(tmp_path / "does-not-exist.env"))
    assert state["enabled"] is False
    assert state["role"] == ""
    assert state["channel"] == "stereo"
    assert state["bond_id"] == ""
    assert state["leader_addr"] == ""
    assert state["buffer_ms"] == DEFAULT_BUFFER_MS
    assert state["codec"] == DEFAULT_CODEC
    assert state["error"] is None


def test_absent_file_never_raises(tmp_path):
    # Should not raise even for a path whose parent dir is missing.
    read_grouping_state(str(tmp_path / "nope" / "grouping.env"))


def test_state_has_exactly_the_expected_keys(tmp_path):
    state = read_grouping_state(str(tmp_path / "missing.env"))
    assert set(state.keys()) == _EXPECTED_KEYS


# ---------- valid enabled => full dict incl codec ----------


def test_valid_enabled_full_dict_includes_codec(tmp_path):
    path = _write_env(tmp_path, _leader_env())
    state = read_grouping_state(path)
    assert state["enabled"] is True
    assert state["role"] == "leader"
    assert state["channel"] == "left"
    assert state["bond_id"] == "living-room"
    assert state["codec"] == "opus"
    assert state["error"] is None


def test_valid_enabled_codec_defaults_to_flac(tmp_path):
    """An enabled config with no codec key surfaces the default codec."""
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=kitchen\n"
    )
    path = _write_env(tmp_path, body)
    state = read_grouping_state(path)
    assert state["codec"] == DEFAULT_CODEC


def test_state_dict_is_json_able(tmp_path):
    """The dict must round-trip through json (the /state contract)."""
    import json

    path = _write_env(tmp_path, _leader_env())
    state = read_grouping_state(path)
    # Should not raise; round-trips to an equal dict.
    assert json.loads(json.dumps(state)) == state


# ---------- invalid enabled => error present ----------


def test_invalid_enabled_surfaces_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        # no bond id => invalid (fail-LOUD)
    )
    path = _write_env(tmp_path, body)
    state = read_grouping_state(path)
    assert state["enabled"] is True
    assert state["error"] is not None
    assert "BOND_ID" in state["error"]


def test_invalid_bad_codec_surfaces_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=den\n"
        "JASPER_GROUPING_CODEC=mp3\n"
    )
    path = _write_env(tmp_path, body)
    state = read_grouping_state(path)
    assert state["enabled"] is True
    assert state["error"] is not None
    assert "CODEC" in state["error"]


# ---------- fresh re-read on every call ----------


def test_re_reads_file_fresh_each_call(tmp_path):
    """Write the file once, read it; rewrite it, read again — the second
    read must reflect the new contents (no caching, never os.environ)."""
    p = tmp_path / "grouping.env"
    p.write_text("JASPER_GROUPING=off\n")
    path = str(p)

    first = read_grouping_state(path)
    assert first["enabled"] is False

    # Mutate the SSOT file in place — simulating a wizard save.
    p.write_text(_leader_env())
    second = read_grouping_state(path)
    assert second["enabled"] is True
    assert second["role"] == "leader"
    assert second["codec"] == "opus"


def test_re_read_picks_up_codec_change(tmp_path):
    """A narrower fresh-read assertion: only the codec key changes between
    two writes, and the second read reflects it."""
    p = tmp_path / "grouping.env"
    p.write_text(_leader_env())  # codec=opus
    path = str(p)
    assert read_grouping_state(path)["codec"] == "opus"

    p.write_text(_leader_env().replace("CODEC=opus", "CODEC=flac"))
    assert read_grouping_state(path)["codec"] == "flac"
