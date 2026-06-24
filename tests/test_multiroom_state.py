# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
    GROUPING_RESPONSE_KEY,
    derive_grouping_runtime,
    grouping_response,
    parse_grouping_response,
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


# Fake outputd-tap reader: pretend the leader IS tapping. Lets config-focused
# leader tests exercise the enabled path without touching a real /run file.
_SNAPFIFO = "/run/jasper-snapserver/snapfifo"


def _tap_set():
    return _SNAPFIFO


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
    "trim_db", "peer_addr", "peer_name", "roster",
    "mains_highpass_enabled", "subwoofer_present",
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
    state = read_grouping_state(
        path, unit_state_reader=_stub, tap_path_reader=_tap_set
    )
    assert state["enabled"] is True
    assert state["role"] == "leader"
    assert state["channel"] == "left"
    assert state["bond_id"] == "living-room"
    assert state["codec"] == "opus"
    assert state["error"] is None


def test_sub_bond_surfaces_crossover_hz(tmp_path):
    """A sub bond's snapshot carries crossover_hz (fresh-read from the SSOT);
    a plain non-sub bond omits it."""
    sub_env = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=follower\n"
        "JASPER_GROUPING_CHANNEL=sub\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_LEADER_ADDR=192.168.1.50\n"
        "JASPER_GROUPING_CROSSOVER_HZ=110\n"
    )
    state = read_grouping_state(
        _write_env(tmp_path, sub_env), unit_state_reader=_stub
    )
    assert state["channel"] == "sub"
    assert state["crossover_hz"] == 110.0

    main_with_sub = read_grouping_state(
        _write_env(
            tmp_path,
            _follower_env()
            + "JASPER_GROUPING_SUBWOOFER_PRESENT=on\n"
            + "JASPER_GROUPING_CROSSOVER_HZ=95\n",
        ),
        unit_state_reader=_stub,
    )
    assert main_with_sub["channel"] == "right"
    assert main_with_sub["subwoofer_present"] is True
    assert main_with_sub["crossover_hz"] == 95.0

    non_sub = read_grouping_state(
        _write_env(tmp_path, _follower_env()), unit_state_reader=_stub
    )
    assert non_sub["channel"] == "right"
    assert "crossover_hz" not in non_sub


def test_leader_roster_surfaces_as_list(tmp_path):
    """A leader cfg with a 2-member roster surfaces snapshot["roster"] as a
    list of {addr,name,channel}; a solo/empty config surfaces []."""
    leader_with_roster = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=living-room\n"
        "JASPER_GROUPING_CROSSOVER_HZ=90\n"
        "JASPER_GROUPING_ROSTER=192.168.1.7|Right|right,192.168.1.8|Sub|sub\n"
    )
    state = read_grouping_state(
        _write_env(tmp_path, leader_with_roster),
        unit_state_reader=_stub, tap_path_reader=_tap_set,
    )
    assert state["roster"] == [
        {"addr": "192.168.1.7", "name": "Right", "channel": "right"},
        {"addr": "192.168.1.8", "name": "Sub", "channel": "sub"},
    ]
    assert state["subwoofer_present"] is True
    assert state["mains_highpass_enabled"] is True
    assert state["crossover_hz"] == 90.0

    # Solo / disabled config: empty roster list.
    solo = read_grouping_state(str(tmp_path / "missing.env"))
    assert solo["roster"] == []
    assert solo["subwoofer_present"] is False


def test_valid_enabled_codec_defaults_to_flac(tmp_path):
    """An enabled config with no codec key surfaces the default codec."""
    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"
        "JASPER_GROUPING_BOND_ID=kitchen\n"
    )
    path = _write_env(tmp_path, body)
    state = read_grouping_state(
        path, unit_state_reader=_stub, tap_path_reader=_tap_set
    )
    assert state["codec"] == DEFAULT_CODEC


