# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.multiroom.reconcile.

The reconciler's decision (`plan`), the argv builders, and the args
assembly (`_assemble_args`) are PURE, total functions — no subprocess, no
systemctl, no clock. These tests drive them with synthetic GroupingConfigs
and assert on the returned ReconcilePlan / argv list / env-key values. The
args-file writer (`_write_args_file`) is exercised against a tmp path, and
`main()` is exercised with `_apply` mocked and the args file redirected to
tmp — so the assemble+persist round-trip is asserted without real
systemctl or touching /run. The real systemctl path (`_apply` itself) is
still validated on hardware.

Mirrors the house style in tests/test_peering_state.py: synthetic inputs,
plain asserts; file I/O goes to pytest's tmp_path.
"""
from __future__ import annotations

import os

from jasper.multiroom.config import (
    DEFAULT_BUFFER_MS,
    DEFAULT_CODEC,
    BondMember,
    GroupingConfig,
)
from jasper.multiroom import reconcile as reconcile_mod
from jasper.multiroom.reconcile import (
    AIRPLAY_BONDED_EXTRA_DELAY_ENV,
    SNAPCLIENT_UNIT,
    SNAPFIFO,
    SNAPSERVER_UNIT,
    ReconcilePlan,
    UnitIntent,
    _assemble_args,
    _write_args_file,
    airplay_grouping_env,
    desired_snapfifo_path,
    main,
    plan,
    snapclient_argv,
    snapserver_argv,
)


# ---------- config builders ----------


def _disabled() -> GroupingConfig:
    return GroupingConfig(
        enabled=False,
        role="",
        channel="stereo",
        bond_id="",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )


def _leader(*, channel="left", bond_id="living-room", buffer_ms=DEFAULT_BUFFER_MS,
            codec=DEFAULT_CODEC) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel=channel,
        bond_id=bond_id,
        leader_addr="",
        buffer_ms=buffer_ms,
        codec=codec,
        error=None,
    )


def _follower(*, channel="right", bond_id="living-room", leader_addr="192.168.1.50",
              buffer_ms=DEFAULT_BUFFER_MS, codec=DEFAULT_CODEC) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="follower",
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        buffer_ms=buffer_ms,
        codec=codec,
        error=None,
    )


def _invalid() -> GroupingConfig:
    """Enabled but carrying an error (the fail-LOUD state)."""
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error="JASPER_GROUPING_BOND_ID is empty (grouping is on)",
    )


def _desired(plan_: ReconcilePlan, unit: str) -> str:
    """Pull the single desired state for a unit out of a plan."""
    matches = [i.desired for i in plan_.intents if i.unit == unit]
    assert len(matches) == 1, f"expected exactly one intent for {unit}, got {matches}"
    return matches[0]


# ---------- airplay_grouping_env(): bonded-leader-only offset delta ----------


def test_airplay_grouping_env_leader_adds_snapcast_buffer():
    # An active bonded leader folds its Snapcast playout buffer (400 ms
    # default) into shairport's backend latency offset.
    assert airplay_grouping_env(_leader()) == {
        AIRPLAY_BONDED_EXTRA_DELAY_ENV: "0.400000"
    }


def test_airplay_grouping_env_leader_tracks_buffer_ms():
    assert airplay_grouping_env(_leader(buffer_ms=250)) == {
        AIRPLAY_BONDED_EXTRA_DELAY_ENV: "0.250000"
    }


def test_airplay_grouping_env_follower_is_empty():
    # A follower parks shairport (no AirPlay receiver), so no offset delta.
    assert airplay_grouping_env(_follower()) == {}


def test_airplay_grouping_env_solo_is_empty():
    # INVARIANT: a solo speaker gets NO bonded term — the empty dict clears
    # grouping-airplay.env to the byte-identical solo offset.
    assert airplay_grouping_env(_disabled()) == {}


def test_airplay_grouping_env_invalid_is_empty():
    # A fail-LOUD invalid config is not an active member -> no offset delta.
    assert airplay_grouping_env(_invalid()) == {}


# ---------- plan(): disabled => stop both ----------


def test_plan_disabled_stops_both():
    p = plan(_disabled())
    assert _desired(p, SNAPSERVER_UNIT) == "stop"
    assert _desired(p, SNAPCLIENT_UNIT) == "stop"
    assert "solo" in p.summary


def test_plan_disabled_stops_snap_units_and_restores_renderers():
    """Solo: snap units stop; parked source resources carry RESTORE
    intents (start-only-if-enabled — the un-park after a bond dissolves;
    a no-op on a speaker that was never bonded since the units are
    already running or wizard-disabled)."""
    from jasper.local_sources import local_source_restore_units
    p = plan(_disabled())
    by_unit = {i.unit: i.desired for i in p.intents}
    assert by_unit[SNAPSERVER_UNIT] == "stop"
    assert by_unit[SNAPCLIENT_UNIT] == "stop"
    for u in local_source_restore_units():
        assert by_unit[u] == "restore"
    assert len(p.intents) == 2 + len(local_source_restore_units())


# ---------- plan(): enabled + error => stop both (never start a broken bond) ----------


def test_plan_enabled_but_invalid_stops_both():
    p = plan(_invalid())
    assert _desired(p, SNAPSERVER_UNIT) == "stop"
    assert _desired(p, SNAPCLIENT_UNIT) == "stop"


def test_plan_invalid_summary_surfaces_error_and_not_starting():
    p = plan(_invalid())
    assert "INVALID" in p.summary
    assert "BOND_ID" in p.summary
    assert "not starting" in p.summary


def test_plan_invalid_starts_nothing():
    """Fail-safe: a broken bond must never produce a START intent for the
    snap units — and it RESTORES parked source resources (a broken bond
    must not keep the household's sources parked on top of not playing).
    "restore" is start-only-if-enabled, applied at the I/O layer."""
    p = plan(_invalid())
    snap = {SNAPSERVER_UNIT, SNAPCLIENT_UNIT}
    assert all(i.desired == "stop" for i in p.intents if i.unit in snap)
    assert all(
        i.desired == "restore" for i in p.intents if i.unit not in snap
    )
    assert not any(i.desired == "start" for i in p.intents)


# ---------- plan(): leader => start server + start client ----------


def test_plan_leader_starts_server_and_client():
    p = plan(_leader())
    assert _desired(p, SNAPSERVER_UNIT) == "start"
    assert _desired(p, SNAPCLIENT_UNIT) == "start"


def test_plan_leader_summary_mentions_bond_and_channel():
    p = plan(_leader(channel="left", bond_id="living-room"))
    assert "living-room" in p.summary
    assert "left" in p.summary


# ---------- plan(): follower => stop server + start client ----------


def test_plan_follower_stops_server_starts_client():
    p = plan(_follower())
    assert _desired(p, SNAPSERVER_UNIT) == "stop"
    assert _desired(p, SNAPCLIENT_UNIT) == "start"


def test_plan_follower_summary_mentions_leader_addr():
    p = plan(_follower(leader_addr="10.0.0.7"))
    assert "10.0.0.7" in p.summary


# ---------- plan(): stops-before-starts ordering ----------


def test_plan_intents_ordered_stops_before_starts_leader():
    """Leader has no stops, but the ordering invariant must still hold
    (no start precedes a stop) — trivially true here."""
    p = plan(_leader())
    desireds = [i.desired for i in p.intents]
    assert _stops_before_starts(desireds)


def test_plan_intents_ordered_stops_before_starts_follower():
    """Follower has one stop (server) and one start (client) — the stop
    must come first so a role flip tears down before bringing up."""
    p = plan(_follower())
    desireds = [i.desired for i in p.intents]
    assert _stops_before_starts(desireds)
    # Be explicit: the first intent is the server stop.
    assert p.intents[0].unit == SNAPSERVER_UNIT
    assert p.intents[0].desired == "stop"


def test_plan_intents_ordered_stops_before_starts_disabled():
    p = plan(_disabled())
    assert _stops_before_starts([i.desired for i in p.intents])


def test_plan_intents_ordered_stops_before_starts_invalid():
    p = plan(_invalid())
    assert _stops_before_starts([i.desired for i in p.intents])


def _stops_before_starts(desireds: list[str]) -> bool:
    """True iff no "start" appears before a "stop" in the sequence."""
    seen_start = False
    for d in desireds:
        if d == "start":
            seen_start = True
        elif d == "stop" and seen_start:
            return False
    return True


# ---------- plan(): returned types ----------


def test_plan_returns_reconcileplan_of_unitintents():
    p = plan(_leader())
    assert isinstance(p, ReconcilePlan)
    assert all(isinstance(i, UnitIntent) for i in p.intents)
    assert isinstance(p.summary, str) and p.summary


# ---------- snapserver_argv(): codec + buffer_ms flow into argv ----------


def test_snapserver_argv_includes_codec():
    argv = snapserver_argv(_leader(codec="opus"))
    joined = " ".join(argv)
    assert "codec=opus" in joined


def test_snapserver_argv_includes_buffer_ms():
    argv = snapserver_argv(_leader(buffer_ms=750))
    joined = " ".join(argv)
    assert "buffer_ms=750" in joined


def test_snapserver_argv_reads_the_fifo_source():
    argv = snapserver_argv(_leader())
    joined = " ".join(argv)
    assert SNAPFIFO in joined
    assert "pipe://" in joined


def test_snapserver_argv_starts_with_snapserver():
    argv = snapserver_argv(_leader())
    assert argv[0] == "snapserver"


def test_snapserver_argv_codec_passthrough_all_codecs():
    for codec in ("pcm", "flac", "opus"):
        argv = snapserver_argv(_leader(codec=codec))
        assert f"codec={codec}" in " ".join(argv)


# ---------- snapclient_argv(): host + buffer targeting ----------


def test_snapclient_argv_leader_targets_loopback():
    """The leader runs its own server, so its client targets 127.0.0.1."""
    argv = snapclient_argv(_leader())
    assert "127.0.0.1" in argv


def test_snapclient_argv_follower_targets_leader_addr():
    argv = snapclient_argv(_follower(leader_addr="192.168.1.50"))
    assert "192.168.1.50" in argv
    # And NOT the loopback.
    assert "127.0.0.1" not in argv


def test_snapclient_argv_latency_from_buffer_ms():
    argv = snapclient_argv(_follower(buffer_ms=600))
    assert argv[argv.index("--latency") + 1] == "0"


def test_snapclient_argv_latency_from_client_latency_ms():
    cfg = _follower(buffer_ms=600)
    cfg = GroupingConfig(
        **{**cfg.__dict__, "client_latency_ms": 17}
    )
    argv = snapclient_argv(cfg)
    assert argv[argv.index("--latency") + 1] == "17"


def test_snapclient_argv_starts_with_snapclient():
    argv = snapclient_argv(_leader())
    assert argv[0] == "snapclient"


def test_snapclient_argv_host_flag_present():
    argv = snapclient_argv(_follower(leader_addr="10.0.0.7"))
    assert "--host" in argv
    # The value immediately follows the flag.
    assert argv[argv.index("--host") + 1] == "10.0.0.7"


def test_snapclient_argv_follower_passes_stable_mdns_host_verbatim():
    """A stable mDNS .local handle (what the bond wizard mints, surviving the
    leader's DHCP-IP churn) is passed verbatim to --host — snapclient resolves
    it at connect time. The handle is NOT rewritten to an IP here."""
    argv = snapclient_argv(_follower(leader_addr="jts3.local"))
    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "jts3.local"


# ---------- snapclient_argv(): inv-2 leader content lane (STAGED) ----------
#
# The DAC reroute is gated off behind LEADER_CONTENT_LANE_GATE; player_fifo
# defaults to None so snapclient is unchanged until the outputd reader lands.


def test_snapclient_argv_unchanged_when_player_fifo_unset():
    """player_fifo=None (default) is BYTE-FOR-BYTE the pre-inv-2 command — the
    gated-off reroute is a true no-op."""
    cfg = _follower(leader_addr="jts3.local")
    assert snapclient_argv(cfg) == [
        "snapclient", "--host", "jts3.local", "--latency", "0",
    ]
    assert snapclient_argv(cfg, player_fifo=None) == snapclient_argv(cfg)
    assert "--player" not in snapclient_argv(cfg)


def test_snapclient_argv_adds_file_player_when_fifo_set():
    """When staged on, snapclient writes raw PCM to the member-content FIFO via
    its `file` player (never snd-aloop — inv-2); the leader still targets
    loopback."""
    fifo = "/run/jasper-grouping/member-content.fifo"
    argv = snapclient_argv(_leader(), player_fifo=fifo)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"  # leader -> own server
    assert "--player" in argv
    assert argv[argv.index("--player") + 1] == f"file:filename={fifo}"


# ---------- snapclient_argv(): ACTIVE follower loopback (Slice 3) ----------


def test_snapclient_argv_active_endpoint_uses_alsa_loopback_player():
    """An active follower writes the round-trip snd-aloop loopback via the ALSA
    player (--soundcard <dev> --player alsa), NOT the dumb-follower file FIFO —
    its CamillaDSP captures the paired side and runs Layer A in the bonded
    path."""
    dev = reconcile_mod.GROUPING_LOOPBACK_PLAYBACK
    argv = snapclient_argv(_follower(), player_alsa_device=dev)
    assert argv[argv.index("--soundcard") + 1] == dev
    assert argv[argv.index("--player") + 1] == "alsa"
    assert "file:filename=" not in " ".join(argv)


def test_snapclient_argv_alsa_player_takes_precedence_over_fifo():
    """If both are (defensively) passed, the ALSA loopback wins — the active
    path never falls back to the FIFO."""
    argv = snapclient_argv(
        _follower(), player_fifo="/x.fifo", player_alsa_device="hw:Loopback,0,5",
    )
    assert "--soundcard" in argv and "file:filename=" not in " ".join(argv)


# ---------- _assemble_args(): pure derivation of the two env keys ----------
#
# These mirror the snap*_argv tests but assert on the env-key VALUES the
# units read (argv[0] stripped, space-joined), still without any I/O.


SERVER_KEY = "JASPER_SNAPSERVER_ARGS"
CLIENT_KEY = "JASPER_SNAPCLIENT_ARGS"


def test_assemble_args_returns_both_keys_always():
    for cfg in (_disabled(), _invalid(), _leader(), _follower()):
        d = _assemble_args(cfg)
        assert set(d) == {SERVER_KEY, CLIENT_KEY}


def test_assemble_args_leader_sets_both_keys():
    d = _assemble_args(_leader())
    assert d[SERVER_KEY]  # non-empty
    assert d[CLIENT_KEY]  # non-empty


def test_assemble_args_leader_strips_binary_name_from_server():
    """The persisted value is argv AFTER argv[0] — the binary is already
    in the unit's ExecStart, so it must not be duplicated."""
    d = _assemble_args(_leader())
    # snapserver_argv[0] == "snapserver"; the joined value must NOT start
    # with the binary token.
    assert not d[SERVER_KEY].split()[0] == "snapserver"
    assert d[SERVER_KEY] == " ".join(snapserver_argv(_leader())[1:])


def test_assemble_args_leader_strips_binary_name_from_client():
    from jasper.multiroom.reconcile import MEMBER_CONTENT_FIFO
    d = _assemble_args(_leader())
    assert not d[CLIENT_KEY].split()[0] == "snapclient"
    # An active member's client ALWAYS carries the round-trip file player
    # (Increment 5): never an ALSA sink, which would fight outputd for
    # the DAC.
    assert d[CLIENT_KEY] == " ".join(
        snapclient_argv(_leader(), player_fifo=MEMBER_CONTENT_FIFO)[1:]
    )
    assert f"--player file:filename={MEMBER_CONTENT_FIFO}" in d[CLIENT_KEY]


def test_assemble_args_leader_server_carries_the_fifo_source():
    d = _assemble_args(_leader())
    assert SNAPFIFO in d[SERVER_KEY]
    assert "pipe://" in d[SERVER_KEY]


def test_assemble_args_follower_server_empty_client_set():
    d = _assemble_args(_follower(leader_addr="192.168.1.50"))
    assert d[SERVER_KEY] == ""          # a follower runs no server
    assert d[CLIENT_KEY]                # but does run a client
    assert "--host 192.168.1.50" in d[CLIENT_KEY]


def test_assemble_args_follower_uses_outputd_fifo_not_direct_alsa():
    """Every active member (either profile) writes the round-trip outputd
    FIFO via snapclient's `file` player — there is no direct-ALSA endpoint
    variant any more."""
    from jasper.multiroom.reconcile import MEMBER_CONTENT_FIFO

    d = _assemble_args(_follower())

    assert d[SERVER_KEY] == ""
    assert f"--player file:filename={MEMBER_CONTENT_FIFO}" in d[CLIENT_KEY]
    assert "alsa:device=default" not in d[CLIENT_KEY]


def test_assemble_args_active_endpoint_writes_loopback_not_fifo():
    """An ACTIVE follower (active_endpoint=True) writes the snd-aloop round-trip
    loopback via the ALSA player; the dumb-follower FIFO is NOT used (camilla
    owns the path). The default (active_endpoint=False) is unchanged."""
    from jasper.multiroom.reconcile import (
        GROUPING_LOOPBACK_PLAYBACK,
        MEMBER_CONTENT_FIFO,
    )

    d = _assemble_args(_follower(), active_endpoint=True)
    assert d[SERVER_KEY] == ""  # a follower runs no server
    assert f"--soundcard {GROUPING_LOOPBACK_PLAYBACK} --player alsa" in d[CLIENT_KEY]
    assert MEMBER_CONTENT_FIFO not in d[CLIENT_KEY]
    # default path is the dumb FIFO (regression guard for the off-by-default).
    assert "--soundcard" not in _assemble_args(_follower())[CLIENT_KEY]


def test_outputd_grouping_env_active_endpoint_clears_dac_content():
    """An ACTIVE follower disables outputd's dac_content ChannelPick — camilla
    owns the channel-pick + split, so outputd runs its normal active sink. The
    round-trip lane env is cleared; TTS also stays off outputd because active
    voice rides fan-in upstream of the crossover. A DUMB member still arms the
    FIFO lane."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        OUTPUTD_TTS_SOCKET_ENV,
        outputd_grouping_env,
    )

    active = outputd_grouping_env(_follower(), active_endpoint=True)
    assert active[OUTPUTD_DAC_CONTENT_FIFO_ENV] == ""  # cleared (no dac_content)
    assert active[OUTPUTD_TTS_SOCKET_ENV] == ""
    dumb = outputd_grouping_env(_follower(), active_endpoint=False)
    assert dumb[OUTPUTD_DAC_CONTENT_FIFO_ENV] != ""  # dumb member arms the lane


def test_outputd_grouping_env_emits_sub_corner_only_for_sub():
    """The wireless-sub low-pass corner rides the outputd lane ONLY when the
    member's channel is "sub"; it is ABSENT for every other channel (a non-sub
    member must never carry it)."""
    import dataclasses

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_SUB_HZ_ENV,
        outputd_grouping_env,
    )

    sub = dataclasses.replace(_follower(channel="sub"), crossover_hz=120.0)
    env = outputd_grouping_env(sub)
    assert env[OUTPUTD_DAC_CONTENT_SUB_HZ_ENV] == "120.0"

    for ch in ("left", "right", "stereo", "mono"):
        env = outputd_grouping_env(_follower(channel=ch))
        assert OUTPUTD_DAC_CONTENT_SUB_HZ_ENV not in env


def test_outputd_grouping_env_highpasses_mains_when_bond_has_sub():
    import dataclasses

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV,
        outputd_grouping_env,
    )

    leader = dataclasses.replace(
        _leader(channel="left"),
        crossover_hz=100.0,
        roster=(BondMember(addr="192.168.1.8", name="Sub", channel="sub"),),
    )
    assert outputd_grouping_env(leader)[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == "100.0"

    follower = dataclasses.replace(
        _follower(channel="right"),
        crossover_hz=100.0,
        subwoofer_present=True,
    )
    assert outputd_grouping_env(follower)[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == "100.0"


def test_outputd_grouping_env_clears_main_highpass_when_not_applicable():
    import dataclasses

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV,
        outputd_grouping_env,
    )

    rostered = dataclasses.replace(
        _leader(channel="left"),
        crossover_hz=90.0,
        roster=(BondMember(addr="192.168.1.8", name="Sub", channel="sub"),),
    )
    assert outputd_grouping_env(
        dataclasses.replace(rostered, mains_highpass_enabled=False)
    )[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == ""
    assert outputd_grouping_env(_leader(channel="left"))[
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV
    ] == ""
    assert outputd_grouping_env(
        dataclasses.replace(_follower(channel="sub"), subwoofer_present=True)
    )[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == ""
    assert outputd_grouping_env(
        dataclasses.replace(_follower(channel="right"), subwoofer_present=True),
        active_endpoint=True,
    )[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == ""
    assert outputd_grouping_env(_disabled())[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == ""


def test_outputd_grouping_env_clears_tts_socket_for_a_sub():
    """A sub plays only low-passed bass and NEVER voice; outputd mixes TTS AFTER
    the low-pass, so a sub must NOT arm the outputd TTS lane (else full-range
    speech would reach the subwoofer). Every non-sub PASSIVE member keeps it
    armed; active endpoints keep it cleared regardless of channel because active
    voice rides fan-in upstream of the crossover."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_TTS_SOCKET,
        OUTPUTD_TTS_SOCKET_ENV,
        outputd_grouping_env,
    )

    sub = outputd_grouping_env(_follower(channel="sub"))
    assert sub[OUTPUTD_TTS_SOCKET_ENV] == ""  # cleared = unset to outputd
    active_sub = outputd_grouping_env(
        _follower(channel="sub"), active_endpoint=True,
    )
    assert active_sub[OUTPUTD_TTS_SOCKET_ENV] == ""

    for ch in ("left", "right", "stereo", "mono"):
        env = outputd_grouping_env(_follower(channel=ch))
        assert env[OUTPUTD_TTS_SOCKET_ENV] == OUTPUTD_TTS_SOCKET
        active_env = outputd_grouping_env(
            _follower(channel=ch), active_endpoint=True,
        )
        assert active_env[OUTPUTD_TTS_SOCKET_ENV] == ""


