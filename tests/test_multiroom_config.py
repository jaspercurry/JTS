"""Unit tests for jasper.multiroom.config.

Pure logic — no I/O beyond the env file the loader reads. Exercises the
off-by-default fail-safe path, the fail-LOUD configured-but-invalid path,
buffer clamping, and the master-toggle parse (case/whitespace).

Mirrors the house style in tests/test_peering_config.py: tmp_path-written
env file, no network/subprocess, plain asserts.
"""
from __future__ import annotations

import pytest

from jasper.multiroom.config import (
    ALLOWED_CODECS,
    DEFAULT_BUFFER_MS,
    DEFAULT_CLIENT_LATENCY_MS,
    DEFAULT_CODEC,
    is_enabled,
    load_config,
    validate_grouping,
)


# ---------- validate_grouping: the shared rule (load_config + endpoint) ----


def test_validate_grouping_valid_leader_and_follower():
    assert validate_grouping(
        role="leader", channel="left", bond_id="lr", leader_addr="",
    ) is None
    assert validate_grouping(
        role="follower", channel="right", bond_id="lr", leader_addr="10.0.0.7",
    ) is None


def test_validate_grouping_missing_bond_id():
    assert "BOND_ID" in validate_grouping(
        role="leader", channel="left", bond_id="", leader_addr="",
    )


def test_validate_grouping_bad_channel():
    assert "CHANNEL" in validate_grouping(
        role="leader", channel="surround", bond_id="lr", leader_addr="",
    )


def test_validate_grouping_bad_role():
    assert "ROLE" in validate_grouping(
        role="boss", channel="left", bond_id="lr", leader_addr="",
    )


def test_validate_grouping_follower_needs_leader_addr():
    assert "LEADER_ADDR" in validate_grouping(
        role="follower", channel="right", bond_id="lr", leader_addr="",
    )


def test_validate_grouping_bad_codec():
    assert "CODEC" in validate_grouping(
        role="leader", channel="left", bond_id="lr", leader_addr="",
        codec="mp3",
    )


# ---------- helpers ----------


def _write_env(tmp_path, body: str):
    """Write a grouping.env under tmp_path and return its str path."""
    p = tmp_path / "grouping.env"
    p.write_text(body)
    return str(p)


def _leader_env() -> str:
    """A minimal valid ENABLED leader config body."""
    return (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
    )


# ---------- absent / unreadable file ----------


def test_absent_file_is_disabled_with_defaults(tmp_path):
    """A missing file resolves to the all-off, no-error config and never
    raises. This is the load-bearing default for a solo speaker.
    """
    cfg = load_config(str(tmp_path / "does-not-exist.env"))
    assert cfg.enabled is False
    assert cfg.role == ""
    assert cfg.channel == "stereo"
    assert cfg.bond_id == ""
    assert cfg.leader_addr == ""
    assert cfg.buffer_ms == DEFAULT_BUFFER_MS
    assert cfg.client_latency_ms == DEFAULT_CLIENT_LATENCY_MS
    assert cfg.left_delay_ms == 0.0
    assert cfg.right_delay_ms == 0.0
    assert cfg.error is None


def test_absent_file_never_raises(tmp_path):
    """Loading a nonexistent path is total — no exception."""
    # Should not raise.
    load_config(str(tmp_path / "nope" / "grouping.env"))


# ---------- master toggle: off / missing / garbage => fail-safe ----------


def test_explicit_off_is_disabled_no_error(tmp_path):
    path = _write_env(tmp_path, "JASPER_GROUPING=off\n")
    cfg = load_config(path)
    assert cfg.enabled is False
    assert cfg.error is None


def test_missing_toggle_is_disabled_no_error(tmp_path):
    """File present but no JASPER_GROUPING key => off, no error."""
    path = _write_env(tmp_path, "JASPER_GROUPING_ROLE=leader\n")
    cfg = load_config(path)
    assert cfg.enabled is False
    assert cfg.error is None


@pytest.mark.parametrize("value", ["garbage", "1", "true", "yes", "enabled", "  ", "of"])
def test_garbage_toggle_is_disabled_no_error(tmp_path, value):
    """Any non-"on" value fails SAFE to disabled with no error — a broken
    toggle must never silently leave grouping ON.
    """
    path = _write_env(tmp_path, f"JASPER_GROUPING={value}\n")
    cfg = load_config(path)
    assert cfg.enabled is False
    assert cfg.error is None


# ---------- master toggle: case-insensitivity + whitespace ----------