def test_state_dict_is_json_able(tmp_path):
    """The dict must round-trip through json (the /state contract)."""
    import json

    path = _write_env(tmp_path, _leader_env())
    state = read_grouping_state(
        path, unit_state_reader=_stub, tap_path_reader=_tap_set
    )
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
    second = read_grouping_state(
        path, unit_state_reader=_stub, tap_path_reader=_tap_set
    )
    assert second["enabled"] is True
    assert second["role"] == "leader"
    assert second["codec"] == "opus"


def test_re_read_picks_up_codec_change(tmp_path):
    """A narrower fresh-read assertion: only the codec key changes between
    two writes, and the second read reflects it."""
    p = tmp_path / "grouping.env"
    p.write_text(_leader_env())  # codec=opus
    path = str(p)
    assert read_grouping_state(
        path, unit_state_reader=_stub, tap_path_reader=_tap_set
    )["codec"] == "opus"

    p.write_text(_leader_env().replace("CODEC=opus", "CODEC=flac"))
    assert read_grouping_state(
        path, unit_state_reader=_stub, tap_path_reader=_tap_set
    )["codec"] == "flac"


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


def test_runtime_leader_ok_when_both_active_and_tapping(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "active", SNAPCLIENT: "active"},
        leader_tap_path="/run/jasper-snapserver/snapfifo",
    )
    assert rt["health"] == "ok"
    assert rt["units"][SNAPSERVER] == {"expected": "start", "actual": "active"}
    assert rt["units"][SNAPCLIENT] == {"expected": "start", "actual": "active"}


def test_runtime_pair_lock_is_unknown_when_clock_lock_unobservable(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "active", SNAPCLIENT: "active"},
        leader_tap_path="/run/jasper-snapserver/snapfifo",
        stream_clients=[{
            "name": "jts", "connected": True, "stream_id": "jts",
            "muted": False, "group_muted": False, "volume_percent": 100,
            "latency_ms": 0,
        }],
        self_name="jts",
        want_stream="jts",
        local_outputd_status={"dac_content": {"enabled": True, "serving_fifo": True}},
    )
    assert rt["health"] == "ok"
    assert rt["pair_lock"]["status"] == "unknown"
    assert rt["pair_lock"]["locked_and_healthy"] is False
    assert rt["pair_lock"]["signals"]["local_fifo"]["bytes_flowing"] is True
    clock = rt["pair_lock"]["signals"]["follower_clock_lock"]
    assert clock["locked"] is None
    assert "does not expose" in clock["detail"]


def test_runtime_pair_lock_degrades_when_fifo_not_serving(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _follower_env()),
        {SNAPSERVER: "inactive", SNAPCLIENT: "active"},
        local_outputd_status={"dac_content": {"enabled": True, "serving_fifo": False}},
    )
    assert rt["health"] == "ok"
    assert rt["pair_lock"]["status"] == "degraded"
    assert "not serving FIFO bytes" in rt["pair_lock"]["detail"]


def test_runtime_pair_lock_distinguishes_group_mute_from_clock_lock(tmp_path):
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "active", SNAPCLIENT: "active"},
        leader_tap_path="/run/jasper-snapserver/snapfifo",
        stream_clients=[{
            "name": "jts", "connected": True, "stream_id": "jts",
            "muted": False, "group_muted": True, "volume_percent": 100,
            "latency_ms": 0,
        }],
        self_name="jts",
        want_stream="jts",
        local_outputd_status={"dac_content": {"enabled": True, "serving_fifo": True}},
    )
    assert rt["health"] == "degraded"
    assert rt["pair_lock"]["status"] == "degraded"
    assert rt["pair_lock"]["signals"]["snapcast_clients"]["muted_or_zero"] == 1


def test_runtime_leader_degraded_when_units_up_but_no_producer(tmp_path):
    """The staff-review gap, generalized: snap units active but nothing
    feeds the snapfifo (empty leader_tap_path —
    the bond apply did not land) => degraded, snapserver reads an empty
    FIFO and followers get silence while /state would otherwise look ok."""
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "active", SNAPCLIENT: "active"},
        leader_tap_path="",
    )
    assert rt["health"] == "degraded"
    assert "does not write the snapserver pipe" in rt["detail"]
    assert "jasper-grouping-reconcile" in rt["detail"]
    # Units themselves are still reported active — the failure is the dry
    # stream source, not a down unit.
    assert rt["units"][SNAPSERVER] == {"expected": "start", "actual": "active"}