def test_outputd_grouping_env_no_sub_corner_when_not_active_member():
    """An active-endpoint sub (camilla owns the pick) and a disabled config
    both clear the lane — the corner key is never emitted there."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_SUB_HZ_ENV,
        outputd_grouping_env,
    )

    active = outputd_grouping_env(_follower(channel="sub"), active_endpoint=True)
    assert OUTPUTD_DAC_CONTENT_SUB_HZ_ENV not in active
    assert OUTPUTD_DAC_CONTENT_SUB_HZ_ENV not in outputd_grouping_env(_disabled())


def test_assemble_args_disabled_clears_both():
    d = _assemble_args(_disabled())
    assert d[SERVER_KEY] == ""
    assert d[CLIENT_KEY] == ""


def test_assemble_args_invalid_clears_both():
    """Fail-safe: an enabled-but-broken bond derives empty args so a
    started unit can never pick up stale values."""
    d = _assemble_args(_invalid())
    assert d[SERVER_KEY] == ""
    assert d[CLIENT_KEY] == ""


def test_assemble_args_codec_and_buffer_flow_into_server():
    d = _assemble_args(_leader(codec="opus", buffer_ms=750))
    assert "codec=opus" in d[SERVER_KEY]
    assert "buffer_ms=750" in d[SERVER_KEY]


# ---------- _write_args_file(): atomic, mode 0644, fail-soft ----------


def test_write_args_file_round_trips_keys(tmp_path, monkeypatch):
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    keys = {SERVER_KEY: "--stream.source pipe://x", CLIENT_KEY: "--host 127.0.0.1 --latency 0"}
    assert _write_args_file(keys, path=str(target)) is True
    text = target.read_text()
    assert f"{SERVER_KEY}=--stream.source pipe://x\n" in text
    assert f"{CLIENT_KEY}=--host 127.0.0.1 --latency 0\n" in text


def test_write_args_file_empty_values_writes_bare_keys(tmp_path, monkeypatch):
    """The disabled/invalid case: both keys present but empty — clears
    any stale args rather than leaving the prior value live."""
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    assert _write_args_file({SERVER_KEY: "", CLIENT_KEY: ""}, path=str(target)) is True
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_write_args_file_mode_is_0644(tmp_path, monkeypatch):
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}, path=str(target))
    assert (os.stat(target).st_mode & 0o777) == 0o644


def test_write_args_file_makedirs_parent(tmp_path, monkeypatch):
    """The reconciler os.makedirs the dir (it is NOT a unit
    RuntimeDirectory) — a missing parent must be created, not error."""
    sub = tmp_path / "jasper-grouping"
    target = sub / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(sub))
    assert not sub.exists()
    assert _write_args_file({SERVER_KEY: "x", CLIENT_KEY: ""}, path=str(target)) is True
    assert sub.is_dir()
    assert target.exists()


def test_write_args_file_is_fail_soft(tmp_path, monkeypatch):
    """A write failure (e.g. makedirs raising) returns False and NEVER
    raises — a lost args write must not crash the reconcile."""
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise OSError("disk full")

    # The atomic tempfile+rename mechanics now live in jasper.atomic_io;
    # inject the failure there (makedirs is the first I/O it does).
    monkeypatch.setattr(reconcile_mod.atomic_io.os, "makedirs", _boom)
    # Must not raise; must report failure.
    assert _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}) is False


def test_write_args_file_no_partial_file_on_inner_failure(tmp_path, monkeypatch):
    """If the write/rename fails after mkstemp, the temp file is cleaned
    up and no target file is published."""
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise OSError("rename failed")

    # The rename now happens inside jasper.atomic_io; boom it there.
    monkeypatch.setattr(reconcile_mod.atomic_io.os, "replace", _boom)
    assert _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}, path=str(target)) is False
    assert not target.exists()
    # No leftover temp files in the dir.
    assert list(tmp_path.glob(".snapcast-args.*")) == []


# ---------- main(): assembles + writes args BEFORE applying the plan ----------
#
# main() is the I/O entrypoint; here we stub out the real systemctl calls
# (_apply) and the config load, and redirect the args file to a tmp path,
# so we can assert the args-write happens (and is ordered before _apply)
# without touching the host.


def _patch_main_io(monkeypatch, tmp_path, cfg):
    """Redirect ALL of main()'s side effects to a tmp dir + record order.

    Patches: the args + outputd-env files and the member FIFO into
    tmp_path; load_config to the synthetic cfg; _apply + _restart_outputd
    to order-recording fakes; and the leader_config sync entrypoints to
    spies (main from-imports them at call time, so patching the
    leader_config MODULE attributes intercepts them)."""
    import jasper.multiroom.leader_config as leader_config_mod

    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    monkeypatch.setattr(reconcile_mod, "ARGS_FILE", str(target))
    monkeypatch.setattr(
        reconcile_mod, "OUTPUTD_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-outputd.env"),
    )
    monkeypatch.setattr(
        reconcile_mod, "VOICE_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-voice.env"),
    )
    monkeypatch.setattr(
        reconcile_mod, "AIRPLAY_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-airplay.env"),
    )
    monkeypatch.setattr(
        reconcile_mod, "MEMBER_CONTENT_FIFO",
        str(tmp_path / "member-content.fifo"),
    )
    monkeypatch.setattr(
        reconcile_mod, "FOLLOWER_STATUS_FILE",
        str(tmp_path / "grouping-follower-status.json"),
    )
    # Default to the PASSIVE path (these legacy main() tests assert dumb-member
    # behavior); the active-follower main() flow has its own tests that override
    # this. is_active_speaker_box reads the topology, so stub it for hermeticity.
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: False)
    monkeypatch.setattr(reconcile_mod, "load_config", lambda *a, **k: cfg)
    # Snapcast provisioning (main() calls it for any enabled bond): default to a
    # present no-op so these tests never shell out to apt. The provisioning tests
    # override it. main() from-imports it, so patch the provision module attr.
    import jasper.multiroom.provision as provision_mod

    monkeypatch.setattr(
        provision_mod, "ensure_snapcast_installed",
        lambda **kw: {"state": "present", "detail": ""},
    )

    order: list[str] = []
    real_write = reconcile_mod._write_args_file

    def _spy_write(keys, *, path=str(target)):
        order.append("write")
        return real_write(keys, path=path)

    def _fake_apply(plan_):
        order.append("apply")
        # Assert the args file already exists when _apply runs.
        assert target.exists(), "args file must be written BEFORE _apply"
        return 0

    monkeypatch.setattr(reconcile_mod, "_write_args_file", _spy_write)
    monkeypatch.setattr(reconcile_mod, "_apply", _fake_apply)
    monkeypatch.setattr(
        reconcile_mod, "_restart_outputd",
        lambda: order.append("outputd_restart") or True,
    )
    def _fake_restart(unit, *, no_block=False):
        suffix = ":no_block" if no_block else ""
        order.append(f"restart:{unit}{suffix}")
        return True

    monkeypatch.setattr(reconcile_mod, "_restart_unit", _fake_restart)
    monkeypatch.setattr(
        leader_config_mod, "apply_bonded_leader_config_sync",
        lambda cfg_: order.append("camilla_bonded") or "bonded.yml",
    )
    monkeypatch.setattr(
        leader_config_mod, "restore_solo_config_sync",
        lambda: order.append("camilla_restore_check") and None,
    )
    import jasper.multiroom.snapcast_rpc as snapcast_rpc_mod

    monkeypatch.setattr(
        snapcast_rpc_mod, "ensure_groups_on_stream",
        lambda want, **kw: order.append("stream_binding") or {
            "reachable": True, "groups": 1, "fixed": 0, "failed": 0,
        },
    )
    return target, order


def test_main_leader_writes_both_keys_then_applies(tmp_path, monkeypatch):
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    rc = main([])
    assert rc == 0
    # Args persisted before units start (the full order is pinned by
    # test_main_leader_order_env_restart_units_then_camilla).
    assert order.index("write") < order.index("apply")
    text = target.read_text()
    assert text.startswith(f"{SERVER_KEY}=")
    assert SNAPFIFO in text                       # server reads the fifo
    assert f"\n{CLIENT_KEY}=--host 127.0.0.1" in text  # leader client → loopback


def test_main_follower_writes_client_only(tmp_path, monkeypatch):
    target, order = _patch_main_io(
        monkeypatch, tmp_path, _follower(leader_addr="192.168.1.50")
    )
    rc = main([])
    assert rc == 0
    text = target.read_text()
    assert f"{SERVER_KEY}=\n" in text             # follower: server empty
    assert f"{CLIENT_KEY}=--host 192.168.1.50" in text


def test_main_disabled_writes_empty_args(tmp_path, monkeypatch):
    target, _order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    rc = main([])
    assert rc == 0
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_invalid_writes_empty_args(tmp_path, monkeypatch):
    target, _order = _patch_main_io(monkeypatch, tmp_path, _invalid())
    rc = main([])
    assert rc == 0
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_survives_args_write_failure(tmp_path, monkeypatch):
    """A failed args write is fail-soft: main() still applies the plan,
    never crashes."""
    _target, _order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_write_args_file", lambda *a, **k: False)

    applied = []
    monkeypatch.setattr(
        reconcile_mod, "_apply", lambda plan_: applied.append(True) or 0
    )
    main([])
    assert applied == [True]  # plan still applied despite the write failure


def test_main_leader_order_env_restart_units_then_camilla(tmp_path, monkeypatch):
    """The load-bearing ORDER: derived files → outputd restart (env
    changed on first run) → unit plan → camilla bonded apply LAST (the
    pipe's reader, snapserver, must exist before CamillaDSP's File sink
    opens it for write)."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    rc = main([])
    assert rc == 0
    # The voice-env change kicks jasper-aec-reconcile, NOT jasper-voice:
    # that script is the single owner of the voice/bridge units and
    # decides restart-vs-park from the derived flag + provider + mic.
    assert order == [
        "write", "outputd_restart",
        "restart:jasper-aec-reconcile.service:no_block",
        "apply", "restart:shairport-sync.service", "camilla_bonded",
        "stream_binding",
    ]