@pytest.mark.parametrize("value", ["on", "ON", "On", " on ", "\ton\t", "oN "])
def test_toggle_on_case_and_whitespace_insensitive(tmp_path, value):
    """The toggle is trimmed and lowercased before comparison."""
    body = _leader_env().replace("JASPER_GROUPING=on", f"JASPER_GROUPING={value}")
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.error is None


# ---------- enabled + valid ----------


def test_enabled_valid_leader_parses(tmp_path):
    path = _write_env(tmp_path, _leader_env())
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.role == "leader"
    assert cfg.channel == "left"
    assert cfg.bond_id == "living-room"
    assert cfg.error is None


def test_enabled_valid_follower_parses(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_CHANNEL=right\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_LEADER_ADDR=192.168.1.50\n"
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.role == "follower"
    assert cfg.channel == "right"
    assert cfg.bond_id == "living-room"
    assert cfg.leader_addr == "192.168.1.50"
    assert cfg.error is None


def test_enabled_channel_defaults_to_stereo(tmp_path):
    """An ENABLED config with no channel key falls back to stereo (valid)."""
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_BOND_ID=kitchen\n"
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.channel == "stereo"
    assert cfg.error is None


# ---------- enabled + INVALID => fail LOUD (enabled stays True) ----------


def test_invalid_empty_bond_id_sets_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        # no bond id
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.error is not None
    assert "BOND_ID" in cfg.error


def test_invalid_bad_channel_sets_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=surround\n"
        "JASPER_GROUPING_BOND_ID=den\n"
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.error is not None
    assert "CHANNEL" in cfg.error


def test_invalid_bad_role_sets_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=boss\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=den\n"
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.error is not None
    assert "ROLE" in cfg.error


def test_invalid_empty_role_sets_error(tmp_path):
    """Role is required when enabled — empty string is not in ALLOWED_ROLES."""
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=den\n"
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.error is not None
    assert "ROLE" in cfg.error


def test_invalid_follower_without_leader_addr_sets_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_CHANNEL=right\n"
        "JASPER_GROUPING_BOND_ID=den\n"
        # no leader addr
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.role == "follower"
    assert cfg.error is not None
    assert "LEADER_ADDR" in cfg.error


# ---------- buffer_ms: default / clamp / passthrough ----------


def test_buffer_ms_default_when_absent(tmp_path):
    path = _write_env(tmp_path, _leader_env())
    cfg = load_config(path)
    assert cfg.buffer_ms == DEFAULT_BUFFER_MS


def test_buffer_ms_default_when_non_int(tmp_path):
    body = _leader_env() + "JASPER_GROUPING_BUFFER_MS=notanumber\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.buffer_ms == DEFAULT_BUFFER_MS


def test_buffer_ms_clamped_below_floor(tmp_path):
    body = _leader_env() + "JASPER_GROUPING_BUFFER_MS=10\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.buffer_ms == 150


def test_buffer_ms_clamped_above_ceiling(tmp_path):
    body = _leader_env() + "JASPER_GROUPING_BUFFER_MS=99999\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.buffer_ms == 1500


@pytest.mark.parametrize("value", [150, 400, 800, 1500])
def test_buffer_ms_valid_passthrough(tmp_path, value):
    body = _leader_env() + f"JASPER_GROUPING_BUFFER_MS={value}\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.buffer_ms == value


def test_buffer_ms_never_an_error(tmp_path):
    """A bad/out-of-range buffer clamps silently and never sets error."""
    body = _leader_env() + "JASPER_GROUPING_BUFFER_MS=-5\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.buffer_ms == 150
    assert cfg.error is None


def test_client_latency_ms_parse_and_validation_matrix(tmp_path):
    path = _write_env(
        tmp_path,
        _leader_env() + "JASPER_GROUPING_CLIENT_LATENCY_MS=12\n",
    )
    cfg = load_config(path)
    assert cfg.client_latency_ms == 12
    assert cfg.error is None

    path = _write_env(
        tmp_path,
        _leader_env() + "JASPER_GROUPING_CLIENT_LATENCY_MS=later\n",
    )
    cfg = load_config(path)
    assert "CLIENT_LATENCY_MS" in cfg.error

    path = _write_env(
        tmp_path,
        _leader_env() + "JASPER_GROUPING_CLIENT_LATENCY_MS=-1\n",
    )
    cfg = load_config(path)
    assert "must be between" in cfg.error


def test_channel_delay_ms_parse_and_validation_matrix(tmp_path):
    path = _write_env(
        tmp_path,
        _leader_env()
        + "JASPER_GROUPING_LEFT_DELAY_MS=1.25\n"
        + "JASPER_GROUPING_RIGHT_DELAY_MS=0.5\n",
    )
    cfg = load_config(path)
    assert cfg.left_delay_ms == 1.25
    assert cfg.right_delay_ms == 0.5
    assert cfg.error is None

    path = _write_env(
        tmp_path,
        _leader_env() + "JASPER_GROUPING_LEFT_DELAY_MS=fast\n",
    )
    cfg = load_config(path)
    assert "LEFT_DELAY_MS" in cfg.error

    path = _write_env(
        tmp_path,
        _leader_env() + "JASPER_GROUPING_RIGHT_DELAY_MS=-0.1\n",
    )
    cfg = load_config(path)
    assert "must be between" in cfg.error


# ---------- codec: default / valid / invalid ----------


def test_codec_default_when_absent(tmp_path):
    """An ENABLED config with no codec key falls back to the flac default."""
    path = _write_env(tmp_path, _leader_env())
    cfg = load_config(path)
    assert cfg.codec == DEFAULT_CODEC
    assert cfg.codec == "flac"
    assert cfg.error is None


def test_default_codec_const_is_flac():
    assert DEFAULT_CODEC == "flac"


def test_allowed_codecs_const():
    assert ALLOWED_CODECS == ("pcm", "flac", "opus")


@pytest.mark.parametrize("codec", ["pcm", "flac", "opus"])
def test_valid_codec_passthrough(tmp_path, codec):
    body = _leader_env() + f"JASPER_GROUPING_CODEC={codec}\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.codec == codec
    assert cfg.error is None


def test_codec_empty_falls_back_to_default(tmp_path):
    """An empty codec value on an enabled config is the default, not an error."""
    body = _leader_env() + "JASPER_GROUPING_CODEC=\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.codec == DEFAULT_CODEC
    assert cfg.error is None


def test_invalid_bad_codec_sets_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=den\n"
        "JASPER_GROUPING_CODEC=mp3\n"
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is True
    assert cfg.error is not None
    assert "CODEC" in cfg.error


def test_bad_codec_not_an_error_when_disabled(tmp_path):
    """Validation only fires when enabled — a bad codec in an off file is moot."""
    body = "JASPER_GROUPING=off\nJASPER_GROUPING_CODEC=mp3\n"
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.enabled is False
    assert cfg.error is None


def test_codec_validation_ordered_after_channel(tmp_path):
    """A config with BOTH a bad channel and a bad codec reports the channel
    error first — codec validation is ordered after channel in the cascade.
    """
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=surround\n"   # bad channel
        "JASPER_GROUPING_BOND_ID=den\n"
        "JASPER_GROUPING_CODEC=mp3\n"          # also bad codec
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.error is not None
    assert "CHANNEL" in cfg.error
    assert "CODEC" not in cfg.error


def test_codec_validation_ordered_before_role(tmp_path):
    """A config with BOTH a bad codec and a bad role reports the codec error
    first — codec validation is ordered before role in the cascade.
    """
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=boss\n"          # bad role
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=den\n"
        "JASPER_GROUPING_CODEC=mp3\n"          # bad codec
    )
    path = _write_env(tmp_path, body)
    cfg = load_config(path)
    assert cfg.error is not None
    assert "CODEC" in cfg.error
    assert "ROLE" not in cfg.error