def test_runtime_leader_default_tap_is_empty_so_degraded(tmp_path):
    """Tap defaults to "" so a caller that does not inject it gets the
    safe-degraded reading for a leader (cannot silently look ok)."""
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "active", SNAPCLIENT: "active"},
    )
    assert rt["health"] == "degraded"
    assert "does not write the snapserver pipe" in rt["detail"]


def test_runtime_leader_down_unit_wins_over_tap_check(tmp_path):
    """A down snap unit is the more fundamental failure; its detail wins
    over the producer-feed check even when the feed is also empty."""
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _leader_env()),
        {SNAPSERVER: "failed", SNAPCLIENT: "active"},
        leader_tap_path="",
    )
    assert rt["health"] == "degraded"
    assert "leader degraded" in rt["detail"]
    assert "no music producer" not in rt["detail"]


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


def test_runtime_follower_tap_argument_is_ignored(tmp_path):
    """A follower has no producer concept: an empty leader_tap_path must
    NOT push it to degraded (the producer-feed check is leader-only)."""
    rt = derive_grouping_runtime(
        _cfg(tmp_path, _follower_env()),
        {SNAPSERVER: "inactive", SNAPCLIENT: "active"},
        leader_tap_path="",
    )
    assert rt["health"] == "ok"
    assert "no music producer" not in rt["detail"]


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


def _healthy_stream(name=None):
    """A snapserver registry where this leader's client is connected,
    bound to our stream, audible — the all-clear stream probe. The
    client name defaults to THIS machine's hostname because the derive's
    own-client check compares against socket.gethostname()."""
    import socket

    name = name or socket.gethostname().strip()
    return lambda: [{
        "group_id": "g1", "stream_id": "jts", "name": name,
        "connected": True, "muted": False, "volume_percent": 100,
    }]


def test_leader_tap_set_is_ok(tmp_path):
    """Valid leader, units active, a LIVE producer feed (the future
    Increment 3–5 shape, exercised via the injected reader) => ok."""
    tapped = []

    def tap_spy():
        tapped.append(True)
        return _SNAPFIFO

    state = read_grouping_state(
        _write_env(tmp_path, _leader_env()),
        unit_state_reader=_stub,
        tap_path_reader=tap_spy,
        stream_clients_reader=_healthy_stream(),
    )
    assert state["runtime"]["health"] == "ok"
    assert "leader streaming" in state["runtime"]["detail"]
    assert tapped == [True]  # leader DOES consult the injected feed reader


def test_read_grouping_state_threads_local_outputd_status_to_pair_lock(tmp_path):
    state = read_grouping_state(
        _write_env(tmp_path, _follower_env()),
        unit_state_reader=_stub,
        local_outputd_reader=lambda: {
            "dac_content": {"enabled": True, "serving_fifo": True},
        },
    )
    fifo = state["runtime"]["pair_lock"]["signals"]["local_fifo"]
    assert fifo["available"] is True
    assert fifo["bytes_flowing"] is True
    assert "not a clock-lock" in fifo["meaning"]


def test_read_grouping_state_outputd_status_reader_is_failsoft(tmp_path):
    def boom():
        raise RuntimeError("outputd socket exploded")

    state = read_grouping_state(
        _write_env(tmp_path, _follower_env()),
        unit_state_reader=_stub,
        local_outputd_reader=boom,
    )
    fifo = state["runtime"]["pair_lock"]["signals"]["local_fifo"]
    assert fifo["available"] is False
    assert state["runtime"]["health"] == "ok"