def test_main_leader_second_run_skips_outputd_restart(tmp_path, monkeypatch):
    """Compare-before-write: an unchanged outputd lane env must NOT
    restart outputd on the next reconcile (no churn on no-change runs)."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    order.clear()
    assert main([]) == 0
    # no outputd/voice restart on the unchanged second run
    assert order == ["write", "apply", "camilla_bonded", "stream_binding"]


def test_main_nonleader_runs_solo_restore_not_bonded_apply(tmp_path, monkeypatch):
    """A follower / solo reconcile goes through the restore path (a no-op
    when already solo) and never the bonded apply."""
    for cfg in (_follower(leader_addr="192.168.1.50"), _disabled()):
        _target, order = _patch_main_io(monkeypatch, tmp_path, cfg)
        assert main([]) == 0
        assert "camilla_bonded" not in order
        assert "camilla_restore_check" in order


def test_main_leader_writes_member_fifo(tmp_path, monkeypatch):
    """An active member's round-trip FIFO exists after reconcile (created
    before snapclient would start writing it)."""
    import stat as stat_mod
    _target, _order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    fifo = tmp_path / "member-content.fifo"
    assert fifo.exists()
    assert stat_mod.S_ISFIFO(fifo.stat().st_mode)


def test_main_writes_outputd_env_for_member_and_clears_for_solo(tmp_path, monkeypatch):
    """The outputd lane env carries FIFO+channel while bonded and explicit
    empty strings after disband (disable-clears-stale)."""
    _target, _order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    env = (tmp_path / "grouping-outputd.env").read_text()
    assert "JASPER_OUTPUTD_DAC_CONTENT_FIFO=" + str(
        tmp_path / "member-content.fifo") in env
    assert "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL=left" in env

    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    assert main([]) == 0
    env = (tmp_path / "grouping-outputd.env").read_text()
    assert "JASPER_OUTPUTD_DAC_CONTENT_FIFO=\n" in env
    assert "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL=\n" in env
    assert "outputd_restart" in order  # env changed bonded→cleared ⇒ restart


def test_main_leader_writes_airplay_offset_and_restarts_shairport(tmp_path, monkeypatch):
    """A bonded leader folds the Snapcast buffer into its AirPlay offset
    (grouping-airplay.env) and restarts shairport — AFTER the unit plan —
    so ExecStartPre re-derives the offset."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    env = (tmp_path / "grouping-airplay.env").read_text()
    assert "JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC=0.400000" in env
    assert "restart:shairport-sync.service" in order
    assert order.index("apply") < order.index("restart:shairport-sync.service")