# ---------- is_enabled() mirrors load_config().enabled ----------


def test_is_enabled_matches_load_config_disabled(tmp_path):
    path = _write_env(tmp_path, "JASPER_GROUPING=off\n")
    assert is_enabled(path) == load_config(path).enabled
    assert is_enabled(path) is False


def test_is_enabled_matches_load_config_enabled(tmp_path):
    path = _write_env(tmp_path, _leader_env())
    assert is_enabled(path) == load_config(path).enabled
    assert is_enabled(path) is True


def test_is_enabled_true_for_configured_but_invalid(tmp_path):
    """A configured-but-broken bond is still enabled (the fail-LOUD state);
    is_enabled() tracks enabled, not validity.
    """
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        # no bond id => invalid
    )
    path = _write_env(tmp_path, body)
    assert is_enabled(path) is True
    assert load_config(path).error is not None


def test_is_enabled_absent_file(tmp_path):
    assert is_enabled(str(tmp_path / "missing.env")) is False


def test_validate_grouping_leader_addr_shape_gate():
    """leader_addr feeds THREE consumers (snapclient argv, the control-API
    volume forward's URL build, the landing page's leader link) — a value
    with '/', '@', or whitespace would reshape a URL rather than name a
    host, so validation enforces hostname/IPv4 shape (same alphabet as the
    landing page's HOST_RE gate)."""
    from jasper.multiroom.config import validate_grouping

    def err(addr):
        return validate_grouping(
            role="follower", channel="right", bond_id="b", leader_addr=addr,
        )

    assert err("jts.local") is None
    assert err("jts.local.") is None        # FQDN trailing dot
    assert err("192.168.1.9") is None
    assert err("speaker-2") is None
    for bad in ("evil.com/x", "user@host", "jts local", "http://jts.local",
                "[::1]"):
        assert err(bad) is not None, bad
        assert "hostname or IPv4" in err(bad)


