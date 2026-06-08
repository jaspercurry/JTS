"""Unit tests for jasper.multiroom.state.read_grouping_state.

The state reader is the fresh-read SSOT projection consumed by
jasper-control's /state aggregator. These tests assert it (a) never reads
os.environ, (b) re-reads the file on every call so a wizard save shows up
immediately, (c) returns a JSON-able dict mirroring GroupingConfig, and
(d) is total — a missing/malformed file resolves to the disabled snapshot
without raising.

Mirrors the house style in tests/test_multiroom_config.py: tmp_path-written
env file, no network/subprocess, plain asserts. Runtime-health tests inject
a fake unit-state reader (``unit_state_reader=``) so they never shell out to
``systemctl`` and stay deterministic; the pure ``derive_grouping_runtime`` is
tested directly with synthetic unit states.
"""
from __future__ import annotations

from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC, load_config
from jasper.multiroom.state import (
    derive_grouping_runtime,
    read_grouping_state,
)


# ---------- helpers ----------


def _write_env(tmp_path, body: str) -> str:
    p = tmp_path / "grouping.env"
    p.write_text(body)
    return str(p)


def _stub(units):
    """Fake unit-state reader: pretend every probed unit is active. Lets
    config-focused tests exercise the enabled path without a subprocess."""
    return {u: "active" for u in units}


def _leader_env() -> str:
    return (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_CODEC=opus\n"
    )


def _follower_env() -> str:
    return (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_CHANNEL=right\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_LEADER_ADDR=192.168.1.50\n"
    )


SNAPSERVER = "jasper-snapserver.service"
SNAPCLIENT = "jasper-snapclient.service"


def _cfg(tmp_path, body):
    return load_config(_write_env(tmp_path, body))


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
    state = read_grouping_state(path, unit_state_reader=_stub)
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
    state = read_grouping_state(path, unit_state_reader=_stub)
    assert state["codec"] == DEFAULT_CODEC


def test_state_dict_is_json_able(tmp_path):
    """The dict must round-trip through json (the /state contract)."""
    import json

    path = _write_env(tmp_path, _leader_env())
    state = read_grouping_state(path, unit_state_reader=_stub)
    # Should not raise; round-trips to an equal dict (incl. runtime block).
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

    first = read_grouping_state(path, unit_state_reader=_stub)
    assert first["enabled"] is False

    # Mutate the SSOT file in place — simulating a wizard save.
    p.write_text(_leader_env())
    second = read_grouping_state(path, unit_state_reader=_stub)
    assert second["enabled"] is True
    assert second["role"] == "leader"
    assert second["codec"] == "opus"


def test_re_read_picks_up_codec_change(tmp_path):
    """A narrower fresh-read assertion: only the codec key changes between
    two writes, and the second read reflects it."""
    p = tmp_path / "grouping.env"
    p.write_text(_leader_env())  # codec=opus
    path = str(p)
    assert read_grouping_state(path, unit_state_reader=_stub)["codec"] == "opus"

    p.write_text(_leader_env().replace("CODEC=opus", "CODEC=flac"))
    assert read_grouping_state(path, unit_state_reader=_stub)["codec"] == "flac"


# ---------- runtime health: derive_grouping_runtime (pure) ----------


def test_runtime_off_when_disabled(tmp_path):
    rt = derive_grouping_runtime(_cfg(tmp_path, "JASPER_GROUPING=off\n"), {})
    assert rt["health"] == "off"
    assert rt["units"] == {}


def test_runtime_invalid_carries_error(tmp_path):
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"  # no bond id => invalid
    )
    rt = derive_grouping_runtime(_cfg(tmp_path, body), {})
    assert rt["health"] == "invalid"
    assert "BOND_ID" in rt["detail"]


def test_runtime_leader_ok_when_both_active(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "active", SNAPCLIENT: "active"},
    )
    assert rt["health"] == "ok"
    assert rt["units"][SNAPSERVER] == {"expected": "start", "actual": "active"}
    assert rt["units"][SNAPCLIENT] == {"expected": "start", "actual": "active"}


def test_runtime_leader_degraded_when_server_down(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "failed", SNAPCLIENT: "active"},
    )
    assert rt["health"] == "degraded"
    assert "leader degraded" in rt["detail"]
    assert SNAPSERVER in rt["detail"]


def test_runtime_follower_ok_when_client_active(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _follower_env()),
        {SNAPSERVER: "inactive", SNAPCLIENT: "active"},
    )
    assert rt["health"] == "ok"
    # a follower runs NO server (expected stop), only the client
    assert rt["units"][SNAPSERVER]["expected"] == "stop"
    assert rt["units"][SNAPCLIENT] == {"expected": "start", "actual": "active"}


def test_runtime_follower_degraded_surfaces_unreachable_leader(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _follower_env()),
        {SNAPSERVER: "inactive", SNAPCLIENT: "failed"},
    )
    assert rt["health"] == "degraded"
    assert "follower not connected" in rt["detail"]
    assert "192.168.1.50" in rt["detail"]  # the leader addr, for diagnosis
    assert "failed" in rt["detail"]


def test_runtime_missing_unit_state_is_not_active(tmp_path):
    # The reader returned nothing for a unit -> treated as not-active.
    rt = derive_grouping_runtime(_cfg(tmp_path, _follower_env()), {})
    assert rt["health"] == "degraded"
    assert rt["units"][SNAPCLIENT]["actual"] == "unknown"


# ---------- read_grouping_state runtime wiring ----------


def test_disabled_has_no_runtime_block_and_never_probes(tmp_path):
    probed = []

    def spy(units):
        probed.append(list(units))
        return _stub(units)

    state = read_grouping_state(
        _write_env(tmp_path, "JASPER_GROUPING=off\n"), unit_state_reader=spy
    )
    assert "runtime" not in state
    assert probed == []  # zero subprocess on a solo speaker


def test_enabled_valid_probes_the_planned_units(tmp_path):
    probed = []

    def spy(units):
        probed.append(list(units))
        return {u: "active" for u in units}

    state = read_grouping_state(
        _write_env(tmp_path, _follower_env()), unit_state_reader=spy
    )
    assert state["runtime"]["health"] == "ok"
    assert SNAPCLIENT in probed[0]


def test_enabled_invalid_does_not_probe(tmp_path):
    probed = []

    def spy(units):
        probed.append(list(units))
        return _stub(units)

    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"  # no bond id => invalid
    )
    state = read_grouping_state(_write_env(tmp_path, body), unit_state_reader=spy)
    assert state["runtime"]["health"] == "invalid"
    assert probed == []  # an invalid bond starts nothing -> nothing to probe


def test_real_reader_is_failsoft_without_systemctl(monkeypatch):
    # The real systemctl reader must never raise — a wedged/absent systemd
    # resolves every unit to "unknown", keeping /state alive.
    from jasper.multiroom import state as state_mod

    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(state_mod.subprocess, "run", boom)
    out = state_mod.read_unit_active_states([SNAPSERVER, SNAPCLIENT])
    assert out == {SNAPSERVER: "unknown", SNAPCLIENT: "unknown"}