def test_main_solo_writes_no_airplay_offset_and_skips_shairport_restart(tmp_path, monkeypatch):
    """INVARIANT: a solo speaker that was never bonded writes NO bonded key
    and never restarts shairport — its AirPlay offset stays byte-identical."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    assert main([]) == 0
    assert "restart:shairport-sync.service" not in order
    p = tmp_path / "grouping-airplay.env"
    # Empty body on a fresh file is not written at all (no spurious churn).
    assert not p.exists() or "BONDED_EXTRA_DELAY" not in p.read_text()


def test_main_unbond_restarts_shairport_to_restore_solo_offset(tmp_path, monkeypatch):
    """bonded leader -> solo: the airplay offset env clears and shairport is
    restarted so its offset reverts to the solo value (restore-on-unbond — a
    stranded bonded offset would make solo audio play early)."""
    _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0                                  # bond writes the delta
    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    assert main([]) == 0                                  # unbond clears it
    assert "restart:shairport-sync.service" in order
    assert "BONDED_EXTRA_DELAY" not in (
        tmp_path / "grouping-airplay.env"
    ).read_text()


def test_main_follower_does_not_restart_shairport(tmp_path, monkeypatch):
    """A bonded FOLLOWER's shairport is PARKED by the plan; the airplay-offset
    path must never restart (un-park) it."""
    _target, order = _patch_main_io(
        monkeypatch, tmp_path, _follower(leader_addr="192.168.1.50")
    )
    assert main([]) == 0
    assert "restart:shairport-sync.service" not in order


def test_main_nonleader_skips_stream_binding(tmp_path, monkeypatch):
    """The binding pin is leader-only (the follower has no snapserver)."""
    for cfg in (_follower(leader_addr="192.168.1.50"), _disabled()):
        _target, order = _patch_main_io(monkeypatch, tmp_path, cfg)
        assert main([]) == 0
        assert "stream_binding" not in order


def test_main_unreachable_snapserver_flips_rc(tmp_path, monkeypatch):
    """A bond whose bindings cannot be verified is a degraded bond: the
    oneshot exits nonzero (units keep running; health shows it)."""
    import jasper.multiroom.snapcast_rpc as snapcast_rpc_mod

    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(
        snapcast_rpc_mod, "ensure_groups_on_stream",
        lambda want, **kw: {"reachable": False, "groups": 0, "fixed": 0, "failed": 0},
    )
    assert main([]) == 1
    assert "apply" in order  # units still managed


def test_main_camilla_failure_is_fail_soft_but_flips_rc(tmp_path, monkeypatch):
    """A camilla apply failure never aborts unit management — but the
    oneshot exits nonzero so the failure is visible on the unit."""
    import jasper.multiroom.leader_config as leader_config_mod
    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())

    def _boom(cfg_):
        raise RuntimeError("camilla unavailable")

    monkeypatch.setattr(
        leader_config_mod, "apply_bonded_leader_config_sync", _boom,
    )
    rc = main([])
    assert rc == 1
    assert "apply" in order  # units still managed


# ---------- the leader's music-producer predicate ----------
# (The outputd-as-producer tap machinery — env write/read, change-gate,
# try-restart, SNAPFIFO_PRODUCER_WIRED — was REMOVED 2026-06-11 with the
# canonical design; see HANDOFF-multiroom.md §2 "Stranded by this design".
# desired_snapfifo_path survives as the pure "this role needs a producer"
# predicate driving the runtime-health derive.)


def test_desired_snapfifo_path_leader_needs_producer():
    assert desired_snapfifo_path(_leader()) == SNAPFIFO


def test_desired_snapfifo_path_follower_does_not():
    assert desired_snapfifo_path(_follower()) == ""


def test_desired_snapfifo_path_disabled_does_not():
    assert desired_snapfifo_path(_disabled()) == ""


def test_desired_snapfifo_path_invalid_leader_does_not():
    # enabled + role=leader BUT carrying an error => a broken bond never
    # claims to need (or get) a producer.
    assert desired_snapfifo_path(_invalid()) == ""


def test_main_fresh_solo_first_reconcile_never_touches_voice(
    monkeypatch, tmp_path,
):
    """The first-write-empty rule, pinned: a FRESH solo speaker's first
    reconcile must neither create an empty grouping-voice.env nor restart
    jasper-voice (a ~10-15 s outage on every first boot otherwise). The
    rule lives in _write_outputd_env (absent file + empty body = no
    change); this is the documented promise from HANDOFF-multiroom."""
    order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    rc = reconcile_mod.main(["--reason", "test"])
    assert rc == 0
    assert not (tmp_path / "grouping-voice.env").exists()
    assert "restart:jasper-voice.service" not in order


# ---------- dumb-follower profile: park + restore + absent-unit no-ops ----


def test_plan_follower_parks_the_local_source_resource_groups():
    """role=follower stops every local-source parked resource (the dumb
    follower advertises and runs no local sources — and a phantom local
    session would audibly leak during inv-B fallback periods)."""
    from jasper.local_sources import local_source_park_units
    p = plan(_follower())
    by_unit = {i.unit: i.desired for i in p.intents}
    for u in local_source_park_units():
        assert by_unit[u] == "stop", u
    assert by_unit[SNAPCLIENT_UNIT] == "start"
    assert by_unit[SNAPSERVER_UNIT] == "stop"
    # Ordering contract: every stop precedes the snapclient start.
    kinds = [i.desired for i in p.intents]
    assert kinds.index("start") == len(kinds) - 1


def test_plan_follower_parks_usbsink_gadget_not_only_bridge():
    """USB input owns a bridge daemon and a host-visible gadget init unit.
    Follower parking must stop the gadget owner so a laptop cannot still
    see the follower as a USB audio device."""
    p = plan(_follower())
    by_unit = {i.unit: i.desired for i in p.intents}
    assert by_unit["jasper-usbsink-init.service"] == "stop"
    assert by_unit["jasper-usbsink.service"] == "stop"


def test_plan_follower_parks_source_arbiter_infrastructure():
    """The mux is shared local-source infrastructure. It is not one source's
    daemon, but it must stop while a follower has no local source authority."""
    p = plan(_follower())
    by_unit = {i.unit: i.desired for i in p.intents}
    assert by_unit["jasper-mux.service"] == "stop"


def test_plan_leader_keeps_sources_restored():
    """The leader is the pair's input hub — its renderer stack is never
    parked; the restore intents put a just-demoted ex-follower's sources
    back per the /sources/ wizard."""
    from jasper.local_sources import local_source_restore_units
    p = plan(_leader())
    by_unit = {i.unit: i.desired for i in p.intents}
    for u in local_source_restore_units():
        assert by_unit[u] == "restore", u
    assert by_unit["jasper-mux.service"] == "restore"
    assert "jasper-usbsink-init.service" not in {
        i.unit for i in p.intents if i.desired == "restore"
    }


def _apply_with_fake_systemctl(monkeypatch, intents, *, enabled=(),
                               absent=()):
    """Run _apply with subprocess.run faked. Returns (rc, calls) where
    calls is the list of argv lists systemctl saw."""
    import subprocess as sp
    from jasper.multiroom.reconcile import ReconcilePlan, _apply

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        verb, unit = argv[1], argv[-1]
        if verb == "is-enabled":
            return sp.CompletedProcess(argv, 0 if unit in enabled else 1)
        if unit in absent:
            if kw.get("check"):
                raise sp.CalledProcessError(
                    5, argv, stderr=f"Failed to {verb} {unit}: Unit not loaded.",
                )
            return sp.CompletedProcess(argv, 5)
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    rc = _apply(ReconcilePlan(intents=tuple(intents), summary="test"))
    return rc, calls


def test_apply_restore_starts_only_enabled_units(monkeypatch):
    from jasper.multiroom.reconcile import UnitIntent
    rc, calls = _apply_with_fake_systemctl(
        monkeypatch,
        [UnitIntent("a.service", "restore", "t"),
         UnitIntent("b.service", "restore", "t")],
        enabled={"a.service"},
    )
    assert rc == 0
    assert ["systemctl", "start", "a.service"] in calls
    assert not any(c[1] == "start" and c[-1] == "b.service" for c in calls)


def test_apply_absent_unit_is_a_clean_noop(monkeypatch):
    """A streambox box never installs some full-speaker units — stop
    intents against absent units must not flip the exit code (absent
    units are no-ops)."""
    from jasper.multiroom.reconcile import UnitIntent
    rc, calls = _apply_with_fake_systemctl(
        monkeypatch,
        [UnitIntent("ghost.service", "stop", "t"),
         UnitIntent("real.service", "stop", "t")],
        absent={"ghost.service"},
    )
    assert rc == 0
    assert ["systemctl", "stop", "real.service"] in calls


def test_apply_real_failure_still_flips_rc(monkeypatch):
    """Absent-unit tolerance must not swallow REAL failures."""
    import subprocess as sp
    from jasper.multiroom.reconcile import ReconcilePlan, UnitIntent, _apply

    def fake_run(argv, **kw):
        raise sp.CalledProcessError(1, argv, stderr="Job failed. See logs.")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    rc = _apply(ReconcilePlan(
        intents=(UnitIntent("x.service", "stop", "t"),), summary="t"))
    assert rc == 1


def test_main_follower_voice_env_kicks_aec_reconcile_with_park_flag(
    tmp_path, monkeypatch,
):
    """Bond-form on a follower: grouping-voice.env carries the validated
    park flag and the change is applied via ONE kick of
    jasper-aec-reconcile (the voice/bridge unit owner) — never a direct
    jasper-voice restart from this reconciler."""
    from jasper.multiroom.reconcile import VOICE_PARK_ENV
    _target, order = _patch_main_io(
        monkeypatch, tmp_path, _follower(leader_addr="192.168.1.50"),
    )
    assert main([]) == 0
    text = (tmp_path / "grouping-voice.env").read_text()
    assert f"{VOICE_PARK_ENV}=1" in text
    assert "restart:jasper-aec-reconcile.service:no_block" in order
    assert "restart:jasper-voice.service" not in order


# ---------- active-follower endpoint status writer (Slice 3) ----------


def test_write_follower_status_round_trips(tmp_path):
    """The reconciler writes a fresh JSON status the /state reader consumes."""
    import json

    from jasper.multiroom.reconcile import _write_follower_status

    p = str(tmp_path / "grouping-follower-status.json")
    _write_follower_status(active_follower=True, blocked_reason="", path=p)
    assert json.loads(open(p).read()) == {
        "active_follower": True, "active_leader": False, "blocked_reason": "",
    }
    # An active LEADER runs camilla#2 as the bond leader (Slice 5).
    _write_follower_status(
        active_follower=False, active_leader=True, blocked_reason="", path=p,
    )
    assert json.loads(open(p).read()) == {
        "active_follower": False, "active_leader": True, "blocked_reason": "",
    }
    # Rewritten every reconcile — a later blocked state replaces the prior one.
    _write_follower_status(active_follower=False, blocked_reason="graph_unprovable", path=p)
    assert json.loads(open(p).read()) == {
        "active_follower": False, "active_leader": False,
        "blocked_reason": "graph_unprovable",
    }


# ---------- main(): ACTIVE follower flow (Slice 3) ----------


def test_main_active_follower_prechecks_early_then_swaps_camilla_after_units(
    tmp_path, monkeypatch,
):
    """An active follower: the readiness GATE runs BEFORE the units (fail-safe),
    snapclient writes the loopback (not the FIFO), and the CamillaDSP swap runs
    AFTER the unit plan (so the loopback has its writer)."""
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    monkeypatch.setattr(
        fc_mod, "precheck_active_follower_sync",
        lambda cfg_: order.append("precheck") or "grouping_follower.yml",
    )
    monkeypatch.setattr(
        fc_mod, "apply_prebuilt_follower_config_sync",
        lambda: order.append("camilla_active_follower") or "grouping_follower.yml",
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    # Gate before units; camilla swap after the unit plan.
    assert order.index("precheck") < order.index("apply")
    assert order.index("apply") < order.index("camilla_active_follower")
    # snapclient targets the round-trip loopback, not the dumb FIFO.
    body = target.read_text()
    assert reconcile_mod.GROUPING_LOOPBACK_PLAYBACK in body
    assert reconcile_mod.MEMBER_CONTENT_FIFO not in body
    # endpoint status persisted as active_crossover.
    status = tmp_path / "grouping-follower-status.json"
    assert '"active_follower": true' in status.read_text()


def test_main_active_follower_precheck_failure_falls_back_to_solo(
    tmp_path, monkeypatch,
):
    """If the readiness gate fails (can't make the driver-domain graph safe),
    the box does NOT bond — it fails safe to SOLO: no CamillaDSP follower swap,
    snapclient cleared, the active-solo restore runs, and the block reason is
    surfaced for /state (invariant 5 fail-closed + self-recovery)."""
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)

    def _boom(cfg_):
        raise fc_mod.ActiveFollowerError("graph_unprovable", "nope")

    monkeypatch.setattr(fc_mod, "precheck_active_follower_sync", _boom)
    monkeypatch.setattr(
        fc_mod, "apply_prebuilt_follower_config_sync",
        lambda: order.append("camilla_active_follower"),
    )
    monkeypatch.setattr(
        fc_mod, "restore_active_follower_solo_sync",
        lambda: order.append("active_solo_restore") or None,
    )

    rc = main(["--reason", "test"])

    assert rc == 1  # surfaced as failed
    assert "camilla_active_follower" not in order  # never bonded the unsafe graph
    assert "active_solo_restore" in order  # fell back to solo active
    # snapclient args cleared (fail-safe to solo => no client).
    assert "JASPER_SNAPCLIENT_ARGS=\n" in target.read_text()
    # block reason surfaced for the dashboard.
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "graph_unprovable"' in status
    assert '"active_follower": false' in status


# ---------- main(): ACTIVE leader flow (Slice 5 — two CamillaDSP) ----------


def _patch_active_leader(monkeypatch, order):
    """Stub the active-leader config arm + the camilla#2 unit lifecycle into the
    order recorder. main() from-imports these at call time, so patching the
    active_leader_config MODULE attributes (and the reconcile module helpers)
    intercepts them."""
    import jasper.multiroom.active_leader_config as alc_mod

    monkeypatch.setattr(
        alc_mod, "precheck_active_leader_sync",
        lambda cfg_: order.append("precheck") or ("bake.yml", "crossover.yml"),
    )
    monkeypatch.setattr(
        alc_mod, "apply_active_leader_bake_sync",
        lambda: order.append("bake") or "bake.yml",
    )
    monkeypatch.setattr(
        alc_mod, "seed_crossover_statefile",
        lambda *a, **k: order.append("seed") or "crossover-statefile.yml",
    )
    monkeypatch.setattr(
        reconcile_mod, "_arm_crossover_unit",
        lambda: order.append("arm_camilla2") or True,
    )
    monkeypatch.setattr(
        reconcile_mod, "_disable_crossover_unit",
        lambda: order.append("disable_camilla2") or True,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_run_audio_hardware_reconcile",
        lambda *, reason: order.append(f"audio_hardware:{reason}") or True,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_ensure_unit_active",
        lambda unit, *, reason: order.append(f"ensure:{unit}:{reason}") or True,
    )
    # Default: snapserver is up (the bake gate passes). The snapserver-down
    # incident test overrides this. camilla#2 defaults to inactive so the arm
    # path exercises the new positive handle-release barrier.
    monkeypatch.setattr(
        reconcile_mod,
        "_unit_is_active",
        lambda unit: unit == reconcile_mod.SNAPSERVER_UNIT,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        lambda: order.append("probe") or reconcile_mod._PcmHandleProbeResult(
            "released",
            "status_closed",
            detail="closed",
            attempts=1,
            timeout_sec=0.8,
        ),
    )
    return alc_mod


def test_active_content_pcm_probe_reports_released_on_closed_status():
    """The release barrier keys on the per-substream procfs status: exact
    `closed` means hw:Loopback,0,5 has no opener and camilla#2 may arm."""
    calls = []

    class Proc:
        returncode = 0
        stdout = "closed\n"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Proc()

    result = reconcile_mod._wait_for_active_content_pcm_release(
        status_path="/tmp/status",
        run=fake_run,
        timeout_sec=0,
    )

    assert result.released
    assert result.reason == "status_closed"
    assert result.attempts == 1
    assert calls[0][0] == ["cat", "/tmp/status"]


def test_active_content_pcm_probe_times_out_when_status_stays_open():
    """Any non-closed ALSA substream status is treated as an open handle.
    Timeout returns busy so the active-leader arm can fail closed."""

    class Proc:
        returncode = 0
        stdout = "state: RUNNING\nowner_pid: 1234\n"
        stderr = ""

    result = reconcile_mod._wait_for_active_content_pcm_release(
        status_path="/tmp/status",
        run=lambda *a, **k: Proc(),
        timeout_sec=0,
    )

    assert result.busy
    assert result.reason == "timeout"
    assert result.detail == "state: RUNNING"


def test_active_content_pcm_probe_fail_soft_when_probe_tool_missing():
    """If the probe tool is absent, the barrier reports unknown instead of
    crashing the reconciler; main() logs a warning and preserves compatibility."""

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("cat")

    result = reconcile_mod._wait_for_active_content_pcm_release(
        status_path="/tmp/status",
        run=fake_run,
        timeout_sec=0,
    )

    assert result.unknown
    assert result.reason == "probe_tool_missing"


def test_main_active_leader_bakes_arms_camilla2_and_reseeds(tmp_path, monkeypatch):
    """An active leader: the readiness GATE runs BEFORE the units (fail-safe);
    after the units, camilla#1 bakes the wire, the crossover statefile is
    RE-SEEDED, then camilla#2 is armed (enable --now). snapclient writes the
    loopback (its own receiver), the leader hosts the stream, and the endpoint
    status persists active_leader=true."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)

    rc = main(["--reason", "test"])

    assert rc == 0
    # Gate before units; bake + re-seed + output-hardware reconverge + arm AFTER
    # the unit plan. Seed precedes both audio-hardware reconcile and the arm:
    # outputd recovery needs the camilla#2 endpoint graph, and a cold camilla#2
    # start must load that re-proven graph.
    assert order.index("precheck") < order.index("apply")
    assert order.index("apply") < order.index("disable_camilla2")
    assert order.index("disable_camilla2") < order.index(
        "ensure:jasper-camilla.service:active-leader-bake"
    )
    assert order.index(
        "ensure:jasper-camilla.service:active-leader-bake"
    ) < order.index("bake")
    assert order.index("bake") < order.index("seed")
    assert order.index("seed") < order.index("audio_hardware:grouping-active-leader-bake")
    assert (
        order.index("audio_hardware:grouping-active-leader-bake")
        < order.index("probe")
        < order.index("arm_camilla2")
    )
    # The active leader defers outputd restart to the audio-hardware reconciler,
    # because it must inspect the freshly loaded roleful graph before choosing
    # the active-content lane.
    assert "outputd_restart" not in order
    assert "stream_binding" in order  # the leader hosts the stream
    # snapclient targets the round-trip loopback (the leader is its own receiver),
    # not the dumb FIFO; the leader still runs snapserver.
    body = target.read_text()
    assert reconcile_mod.GROUPING_LOOPBACK_PLAYBACK in body
    assert reconcile_mod.MEMBER_CONTENT_FIFO not in body
    assert f"{SERVER_KEY}=" in body and SNAPFIFO in body
    # endpoint status persisted as an active LEADER.
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"active_leader": true' in status


def test_main_active_leader_already_armed_skips_release_probe(
    tmp_path, monkeypatch,
):
    """Idempotency: once camilla#2 is active, it legitimately owns substream 5.
    A steady-state reconcile must not probe that handle as if it were camilla#1
    still lagging closed."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_unit_is_active",
        lambda unit: unit in {
            reconcile_mod.SNAPSERVER_UNIT,
            reconcile_mod.CROSSOVER_UNIT,
        },
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "probe" not in order
    assert "arm_camilla2" not in order
    assert "stream_binding" in order
    assert reconcile_mod.GROUPING_LOOPBACK_PLAYBACK in target.read_text()


def test_main_active_leader_precheck_failure_falls_back_to_solo(tmp_path, monkeypatch):
    """If the readiness gate fails (camilla#1 bake OR camilla#2 driver-domain graph
    can't be made safe), the box does NOT bond — it fails safe to SOLO: no bake,
    no camilla#2 arm, snapclient cleared, and the block reason is surfaced for
    /state (invariant 5 fail-closed + self-recovery)."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)

    def _boom(cfg_):
        raise alc_mod.ActiveLeaderError("crossover_graph_unprovable", "nope")

    monkeypatch.setattr(alc_mod, "precheck_active_leader_sync", _boom)
    # On a refused FIRST bond camilla#2 was never armed, so the fail-safe restore
    # takes the (untouched) solo-active follower path — stub it for hermeticity.
    monkeypatch.setattr(
        fc_mod, "restore_active_follower_solo_sync",
        lambda: order.append("active_solo_restore") or None,
    )

    rc = main(["--reason", "test"])

    assert rc == 1
    assert "bake" not in order  # never baked the wire
    assert "arm_camilla2" not in order  # never armed an unprovable crossover
    assert "stream_binding" not in order  # never became a leader
    assert "active_solo_restore" in order  # fell back to solo active
    # snapclient args cleared (fail-safe to solo => no client).
    assert "JASPER_SNAPCLIENT_ARGS=\n" in target.read_text()
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "crossover_graph_unprovable"' in status
    assert '"active_leader": false' in status


def test_main_active_leader_unbond_disables_camilla2_and_restores(tmp_path, monkeypatch):
    """Unbond of an active leader (camilla#2 enabled is the discriminator): tear
    camilla#2 down + restore camilla#1 via the leader stash. The untouched
    active-follower restore path is NOT taken."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    # camilla#2 enabled => this box WAS an active leader.
    monkeypatch.setattr(
        reconcile_mod, "_unit_is_enabled",
        lambda unit: unit == reconcile_mod.CROSSOVER_UNIT,
    )
    monkeypatch.setattr(
        alc_mod, "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "active_speaker_baseline.yml",
    )
    monkeypatch.setattr(
        fc_mod, "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or None,
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "disable_camilla2" in order  # camilla#2 torn down
    assert "leader_restore" in order  # camilla#1 restored via the leader path
    assert "follower_restore" not in order  # the follower path is not taken
    # solo: snapcast cleared (no server, no client).
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_passive_leader_unchanged_never_arms_camilla2(tmp_path, monkeypatch):
    """Solo-impact regression: a PASSIVE leader (box not active) keeps the
    single-camilla pipe bake (apply_bonded_leader_config) and NEVER arms camilla#2
    — the split did not change the passive-leader path."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    # is_active_speaker_box stays False (the _patch_main_io default).
    _patch_active_leader(monkeypatch, order)

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "camilla_bonded" in order  # the unchanged passive-leader apply
    assert "bake" not in order  # NOT the active-leader bake
    assert "arm_camilla2" not in order  # camilla#2 never armed on a passive box
    assert "stream_binding" in order  # still a leader hosting the stream
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"active_leader": false' in status


def test_main_solo_active_box_takes_follower_path_not_leader_teardown(
    tmp_path, monkeypatch,
):
    """Solo-impact regression: a solo/active box whose camilla#2 is NOT enabled
    (never an active leader) takes the untouched active-follower restore path and
    never disables camilla#2 — the unit-enabled discriminator keeps the follower
    path byte-identical."""
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(reconcile_mod, "_unit_is_enabled", lambda unit: False)
    monkeypatch.setattr(
        fc_mod, "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or None,
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "follower_restore" in order  # the untouched follower path
    assert "disable_camilla2" not in order  # camilla#2 never touched
    assert "leader_restore" not in order


def test_main_active_leader_skips_arm_when_bake_fails(tmp_path, monkeypatch):
    """JTS5 incident regression (2026-06-23): if the camilla#1 bake FAILS, it
    stays on its solo-active DAC baseline — so camilla#2 must NOT be armed. Arming
    it would make both CamillaDSP fight for the DAC and (camilla#1 carries
    StartLimitAction=reboot) reboot-loop the box. The arm is gated on bake success."""
    import jasper.multiroom.active_leader_config as alc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)

    def _boom():
        order.append("bake_attempt")
        raise RuntimeError("camilla#1 unreachable")

    monkeypatch.setattr(alc_mod, "apply_active_leader_bake_sync", _boom)

    rc = main(["--reason", "test"])

    assert rc == 1
    assert "bake_attempt" in order  # the bake was attempted...
    assert "arm_camilla2" not in order  # ...but camilla#2 was NOT armed (no DAC fight)


def test_main_active_leader_skips_arm_when_audio_hardware_reconcile_fails(
    tmp_path, monkeypatch,
):
    """After the bake and inert camilla#2 statefile seed, outputd must
    re-converge to the active-content lane before camilla#2 can safely own the
    round-trip loopback. If that handoff fails, leave camilla#2 unarmed."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_run_audio_hardware_reconcile",
        lambda *, reason: order.append("audio_hardware_failed") or False,
    )

    rc = main(["--reason", "test"])

    assert rc == 1
    assert "bake" in order
    assert "seed" in order
    assert "audio_hardware_failed" in order
    assert "probe" not in order
    assert "arm_camilla2" not in order


def test_main_active_leader_skips_bake_when_camilla1_cannot_restart(
    tmp_path, monkeypatch,
):
    """If a prior failed active-leader attempt left camilla#2 holding the active
    lane, reconcile first releases camilla#2 and reset-starts camilla#1. If
    camilla#1 still cannot come back, do not bake or re-arm camilla#2."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_ensure_unit_active",
        lambda unit, *, reason: order.append("camilla1_start_failed") or False,
    )

    rc = main(["--reason", "test"])

    assert rc == 1
    assert "disable_camilla2" in order
    assert "camilla1_start_failed" in order
    assert "bake" not in order
    assert "arm_camilla2" not in order


def test_main_active_leader_skips_arm_and_restores_when_pcm_busy(
    tmp_path, monkeypatch,
):
    """jts3 EBUSY regression (2026-06-24): a successful camilla#1 bake is not
    proof that snd-aloop substream 5 is closed. If the positive handle probe
    times out busy, camilla#2 is NOT armed; camilla#1 is restored to solo-active
    so the box keeps playing locally and a later reconcile can retry."""
    import jasper.multiroom.active_leader_config as alc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        lambda: order.append("probe_busy")
        or reconcile_mod._PcmHandleProbeResult(
            "busy",
            "timeout",
            detail="state: RUNNING",
            attempts=17,
            timeout_sec=0.8,
        ),
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "active_speaker_baseline.yml",
    )

    rc = main(["--reason", "test"])

    assert rc == 1
    assert order.index("seed") < order.index("probe_busy")
    assert "arm_camilla2" not in order
    assert "leader_restore" in order
    assert "stream_binding" not in order
    # The first part of reconcile still writes the active endpoint snapclient
    # args; fail-closed here is the late camilla handoff, not a permanent unbond.
    body = target.read_text()
    assert reconcile_mod.GROUPING_LOOPBACK_PLAYBACK in body
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "active_content_pcm_busy"' in status
    assert '"active_leader": false' in status


def test_main_active_leader_fails_closed_when_probe_tool_missing(
    tmp_path, monkeypatch,
):
    """Hardening (P1 review #3): `unknown` (probe tool missing) is NOT positive
    proof of release, so the reconciler fails CLOSED — restores solo-active and
    leaves camilla#2 un-armed, exactly like the busy path — rather than arming
    into a possible DAC fight. `cat` is universal on a real Pi, so this branch
    is theoretical-only; the point is that 'can't prove it' never arms."""
    import jasper.multiroom.active_leader_config as alc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        lambda: order.append("probe_missing")
        or reconcile_mod._PcmHandleProbeResult(
            "unknown",
            "probe_tool_missing",
            detail="cat",
            attempts=1,
            timeout_sec=0.8,
        ),
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "active_speaker_baseline.yml",
    )

    rc = main(["--reason", "test"])

    assert rc == 1
    assert order.index("seed") < order.index("probe_missing")
    assert "arm_camilla2" not in order
    assert "leader_restore" in order
    assert "stream_binding" not in order
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "active_content_pcm_unverified"' in status
    assert '"active_leader": false' in status
    # Still wrote the active endpoint snapclient args — fail-closed here is the
    # late camilla handoff, not a permanent unbond.
    assert reconcile_mod.GROUPING_LOOPBACK_PLAYBACK in target.read_text()


def test_main_active_leader_skips_bake_and_arm_when_snapserver_down(
    tmp_path, monkeypatch,
):
    """JTS5 incident regression: snapserver not active (no Snapcast installed, or
    it failed to start) means camilla#1's File-sink bake has no FIFO reader — so
    neither the bake NOR the camilla#2 arm runs. camilla#1 keeps the DAC on its
    solo baseline; no two-instance conflict, no reboot."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "is_active_speaker_box", lambda: True)
    _patch_active_leader(monkeypatch, order)
    # snapserver never came up (the bake gate must refuse).
    monkeypatch.setattr(reconcile_mod, "_unit_is_active", lambda unit: False)

    rc = main(["--reason", "test"])

    assert rc == 1
    assert "disable_camilla2" in order
    assert "bake" not in order  # never baked onto a reader-less pipe
    assert "arm_camilla2" not in order  # never armed camilla#2 (no DAC fight)


# ---------- main(): snapcast provisioning (the grouping opt-in install) ----------


def test_main_provisions_snapcast_when_active_before_units(tmp_path, monkeypatch):
    """A valid enabled bond runs the snapcast opt-in install — BEFORE the units
    come up, so the binaries exist before the snap units start."""
    import jasper.multiroom.provision as provision_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(
        provision_mod, "ensure_snapcast_installed",
        lambda **kw: order.append("provision") or {"state": "installed", "detail": ""},
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert order.index("provision") < order.index("apply")


def test_main_provision_failure_flips_rc_without_crashing(tmp_path, monkeypatch):
    """A failed snapcast install is surfaced (rc=1) but never crashes the
    reconcile — the unit plan still runs (fail-soft; the snap units just fail to
    start and the box stays solo-safe)."""
    import jasper.multiroom.provision as provision_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(
        provision_mod, "ensure_snapcast_installed",
        lambda **kw: {"state": "failed", "detail": "no network"},
    )

    rc = main(["--reason", "test"])

    assert rc == 1
    assert "apply" in order  # the unit plan still ran


def test_main_solo_does_not_provision(tmp_path, monkeypatch):
    """A solo (disabled) box never touches apt — provisioning is gated on a valid
    enabled bond, so a solo speaker carries zero snapcast footprint."""
    import jasper.multiroom.provision as provision_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(
        provision_mod, "ensure_snapcast_installed",
        lambda **kw: order.append("provision") or {"state": "present", "detail": ""},
    )

    main(["--reason", "test"])

    assert "provision" not in order


# ---------- _restart_unit: reset-failed before a deliberate restart ----------
# Regression for the 2026-06-24 jts.local follower reboot: six /grouping/set
# POSTs from the leader in 44 s each restarted jasper-outputd; with no
# reset-failed the 6th tripped outputd's StartLimitBurst and systemd escalated
# to StartLimitAction=reboot, rebooting the Pi from deliberate config churn.


def test_restart_unit_resets_failed_before_restart(monkeypatch):
    """A reconciler restart is a DELIBERATE config-apply: it MUST run
    `systemctl reset-failed <unit>` FIRST so a rapid grouping-config burst
    cannot spend the target's StartLimitBurst and escalate to a Pi reboot."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._restart_unit("jasper-outputd.service") is True
    # reset-failed strictly precedes restart, both targeting the same unit.
    assert calls[0] == ["systemctl", "reset-failed", "jasper-outputd.service"]
    assert calls[1][:2] == ["systemctl", "restart"]
    assert calls[1][-1] == "jasper-outputd.service"


def test_restart_unit_can_queue_cross_owner_restart_no_block(monkeypatch):
    """A grouping voice-route change kicks the AEC reconciler, which owns
    jasper-voice/jasper-aec-bridge. That cross-owner handoff must be queued so
    grouping cannot wait behind voice startup and wedge the unit graph."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._restart_unit(
        reconcile_mod.AEC_RECONCILE_UNIT, no_block=True,
    ) is True
    assert calls[0] == [
        "systemctl", "reset-failed", reconcile_mod.AEC_RECONCILE_UNIT,
    ]
    assert calls[1] == [
        "systemctl", "restart", "--no-block", reconcile_mod.AEC_RECONCILE_UNIT,
    ]


def test_restart_unit_reset_failed_is_fail_soft(monkeypatch):
    """reset-failed is best-effort: a reset-failed failure must NOT block the
    restart it precedes — the restart is the load-bearing action."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "reset-failed":
            raise FileNotFoundError("systemctl")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._restart_unit("jasper-camilla.service") is True
    assert ["systemctl", "restart", "jasper-camilla.service"] in calls


def test_restart_unit_reports_real_restart_failure(monkeypatch):
    """reset-failed succeeding must not mask a real restart failure — the
    caller still sees False (and flips the reconcile exit code)."""
    import subprocess as sp

    def fake_run(argv, **kw):
        if argv[1] == "reset-failed":
            return sp.CompletedProcess(argv, 0, stdout="", stderr="")
        raise sp.CalledProcessError(1, argv, stderr="Job for unit failed.")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._restart_unit("jasper-outputd.service") is False