def test_follower_leader_addr_predicate():
    """The ONE active-bonded-follower predicate behind every pair-forward
    gate (control server + voice tools) — composed from is_active_member
    so bond-validity semantics live in one place."""
    from jasper.multiroom.config import GroupingConfig, follower_leader_addr

    def cfg(**kw):
        base = dict(enabled=True, role="follower", channel="right",
                    bond_id="b", leader_addr="jts.local", buffer_ms=400,
                    codec="flac", error=None)
        base.update(kw)
        return GroupingConfig(**base)

    assert follower_leader_addr(cfg()) == "jts.local"
    assert follower_leader_addr(cfg(role="leader", leader_addr="")) is None
    assert follower_leader_addr(cfg(enabled=False)) is None
    assert follower_leader_addr(cfg(error="broken")) is None
    assert follower_leader_addr(cfg(leader_addr="")) is None


def test_trim_db_parse_and_validation_matrix():
    """Pair-balance trim: attenuate-only (the LOUDER speaker comes down),
    floored at -24 (deeper means misconfigured, not unbalanced), absent
    -> 0.0, garbage -> fail-LOUD error."""
    from jasper.multiroom.config import load_config
    import tempfile
    import os

    def cfg_for(extra: str):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False) as f:
            f.write(
                "JASPER_GROUPING=on\n"
                "JASPER_GROUPING_ROLE=follower\n"
                "JASPER_GROUPING_CHANNEL=right\n"
                "JASPER_GROUPING_BOND_ID=b\n"
                "JASPER_GROUPING_LEADER_ADDR=jts.local\n" + extra
            )
            path = f.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    assert cfg_for("").trim_db == 0.0
    assert cfg_for("JASPER_GROUPING_TRIM_DB=-3.5\n").trim_db == -3.5
    assert cfg_for("JASPER_GROUPING_TRIM_DB=-3.5\n").error is None
    assert "must be between" in cfg_for("JASPER_GROUPING_TRIM_DB=1.0\n").error
    assert "must be between" in cfg_for("JASPER_GROUPING_TRIM_DB=-30\n").error
    assert "not a number" in cfg_for("JASPER_GROUPING_TRIM_DB=loud\n").error


def test_peer_roster_parse_and_validation_matrix():
    """Bond roster (leader records its pair sibling): parsed from the
    env file; peer_addr must be a private/loopback IPv4 (the
    cross-speaker control calls are IP-only by SSRF design); peer_name
    is bounded printable text; both absent -> empty (legacy bonds)."""
    from jasper.multiroom.config import load_config, validate_grouping
    import tempfile
    import os

    def cfg_for(extra: str):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False) as f:
            f.write(
                "JASPER_GROUPING=on\n"
                "JASPER_GROUPING_ROLE=leader\n"
                "JASPER_GROUPING_CHANNEL=left\n"
                "JASPER_GROUPING_BOND_ID=b\n" + extra
            )
            path = f.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    cfg = cfg_for("")
    assert cfg.peer_addr == "" and cfg.peer_name == ""
    cfg = cfg_for("JASPER_GROUPING_PEER_ADDR=192.168.1.9\n"
                  "JASPER_GROUPING_PEER_NAME=JTS3\n")
    assert cfg.peer_addr == "192.168.1.9"
    assert cfg.peer_name == "JTS3"
    assert cfg.error is None
    assert "private/loopback" in cfg_for(
        "JASPER_GROUPING_PEER_ADDR=8.8.8.8\n").error
    assert "private/loopback" in cfg_for(
        "JASPER_GROUPING_PEER_ADDR=jts3.local\n").error

    base = dict(role="leader", channel="left", bond_id="b", leader_addr="")
    assert validate_grouping(**base, peer_name="x" * 65) is not None
    assert validate_grouping(**base, peer_name="ok name") is None