def test_leader_tap_empty_is_degraded(tmp_path):
    """Valid leader, units active, nothing feeding the FIFO => degraded
    with the un-piped-config reason — the staff-review gap, generalized."""
    state = read_grouping_state(
        _write_env(tmp_path, _leader_env()),
        unit_state_reader=_stub,
        tap_path_reader=lambda: "",
    )
    assert state["runtime"]["health"] == "degraded"
    assert "does not write the snapserver pipe" in state["runtime"]["detail"]


def test_leader_without_injected_reader_is_degraded(tmp_path, monkeypatch):
    """PRODUCTION default: tap_path_reader falls back to
    active_leader_pipe_path (Increment 5 — the ACTIVE CamillaDSP config
    scanned for the pipe sink). With no pipe-wired config on this
    machine the leader honestly reads degraded, never a false-green
    "streaming". Pin the statefile to a missing path so a dev machine
    with real CamillaDSP state can't flip the result."""
    monkeypatch.setenv(
        "JASPER_CAMILLA_STATEFILE", str(tmp_path / "no-statefile.yml"),
    )
    state = read_grouping_state(
        _write_env(tmp_path, _leader_env()),
        unit_state_reader=_stub,
    )
    assert state["runtime"]["health"] == "degraded"
    assert "does not write the snapserver pipe" in state["runtime"]["detail"]


def test_leader_with_pipe_wired_config_is_ok(tmp_path, monkeypatch):
    """The Increment 5 happy path end-to-end through PRODUCTION wiring:
    statefile -> active config -> pipe scan -> ok. The active config is a
    REAL emit_sound_config artifact (emitter/scanner drift fails here)."""
    from jasper.multiroom.reconcile import SNAPFIFO
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    config = tmp_path / "grouping_leader.yml"
    config.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            enable_rate_adjust=False,
            playback_pipe_path=SNAPFIFO,
        )
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    state = read_grouping_state(
        _write_env(tmp_path, _leader_env()),
        unit_state_reader=_stub,
        stream_clients_reader=_healthy_stream(),
    )
    assert state["runtime"]["health"] == "ok"
    assert "leader streaming" in state["runtime"]["detail"]


def test_follower_does_not_probe_the_tap(tmp_path):
    """A follower has no tap concept: read_grouping_state must NOT call the
    tap reader (zero extra work), and stays ok with the client active."""
    def boom():
        raise AssertionError("tap reader must not be called for a follower")

    state = read_grouping_state(
        _write_env(tmp_path, _follower_env()),
        unit_state_reader=_stub,
        tap_path_reader=boom,
    )
    assert state["runtime"]["health"] == "ok"


def test_solo_does_not_probe_the_tap(tmp_path):
    """Grouping off => no runtime block and the tap reader is never called."""
    def boom():
        raise AssertionError("tap reader must not be called when grouping off")

    state = read_grouping_state(
        _write_env(tmp_path, "JASPER_GROUPING=off\n"),
        unit_state_reader=_stub,
        tap_path_reader=boom,
    )
    assert "runtime" not in state


def test_invalid_leader_does_not_probe_the_tap(tmp_path):
    """An invalid (enabled-but-broken) leader starts nothing and taps
    nothing: the tap reader must not be consulted."""
    def boom():
        raise AssertionError("tap reader must not be called for invalid bond")

    body = (
        "JASPER_GROUPING=on\n"
        "JASPER_GROUPING_ROLE=leader\n"
        "JASPER_GROUPING_CHANNEL=left\n"  # no bond id => invalid
    )
    state = read_grouping_state(
        _write_env(tmp_path, body),
        unit_state_reader=_stub,
        tap_path_reader=boom,
    )
    assert state["runtime"]["health"] == "invalid"


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


# ---------- GET /grouping wire contract (producer/consumer can't drift) ----


def test_grouping_response_parse_roundtrip():
    """parse(build(x)) == x. This is the guard that keeps jasper-control (the
    producer) and the /rooms /unbond consumer from drifting — both go through
    these paired functions sharing GROUPING_RESPONSE_KEY. The C4 regression
    (2026-06-09) was exactly this contract drifting: the producer nested under
    a 'grouping' key while the consumer read bond_id at the top level."""
    snap = {
        "enabled": True, "role": "follower", "channel": "right",
        "bond_id": "bond-1", "leader_addr": "jts.local",
    }
    built = grouping_response(snap)
    assert built == {GROUPING_RESPONSE_KEY: snap}
    assert parse_grouping_response(built) == snap


def test_parse_grouping_response_unknown_cases_are_none():
    """Absent / null / non-dict grouping reads as None ('unknown'), so a
    failed read can never spuriously match a bond_id. Mirrors the fail-soft
    {'grouping': null} the producer emits when read_grouping_state() raises."""
    assert parse_grouping_response(grouping_response(None)) is None  # fail-soft read
    assert parse_grouping_response({}) is None                       # key absent
    assert parse_grouping_response({GROUPING_RESPONSE_KEY: None}) is None   # explicit null
    assert parse_grouping_response({GROUPING_RESPONSE_KEY: "x"}) is None    # not a dict
    assert parse_grouping_response("not a dict") is None             # body not a dict
    assert parse_grouping_response(None) is None


# ---------- stream-client truthing (the 2026-06-11 silent-bond classes) ----


def _stream_rows(**overrides):
    import socket

    row = {
        "group_id": "g1", "stream_id": "jts",
        "name": socket.gethostname().strip(),
        "connected": True, "muted": False, "volume_percent": 100,
    }
    row.update(overrides)
    return [row]


def _leader_runtime_with_stream(tmp_path, stream_clients):
    return read_grouping_state(
        _write_env(tmp_path, _leader_env()),
        unit_state_reader=_stub,
        tap_path_reader=lambda: _SNAPFIFO,
        stream_clients_reader=lambda: stream_clients,
    )["runtime"]


def test_stream_truth_wrong_binding_is_degraded(tmp_path):
    """THE incident class: a connected client bound to a stale stream
    hears silence while every unit is green."""
    rt = _leader_runtime_with_stream(
        tmp_path, _stream_rows(stream_id="default"),
    )
    assert rt["health"] == "degraded"
    assert "bound to stream default" in rt["detail"]
    assert "jasper-grouping-reconcile" in rt["detail"]


def test_stream_truth_muted_or_zero_volume_is_degraded(tmp_path):
    """snapclient's software mixer scales samples by the registry
    volume — muted/0% plays zeros behind green health."""
    rt = _leader_runtime_with_stream(tmp_path, _stream_rows(muted=True))
    assert rt["health"] == "degraded"
    assert "muted or at volume 0" in rt["detail"]
    rt = _leader_runtime_with_stream(
        tmp_path, _stream_rows(volume_percent=0),
    )
    assert rt["health"] == "degraded"


def test_stream_truth_own_client_missing_is_degraded(tmp_path):
    """The leader must hear itself: its own snapclient absent from the
    registry (or disconnected) means a silent leader."""
    rt = _leader_runtime_with_stream(
        tmp_path, _stream_rows(name="someone-else"),
    )
    assert rt["health"] == "degraded"
    assert "own snapclient" in rt["detail"]
    rt = _leader_runtime_with_stream(
        tmp_path, _stream_rows(connected=False),
    )
    assert rt["health"] == "degraded"


def test_stream_truth_unreachable_rpc_is_degraded_not_skipped(tmp_path):
    """An unverifiable bond is a degraded bond — RPC failure maps to an
    explicit verdict, never a silent skip."""
    rt = read_grouping_state(
        _write_env(tmp_path, _leader_env()),
        unit_state_reader=_stub,
        tap_path_reader=lambda: _SNAPFIFO,
        stream_clients_reader=lambda: None,  # reader fail-soft → None
    )["runtime"]
    assert rt["health"] == "degraded"
    assert "snapserver RPC unreachable" in rt["detail"]


def test_stream_truth_disconnected_wrong_binding_does_not_degrade(tmp_path):
    """A DISCONNECTED client's stale binding is the reconciler pin's job
    (it re-binds persisted groups); health only judges what is live."""
    import socket

    rows = _stream_rows() + [{
        "group_id": "g2", "stream_id": "default", "name": "jts3",
        "connected": False, "muted": False, "volume_percent": 100,
    }]
    rt = _leader_runtime_with_stream(tmp_path, rows)
    assert rt["health"] == "ok"
    assert socket.gethostname  # keep import used


# ---------- active-follower endpoint surface (distributed-active Slice 3) ----------


def test_endpoint_block_present_for_active_crossover_follower(tmp_path):
    """An active follower running its local Layer-A crossover surfaces an
    ``endpoint`` block (mode=active_crossover) for the dashboard."""
    path = _write_env(tmp_path, _follower_env())
    state = read_grouping_state(
        path,
        unit_state_reader=_stub,
        endpoint_status_reader=lambda: {"active_follower": True, "blocked_reason": ""},
    )
    assert state["endpoint"] == {
        "mode": "active_crossover", "role": "follower", "blocked_reason": "",
    }


def test_provision_block_surfaced_when_installing(tmp_path, monkeypatch):
    """While the reconciler installs snapcast (the grouping opt-in), /state.grouping
    carries a `provision` block so the /rooms wizard can show 'Installing Snapcast…'."""
    import jasper.multiroom.provision as prov

    monkeypatch.setattr(
        prov, "read_provision_status",
        lambda *a, **k: {"state": "installing", "detail": "~1-2 min"},
    )
    state = read_grouping_state(
        _write_env(tmp_path, _leader_env()), unit_state_reader=_stub,
    )
    assert state["provision"] == {"state": "installing", "detail": "~1-2 min"}


def test_no_provision_block_for_solo(tmp_path, monkeypatch):
    """Gated on cfg.enabled — a solo speaker carries no provision key even if a
    stale status file exists (solo snapshot stays byte-identical)."""
    import jasper.multiroom.provision as prov

    monkeypatch.setattr(
        prov, "read_provision_status",
        lambda *a, **k: {"state": "installing", "detail": "x"},
    )
    state = read_grouping_state(
        _write_env(tmp_path, "JASPER_GROUPING=off\n"), unit_state_reader=_stub,
    )
    assert "provision" not in state


def test_endpoint_block_present_for_active_crossover_leader(tmp_path):
    """An active LEADER running camilla#2 (its local Layer-A crossover, while
    camilla#1 bakes the wire) surfaces an ``endpoint`` block tagged role=leader
    (distributed-active Slice 5)."""
    path = _write_env(tmp_path, _leader_env())
    state = read_grouping_state(
        path,
        unit_state_reader=_stub,
        endpoint_status_reader=lambda: {
            "active_follower": False, "active_leader": True, "blocked_reason": "",
        },
    )
    assert state["endpoint"] == {
        "mode": "active_crossover", "role": "leader", "blocked_reason": "",
    }


def test_endpoint_block_surfaces_fail_closed_block_reason(tmp_path):
    """A REFUSED active-endpoint bond (fell back to solo active) surfaces the
    fail-closed reason — the household-facing 'why it didn't join' signal."""
    path = _write_env(tmp_path, _follower_env())
    state = read_grouping_state(
        path,
        unit_state_reader=_stub,
        endpoint_status_reader=lambda: {
            "active_follower": False, "blocked_reason": "graph_unprovable",
        },
    )
    assert state["endpoint"] == {
        "mode": "blocked", "role": "", "blocked_reason": "graph_unprovable",
    }


def test_no_endpoint_block_for_dumb_member_or_solo(tmp_path):
    """A dumb member (no active-follower status) and a solo speaker carry NO
    endpoint block — the surface is opt-in to active endpoints."""
    follower = read_grouping_state(
        _write_env(tmp_path, _follower_env()),
        unit_state_reader=_stub,
        endpoint_status_reader=lambda: {"active_follower": False, "blocked_reason": ""},
    )
    assert "endpoint" not in follower
    # solo: no status-file read at all, snapshot stays byte-for-byte unchanged.
    solo = read_grouping_state(str(tmp_path / "missing.env"))
    assert "endpoint" not in solo
