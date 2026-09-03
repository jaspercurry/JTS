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

import dataclasses
import fcntl
import os

import pytest

from jasper.ring_assets import (
    RING_ACTIVE_CONTENT_FILE,
    RING_WRITER_LOCK_SUFFIX,
    ring_writer_lock_path,
)
from jasper.multiroom.config import (
    DEFAULT_BUFFER_MS,
    DEFAULT_CODEC,
    BondMember,
    GroupingConfig,
)
from tests._bonded_member import bonded_grouping_env

from jasper.audio_hardware import dac as _dac
from jasper.fanin_coupling import dac_content_lane_marker_armed
from jasper.multiroom import reconcile as reconcile_mod
from jasper.multiroom.dac_content_ring import (
    DAC_CONTENT_LANE_ENV,
    DAC_CONTENT_RING_PCM,
    DAC_CONTENT_RING_PERIOD_FRAMES,
)
from jasper.multiroom.grouping_ring import GROUPING_RING_PCM
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


def _leader(
    *,
    channel="left",
    bond_id="living-room",
    buffer_ms=DEFAULT_BUFFER_MS,
    codec=DEFAULT_CODEC,
) -> GroupingConfig:
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


def _follower(
    *,
    channel="right",
    bond_id="living-room",
    leader_addr="192.168.1.50",
    buffer_ms=DEFAULT_BUFFER_MS,
    codec=DEFAULT_CODEC,
) -> GroupingConfig:
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


def test_plan_disabled_owns_only_snap_units():
    """Source restore is delegated to the canonical source coordinator."""
    p = plan(_disabled())
    by_unit = {i.unit: i.desired for i in p.intents}
    assert by_unit == {SNAPSERVER_UNIT: "stop", SNAPCLIENT_UNIT: "stop"}


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
    """Fail-safe: a broken bond stops both snap units; sources have one owner."""
    p = plan(_invalid())
    assert {(i.unit, i.desired) for i in p.intents} == {
        (SNAPSERVER_UNIT, "stop"),
        (SNAPCLIENT_UNIT, "stop"),
    }


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


def test_snapserver_argv_passes_buffer_as_stream_buffer_flag():
    # buffer_ms is the GLOBAL --stream.buffer (snapcast's end-to-end
    # playout buffer), NOT a pipe:// source-URL param. snapcast silently
    # ignores an unknown &buffer_ms= query key, so the old URL form was
    # inert and the bond ran snapcast's 1000 ms default.
    argv = snapserver_argv(_leader(buffer_ms=750))
    assert "--stream.buffer" in argv
    assert argv[argv.index("--stream.buffer") + 1] == "750"
    # Regression guard: the inert source-URL param must never come back.
    assert "buffer_ms=" not in " ".join(argv)


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
    cfg = GroupingConfig(**{**cfg.__dict__, "client_latency_ms": 17})
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


# ---------- snapclient_argv(): the player device ----------------------------
#
# `player_alsa_device` is the only player form. Both bonded shapes name an SHM
# ring PCM here and differ only in WHICH one (see _assemble_args); unset is the
# bare command.


def test_snapclient_argv_bare_when_no_player_device():
    """`player_alsa_device` unset (the default) leaves the command
    BYTE-FOR-BYTE bare — no player is named at all, so a caller that passes
    nothing cannot half-wire one."""
    cfg = _follower(leader_addr="jts3.local")
    assert snapclient_argv(cfg) == [
        "snapclient",
        "--host",
        "jts3.local",
        "--latency",
        "0",
    ]
    assert snapclient_argv(cfg, player_alsa_device=None) == snapclient_argv(cfg)
    assert "--player" not in snapclient_argv(cfg)


def test_snapclient_argv_names_the_device_through_the_alsa_player():
    """A named device is written through snapclient's `alsa` player
    (--soundcard <dev> --player alsa) — the one player form there is."""
    dev = GROUPING_RING_PCM
    argv = snapclient_argv(_follower(), player_alsa_device=dev)
    assert argv[argv.index("--soundcard") + 1] == dev
    assert argv[argv.index("--player") + 1] == "alsa"


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
    d = _assemble_args(_leader())
    assert not d[CLIENT_KEY].split()[0] == "snapclient"
    # A DUMB member's client (the default, active_endpoint=False) is the
    # dac-content RETURN ring form. NOT "always": an active-speaker endpoint
    # names the grouping ring instead — pinned by
    # test_assemble_args_client_names_one_ring_per_shape below.
    assert d[CLIENT_KEY] == " ".join(
        snapclient_argv(_leader(), player_alsa_device=DAC_CONTENT_RING_PCM)[1:]
    )


def test_assemble_args_leader_server_carries_the_fifo_source():
    d = _assemble_args(_leader())
    assert SNAPFIFO in d[SERVER_KEY]
    assert "pipe://" in d[SERVER_KEY]


def test_assemble_args_follower_server_empty_client_set():
    d = _assemble_args(_follower(leader_addr="192.168.1.50"))
    assert d[SERVER_KEY] == ""  # a follower runs no server
    assert d[CLIENT_KEY]  # but does run a client
    assert "--host 192.168.1.50" in d[CLIENT_KEY]


@pytest.mark.parametrize(
    ("active_endpoint", "ring", "other_ring"),
    [
        (False, DAC_CONTENT_RING_PCM, GROUPING_RING_PCM),
        (True, GROUPING_RING_PCM, DAC_CONTENT_RING_PCM),
    ],
)
def test_assemble_args_client_names_one_ring_per_shape(
    active_endpoint, ring, other_ring,
):
    """ONE snapclient shape, two rings — which one is the whole decision.

    A DUMB member (the default, active_endpoint=False) writes the dac-content
    RETURN ring, which outputd reads as its sole content source. An ACTIVE
    endpoint writes the grouping ring, which its own CamillaDSP captures to run
    Layer A in the bonded path. Each shape names its own ring and never the
    other's — swapping them would silence the box. Neither is the raw DAC, which
    outputd owns."""
    d = _assemble_args(_follower(), active_endpoint=active_endpoint)
    assert d[SERVER_KEY] == ""  # a follower runs no server
    assert f"--soundcard {ring} --player alsa" in d[CLIENT_KEY]
    assert other_ring not in d[CLIENT_KEY]
    assert "alsa:device=default" not in d[CLIENT_KEY]


def test_outputd_grouping_env_active_endpoint_clears_dac_content():
    """An ACTIVE follower disables outputd's dac_content ChannelPick — camilla
    owns the channel-pick + split, so outputd runs its normal active sink. The
    round-trip lane marker is cleared; TTS also stays off outputd because active
    voice rides fan-in upstream of the crossover. A DUMB member still arms the
    lane."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_TTS_SOCKET_ENV,
    )

    active = bonded_grouping_env(_follower(), active_endpoint=True)
    assert active[DAC_CONTENT_LANE_ENV] == ""  # cleared (no dac_content)
    assert active[OUTPUTD_TTS_SOCKET_ENV] == ""
    dumb = bonded_grouping_env(
        _follower(), active_endpoint=False, flat_output_allowed=True
    )
    assert dac_content_lane_marker_armed(dumb)  # dumb member arms the lane


def test_topology_changes_revoke_dac_bypass_without_deleting_bond_intent():
    """Reset and active-layout save both close a bonded passive DAC bypass."""
    from jasper.multiroom.reconcile import _assemble_args, outputd_grouping_env

    bonded = _follower()
    period = DAC_CONTENT_RING_PERIOD_FRAMES
    allowed = outputd_grouping_env(
        bonded, flat_output_allowed=True, outputd_period_frames=period
    )
    after_reset = outputd_grouping_env(
        bonded, flat_output_allowed=False, outputd_period_frames=period
    )
    after_active_save = outputd_grouping_env(
        bonded,
        active_endpoint=True,
        flat_output_allowed=False,
        outputd_period_frames=period,
    )

    assert dac_content_lane_marker_armed(allowed)
    assert after_reset[DAC_CONTENT_LANE_ENV] == ""
    assert after_active_save[DAC_CONTENT_LANE_ENV] == ""
    assert _assemble_args(bonded)[CLIENT_KEY]
    assert _assemble_args(bonded, active_endpoint=True)[CLIENT_KEY]


def test_outputd_grouping_env_emits_sub_corner_only_for_sub():
    """The wireless-sub low-pass corner rides the outputd lane ONLY when the
    member's channel is "sub"; it is ABSENT for every other channel (a non-sub
    member must never carry it)."""

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_SUB_HZ_ENV,
    )

    sub = dataclasses.replace(_follower(channel="sub"), crossover_hz=120.0)
    env = bonded_grouping_env(
        sub,
        flat_output_allowed=True,
    )
    assert env[OUTPUTD_DAC_CONTENT_SUB_HZ_ENV] == "120.0"

    for ch in ("left", "right", "stereo", "mono"):
        env = bonded_grouping_env(
            _follower(channel=ch), flat_output_allowed=True
        )
        assert OUTPUTD_DAC_CONTENT_SUB_HZ_ENV not in env


def test_outputd_grouping_env_highpasses_mains_when_bond_has_sub():

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV,
    )

    leader = dataclasses.replace(
        _leader(channel="left"),
        crossover_hz=100.0,
        roster=(BondMember(addr="192.168.1.8", name="Sub", channel="sub"),),
    )
    assert (
        bonded_grouping_env(
            leader,
            flat_output_allowed=True,
    )[
            OUTPUTD_DAC_CONTENT_HP_HZ_ENV
        ]
        == "100.0"
    )

    follower = dataclasses.replace(
        _follower(channel="right"),
        crossover_hz=100.0,
        subwoofer_present=True,
    )
    assert (
        bonded_grouping_env(
            follower,
            flat_output_allowed=True,
    )[
            OUTPUTD_DAC_CONTENT_HP_HZ_ENV
        ]
        == "100.0"
    )


def test_outputd_grouping_env_clears_main_highpass_when_not_applicable():

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV,
        outputd_grouping_env,
    )

    rostered = dataclasses.replace(
        _leader(channel="left"),
        crossover_hz=90.0,
        roster=(BondMember(addr="192.168.1.8", name="Sub", channel="sub"),),
    )
    assert (
        outputd_grouping_env(
            dataclasses.replace(rostered, mains_highpass_enabled=False)
        )[OUTPUTD_DAC_CONTENT_HP_HZ_ENV]
        == ""
    )
    assert (
        outputd_grouping_env(_leader(channel="left"))[OUTPUTD_DAC_CONTENT_HP_HZ_ENV]
        == ""
    )
    assert (
        outputd_grouping_env(
            dataclasses.replace(_follower(channel="sub"), subwoofer_present=True)
        )[OUTPUTD_DAC_CONTENT_HP_HZ_ENV]
        == ""
    )
    assert (
        outputd_grouping_env(
            dataclasses.replace(_follower(channel="right"), subwoofer_present=True),
            active_endpoint=True,
        )[OUTPUTD_DAC_CONTENT_HP_HZ_ENV]
        == ""
    )
    assert outputd_grouping_env(_disabled())[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == ""


def test_corner_precedence_active_main_defers_wireless_highpass():
    """Revision plan §6 default — corner precedence, mains-HP applied ONCE.

    When a speaker is BOTH an active main AND bonded to a wireless sub, it could
    high-pass the mains TWICE: the wireless mains-HP in outputd's dac_content
    lane, AND its own CamillaDSP active-graph bass-management HP. The
    active-speaker LOCAL config wins — the wireless path defers. Concretely: the
    SAME box (a right-channel main in a bond that contains a sub, mains-HP
    enabled) writes the wireless HP env when it is a DUMB single-DAC member, but
    that env is CLEARED when it is an ACTIVE endpoint (its CamillaDSP owns the
    corner). So the corner is applied exactly once regardless of which path the
    box takes.
    """

    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_HP_HZ_ENV,
        outputd_grouping_env,
    )

    # A right-channel main; the bond has a sub; mains high-pass armed; corner 100.
    active_main = dataclasses.replace(
        _follower(channel="right"),
        crossover_hz=100.0,
        subwoofer_present=True,
        mains_highpass_enabled=True,
    )

    # As a DUMB member (active_endpoint=False): the wireless HP IS applied here.
    dumb = bonded_grouping_env(
        active_main,
        active_endpoint=False,
        flat_output_allowed=True,
    )
    assert dumb[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == "100.0"

    # As an ACTIVE endpoint: the wireless HP DEFERS (cleared) — the box's own
    # CamillaDSP graph owns the corner, so mains-HP is applied exactly once
    # there, never stacked with this lane.
    active = outputd_grouping_env(active_main, active_endpoint=True)
    assert active[OUTPUTD_DAC_CONTENT_HP_HZ_ENV] == ""


def test_corner_precedence_active_graph_highpass_uses_shared_corner():
    """The 'applied once' side of §6: the active-speaker CamillaDSP graph's
    mains bass-management high-pass is emitted at the LocalSubwoofer corner —
    the SAME shared bass-management corner the deferred wireless path would have
    used. Real-shape: parse the emitted startup graph and find the mains'
    lowest-driver LinkwitzRileyHighpass at the sub corner.
    """
    import yaml

    from jasper.active_speaker.camilla_yaml import (
        emit_active_speaker_baseline_config,
    )
    from jasper.active_speaker.profile import ActiveSpeakerPreset
    from jasper.camilla_emit import BASS_MANAGEMENT_CORNER_HZ_DEFAULT

    # A 2-way active main WITH a local sub at the shared default corner.
    preset = ActiveSpeakerPreset.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_preset",
            "preset_id": "p5_active_main_with_sub",
            "name": "P5 active main + sub",
            "way_count": 2,
            "channel_map": {
                "layout": "mono",
                "outputs": [
                    {
                        "index": 0,
                        "side": "mono",
                        "driver_role": "woofer",
                        "label": "Woofer",
                    },
                    {
                        "index": 1,
                        "side": "mono",
                        "driver_role": "tweeter",
                        "label": "Tweeter",
                    },
                ],
            },
            "drivers": {
                "woofer": {"manufacturer": "Acme", "model": "W1"},
                "tweeter": {"manufacturer": "Acme", "model": "T1"},
            },
            "crossover_regions": [
                {
                    "id": "wt",
                    "lower_driver": "woofer",
                    "upper_driver": "tweeter",
                    "fc_hz": 2000.0,
                }
            ],
            "local_subwoofer": {
                "physical_output_index": 2,
                "label": "Sub",
                "crossover_fc_hz": BASS_MANAGEMENT_CORNER_HZ_DEFAULT,
            },
        }
    )
    doc = yaml.safe_load(
        emit_active_speaker_baseline_config(
            preset,
            playback_device="hw:TestDAC",
            baseline_id="p5-precedence-test",
        )
    )
    filters = doc["filters"]
    # There is a LinkwitzRileyHighpass at the sub corner (the mains'
    # bass-management HP) — proving the active graph applies the corner locally.
    hp_at_corner = [
        f
        for f in filters.values()
        if isinstance(f, dict)
        and f.get("type") == "BiquadCombo"
        and f.get("parameters", {}).get("type") == "LinkwitzRileyHighpass"
        and f["parameters"].get("freq") == BASS_MANAGEMENT_CORNER_HZ_DEFAULT
    ]
    assert hp_at_corner, "active graph must high-pass mains at the sub corner"


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
        _follower(channel="sub"),
        active_endpoint=True,
    )
    assert active_sub[OUTPUTD_TTS_SOCKET_ENV] == ""

    for ch in ("left", "right", "stereo", "mono"):
        env = outputd_grouping_env(_follower(channel=ch))
        assert env[OUTPUTD_TTS_SOCKET_ENV] == OUTPUTD_TTS_SOCKET
        active_env = outputd_grouping_env(
            _follower(channel=ch),
            active_endpoint=True,
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
    assert "--stream.buffer 750" in d[SERVER_KEY]
    # The inert source-URL param must not be re-introduced.
    assert "buffer_ms=" not in d[SERVER_KEY]


# ---------- _write_args_file(): atomic, mode 0644, fail-soft ----------


def test_write_args_file_round_trips_keys(tmp_path, monkeypatch):
    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    keys = {
        SERVER_KEY: "--stream.source pipe://x",
        CLIENT_KEY: "--host 127.0.0.1 --latency 0",
    }
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
    assert (
        _write_args_file({SERVER_KEY: "x", CLIENT_KEY: "y"}, path=str(target)) is False
    )
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

    Patches: the args + outputd-env files into tmp_path; the box's resolved
    outputd period to the ring's slot; load_config to the synthetic cfg;
    _apply + _restart_outputd
    to order-recording fakes; and the leader_config sync entrypoints to
    spies (main from-imports them at call time, so patching the
    leader_config MODULE attributes intercepts them)."""
    import jasper.multiroom.leader_config as leader_config_mod

    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    monkeypatch.setattr(reconcile_mod, "ARGS_FILE", str(target))
    monkeypatch.setattr(
        reconcile_mod,
        "OUTPUTD_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-outputd.env"),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "VOICE_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-voice.env"),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "AIRPLAY_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-airplay.env"),
    )
    # The box runs the return ring's slot, so the FOURTH arming gate passes and
    # the dumb-member branch is the one under test. The mismatching box has its
    # own test (test_a_period_that_cannot_carry_the_return_ring_stays_solo).
    monkeypatch.setattr(
        reconcile_mod,
        "box_outputd_period_frames",
        lambda: DAC_CONTENT_RING_PERIOD_FRAMES,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "FOLLOWER_STATUS_FILE",
        str(tmp_path / "grouping-follower-status.json"),
    )
    # macOS has no Linux /proc boot_id. Give every reconciler main() test one
    # stable synthetic boot identity; dedicated effective-role tests exercise
    # missing/malformed/mismatched readers.
    monkeypatch.setattr(
        reconcile_mod,
        "read_current_boot_id",
        lambda: "11111111-1111-4111-8111-111111111111",
    )
    # Default to the PASSIVE path (these legacy main() tests assert dumb-member
    # behavior); the active-follower main() flow has its own tests that override
    # this. is_active_speaker_box reads the topology, so stub it for hermeticity.
    monkeypatch.setattr(
        reconcile_mod, "_output_topology_state", lambda: (False, True)
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: False,
    )
    monkeypatch.setattr("jasper.multiroom.config.load_config", lambda *a, **k: cfg)
    # Snapcast provisioning (main() calls it for any enabled bond): default to a
    # present no-op so these tests never shell out to apt. The provisioning tests
    # override it. main() from-imports it, so patch the provision module attr.
    import jasper.multiroom.provision as provision_mod

    monkeypatch.setattr(
        provision_mod,
        "ensure_snapcast_installed",
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
        reconcile_mod,
        "_restart_outputd",
        lambda: order.append("outputd_restart") or True,
    )

    def _fake_restart(unit, *, no_block=False, active_only=False):
        suffix = ":no_block" if no_block else ""
        verb = "try-restart" if active_only else "restart"
        order.append(f"{verb}:{unit}{suffix}")
        return True

    monkeypatch.setattr(reconcile_mod, "_restart_unit", _fake_restart)
    monkeypatch.setattr(
        reconcile_mod,
        "_converge_sources_after_role",
        lambda **_kwargs: (
            order.append(f"start-owner:{reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT}")
            or True
        ),
    )
    monkeypatch.setattr(
        leader_config_mod,
        "apply_bonded_leader_config_sync",
        lambda cfg_: order.append("camilla_bonded") or "bonded.yml",
    )
    monkeypatch.setattr(
        leader_config_mod,
        "restore_solo_config_sync",
        lambda: order.append("camilla_restore_check") and None,
    )
    import jasper.multiroom.snapcast_rpc as snapcast_rpc_mod

    monkeypatch.setattr(
        snapcast_rpc_mod,
        "ensure_groups_on_stream",
        lambda want, **kw: (
            order.append("stream_binding")
            or {
                "reachable": True,
                "groups": 1,
                "fixed": 0,
                "failed": 0,
            }
        ),
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
    assert SNAPFIFO in text  # server reads the fifo
    assert f"\n{CLIENT_KEY}=--host 127.0.0.1" in text  # leader client → loopback


def test_main_follower_writes_client_only(tmp_path, monkeypatch):
    target, order = _patch_main_io(
        monkeypatch, tmp_path, _follower(leader_addr="192.168.1.50")
    )
    rc = main([])
    assert rc == 0
    text = target.read_text()
    assert f"{SERVER_KEY}=\n" in text  # follower: server empty
    assert f"{CLIENT_KEY}=--host 192.168.1.50" in text


def test_follower_aborts_if_fail_safe_source_deny_cannot_be_published(
    tmp_path,
    monkeypatch,
):
    import json

    from jasper.multiroom.effective_role import grouping_request_fingerprint

    requested = _follower(leader_addr="192.168.1.50")
    _target, order = _patch_main_io(monkeypatch, tmp_path, requested)
    status_path = tmp_path / "grouping-follower-status.json"
    status_path.write_text(
        json.dumps(
            {
                "active_follower": False,
                "active_leader": False,
                "blocked_reason": "old_refusal",
                "requested_fingerprint": grouping_request_fingerprint(requested),
                "local_sources_allowed": True,
            }
        )
    )

    def fail_status_write(*_args, **_kwargs):
        raise OSError("read-only status")

    monkeypatch.setattr(
        reconcile_mod.atomic_io,
        "atomic_write_text",
        fail_status_write,
    )

    assert main([]) == 1
    assert "apply" not in order
    assert not any(item.startswith("start-owner:") for item in order)
    assert json.loads(status_path.read_text())["local_sources_allowed"] is True


def test_main_disabled_writes_empty_args(tmp_path, monkeypatch):
    target, _order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    rc = main([])
    assert rc == 0
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_solo_fails_before_role_mutation_when_boot_id_cannot_bind_grant(
    tmp_path,
    monkeypatch,
):
    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "read_current_boot_id", lambda: "")

    assert main([]) == 1
    assert "apply" not in order
    assert not any(item.startswith("start-owner:") for item in order)


@pytest.mark.parametrize("prior_active_follower", [True, False])
def test_follower_to_solo_keeps_sources_denied_until_role_lands(
    tmp_path,
    monkeypatch,
    prior_active_follower,
):
    import json

    from jasper.multiroom.effective_role import grouping_request_fingerprint

    requested = _disabled()
    previous = _follower(leader_addr="192.168.1.50")
    target, order = _patch_main_io(monkeypatch, tmp_path, requested)
    status_path = tmp_path / "grouping-follower-status.json"
    boot_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(reconcile_mod, "read_current_boot_id", lambda: boot_id)
    status_path.write_text(
        json.dumps(
            {
                "active_follower": prior_active_follower,
                "active_leader": False,
                "blocked_reason": "",
                "requested_fingerprint": grouping_request_fingerprint(previous),
                "local_sources_allowed": False,
                "boot_id": boot_id,
            }
        )
    )

    def apply_after_deny(_decision):
        assert target.exists()
        in_progress = json.loads(status_path.read_text())
        assert in_progress["requested_fingerprint"] == (
            grouping_request_fingerprint(requested)
        )
        assert in_progress["blocked_reason"] == "role_transition_in_progress"
        assert in_progress["local_sources_allowed"] is False
        order.append("apply")
        return 0

    def converge_after_grant(**_kwargs):
        landed = json.loads(status_path.read_text())
        assert landed["local_sources_allowed"] is True
        assert landed["blocked_reason"] == ""
        order.append("source-after-grant")
        return True

    monkeypatch.setattr(reconcile_mod, "_apply", apply_after_deny)
    monkeypatch.setattr(
        reconcile_mod,
        "_converge_sources_after_role",
        converge_after_grant,
    )

    assert main([]) == 0
    final = json.loads(status_path.read_text())
    assert final["boot_id"] == boot_id
    assert final["local_sources_allowed"] is True
    assert order.index("apply") < order.index("source-after-grant")


@pytest.mark.parametrize("prior_active_follower", [True, False])
def test_failed_follower_to_solo_transition_stays_parked_across_retry(
    tmp_path,
    monkeypatch,
    prior_active_follower,
):
    import json

    from jasper.multiroom.effective_role import grouping_request_fingerprint

    requested = _disabled()
    previous = _follower(leader_addr="192.168.1.50")
    _target, order = _patch_main_io(monkeypatch, tmp_path, requested)
    status_path = tmp_path / "grouping-follower-status.json"
    boot_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(reconcile_mod, "read_current_boot_id", lambda: boot_id)
    status_path.write_text(
        json.dumps(
            {
                "active_follower": prior_active_follower,
                "active_leader": False,
                "blocked_reason": "",
                "requested_fingerprint": grouping_request_fingerprint(previous),
                "local_sources_allowed": False,
                "boot_id": boot_id,
            }
        )
    )

    apply_attempt = 0

    def apply_while_denied(_decision):
        nonlocal apply_attempt
        apply_attempt += 1
        status = json.loads(status_path.read_text())
        assert status["local_sources_allowed"] is False
        assert status["blocked_reason"] == "role_transition_in_progress"
        return 1 if apply_attempt == 1 else 0

    monkeypatch.setattr(reconcile_mod, "_apply", apply_while_denied)

    def converge_after_role(**_kwargs):
        status = json.loads(status_path.read_text())
        if apply_attempt == 1:
            assert status["local_sources_allowed"] is False
            assert status["blocked_reason"] == "role_transition_in_progress"
            order.append("source-stays-parked")
        else:
            assert status["local_sources_allowed"] is True
            assert status["blocked_reason"] == ""
            order.append("source-after-retry-grant")
        return True

    monkeypatch.setattr(
        reconcile_mod,
        "_converge_sources_after_role",
        converge_after_role,
    )

    assert main([]) == 1
    after_failure = json.loads(status_path.read_text())
    assert after_failure["local_sources_allowed"] is False
    assert "source-stays-parked" in order

    # The first failed attempt already rewrote status with the *current*
    # request fingerprint. The retry must still recognize the explicit deny as
    # an unfinished transition, rather than publishing a grant before apply.
    assert main([]) == 0
    final = json.loads(status_path.read_text())
    assert final["local_sources_allowed"] is True
    assert apply_attempt == 2
    assert "source-after-retry-grant" in order


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
        "write",
        "outputd_restart",
        "restart:jasper-aec-reconcile.service:no_block",
        "apply",
        "try-restart:shairport-sync.service",
        "camilla_bonded",
        "stream_binding",
        "start-owner:jasper-source-intent-reconcile.service",
    ]


def test_main_leader_second_run_skips_outputd_restart(tmp_path, monkeypatch):
    """Compare-before-write: an unchanged outputd lane env must NOT
    restart outputd on the next reconcile (no churn on no-change runs)."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    order.clear()
    assert main([]) == 0
    # no outputd/voice restart on the unchanged second run
    assert order == [
        "write",
        "apply",
        "camilla_bonded",
        "stream_binding",
        "start-owner:jasper-source-intent-reconcile.service",
    ]


def test_main_nonleader_runs_solo_restore_not_bonded_apply(tmp_path, monkeypatch):
    """A follower / solo reconcile goes through the restore path (a no-op
    when already solo) and never the bonded apply."""
    for cfg in (_follower(leader_addr="192.168.1.50"), _disabled()):
        _target, order = _patch_main_io(monkeypatch, tmp_path, cfg)
        assert main([]) == 0
        assert "camilla_bonded" not in order
        assert "camilla_restore_check" in order


@pytest.mark.parametrize("cfg", [_follower(), _disabled(), _leader()])
def test_role_apply_hands_all_sources_to_canonical_owner(
    cfg,
    tmp_path,
    monkeypatch,
):
    """Follower park and solo/leader restore use the one source owner."""

    _target, order = _patch_main_io(monkeypatch, tmp_path, cfg)
    assert main([]) == 0
    apply_idx = order.index("apply")
    source_idx = order.index("start-owner:jasper-source-intent-reconcile.service")
    assert apply_idx < source_idx
    assert not any("accessory-reconcile" in entry for entry in order)
    assert not any("fanin-coupling" in entry for entry in order)


def test_main_writes_outputd_env_for_member_and_clears_for_solo(tmp_path, monkeypatch):
    """The outputd lane env carries the ring MARKER + channel while bonded and
    explicit empty strings after disband (disable-clears-stale). The legacy FIFO
    key is cleared on BOTH paths — arming the marker beside a path is the
    two-content-sources shape outputd refuses."""
    _target, _order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    env = (tmp_path / "grouping-outputd.env").read_text()
    assert f"{DAC_CONTENT_LANE_ENV}=1\n" in env
    assert f"{reconcile_mod.OUTPUTD_DAC_CONTENT_FIFO_ENV}=\n" in env
    assert "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL=left" in env

    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    assert main([]) == 0
    env = (tmp_path / "grouping-outputd.env").read_text()
    assert f"{DAC_CONTENT_LANE_ENV}=\n" in env
    assert f"{reconcile_mod.OUTPUTD_DAC_CONTENT_FIFO_ENV}=\n" in env
    assert "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL=\n" in env
    assert "outputd_restart" in order  # env changed bonded→cleared ⇒ restart


def test_main_leader_writes_airplay_offset_and_refreshes_active_shairport(
    tmp_path,
    monkeypatch,
):
    """A bonded leader folds the Snapcast buffer into its AirPlay offset
    (grouping-airplay.env) and restarts shairport — AFTER the unit plan —
    so ExecStartPre re-derives the offset."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0
    env = (tmp_path / "grouping-airplay.env").read_text()
    assert "JASPER_AIRPLAY_BONDED_EXTRA_DELAY_SEC=0.400000" in env
    assert "try-restart:shairport-sync.service" in order
    assert order.index("apply") < order.index("try-restart:shairport-sync.service")


def test_main_solo_writes_no_airplay_offset_and_skips_shairport_restart(
    tmp_path, monkeypatch
):
    """INVARIANT: a solo speaker that was never bonded writes NO bonded key
    and never restarts shairport — its AirPlay offset stays byte-identical."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    assert main([]) == 0
    assert "try-restart:shairport-sync.service" not in order
    p = tmp_path / "grouping-airplay.env"
    # Empty body on a fresh file is not written at all (no spurious churn).
    assert not p.exists() or "BONDED_EXTRA_DELAY" not in p.read_text()


def test_main_unbond_refreshes_only_active_shairport_to_restore_solo_offset(
    tmp_path,
    monkeypatch,
):
    """bonded leader -> solo: the airplay offset env clears and shairport is
    restarted so its offset reverts to the solo value (restore-on-unbond — a
    stranded bonded offset would make solo audio play early)."""
    _patch_main_io(monkeypatch, tmp_path, _leader())
    assert main([]) == 0  # bond writes the delta
    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    assert main([]) == 0  # unbond clears it
    assert "try-restart:shairport-sync.service" in order
    assert "BONDED_EXTRA_DELAY" not in (tmp_path / "grouping-airplay.env").read_text()


def test_main_follower_does_not_restart_shairport(tmp_path, monkeypatch):
    """A bonded FOLLOWER's shairport is PARKED by the plan; the airplay-offset
    path must never restart (un-park) it."""
    _target, order = _patch_main_io(
        monkeypatch, tmp_path, _follower(leader_addr="192.168.1.50")
    )
    assert main([]) == 0
    assert "try-restart:shairport-sync.service" not in order


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
        snapcast_rpc_mod,
        "ensure_groups_on_stream",
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
        leader_config_mod,
        "apply_bonded_leader_config_sync",
        _boom,
    )
    rc = main([])
    assert rc == 1
    assert "apply" in order  # units still managed


# ---------- the leader's music-producer predicate ----------
# (The outputd-as-producer tap machinery — env write/read, change-gate,
# try-restart, SNAPFIFO_PRODUCER_WIRED — was REMOVED 2026-06-11 with the
# canonical design. desired_snapfifo_path survives as the pure "this role
# needs a producer" predicate driving the runtime-health derive.)


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
    monkeypatch,
    tmp_path,
):
    """The first-write-empty rule, pinned: a FRESH solo speaker's first
    reconcile must neither create an empty grouping-voice.env nor restart
    jasper-voice (a ~10-15 s outage on every first boot otherwise). The
    rule lives in _write_derived_env (absent file + empty body = no
    change)."""
    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    rc = reconcile_mod.main(["--reason", "test"])
    assert rc == 0
    assert not (tmp_path / "grouping-voice.env").exists()
    assert "restart:jasper-aec-reconcile.service:no_block" not in order
    assert "restart:jasper-voice.service" not in order


# ---------- dumb-follower profile: park + restore + absent-unit no-ops ----


def test_plan_follower_owns_only_snap_units():
    """Source parking is delegated after the grouping data plane lands."""
    p = plan(_follower())
    by_unit = {i.unit: i.desired for i in p.intents}
    assert by_unit == {SNAPSERVER_UNIT: "stop", SNAPCLIENT_UNIT: "start"}


def test_plan_leader_owns_only_snap_units():
    p = plan(_leader())
    by_unit = {i.unit: i.desired for i in p.intents}
    assert by_unit == {SNAPSERVER_UNIT: "start", SNAPCLIENT_UNIT: "start"}


@pytest.mark.parametrize(
    ("active_states", "expect_changed"),
    [
        pytest.param(
            {SNAPSERVER_UNIT: "inactive", SNAPCLIENT_UNIT: "inactive"},
            False,
            id="already_stopped_matches_stop_plan",
        ),
        pytest.param(
            {SNAPSERVER_UNIT: "active", SNAPCLIENT_UNIT: "inactive"},
            True,
            id="live_unit_still_running_is_a_change",
        ),
        pytest.param(
            {SNAPSERVER_UNIT: "unknown-state", SNAPCLIENT_UNIT: "inactive"},
            True,
            id="unproven_probe_counts_as_changed",
        ),
    ],
)
def test_plan_changes_units_reflects_live_state(
    monkeypatch,
    active_states,
    expect_changed,
):
    """A `stop` intent against an already-inactive unit is not a change; a
    live/unproven unit is. This gates whether main() can skip the post-role
    source barrier."""
    import subprocess as sp
    from jasper.multiroom.reconcile import _plan_changes_units

    def fake_run(argv, **kw):
        unit = argv[2]
        return sp.CompletedProcess(
            argv,
            0,
            stdout=f"{active_states[unit]}\n",
            stderr="",
        )

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    solo_plan = plan(_disabled())
    assert _plan_changes_units(solo_plan.intents) is expect_changed


def _apply_with_fake_systemctl(monkeypatch, intents, *, enabled=(), absent=()):
    """Run _apply with subprocess.run faked. Returns (rc, calls) where
    calls is the list of argv lists systemctl saw."""
    import subprocess as sp
    from jasper.multiroom.reconcile import ReconcilePlan, _apply

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        verb, unit = argv[1], argv[-1]
        if verb == "is-enabled":
            is_enabled = unit in enabled
            return sp.CompletedProcess(
                argv,
                0 if is_enabled else 1,
                stdout="enabled\n" if is_enabled else "disabled\n",
                stderr="",
            )
        if unit in absent:
            if kw.get("check"):
                raise sp.CalledProcessError(
                    5,
                    argv,
                    stderr=f"Failed to {verb} {unit}: Unit not loaded.",
                )
            return sp.CompletedProcess(argv, 5)
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    rc = _apply(ReconcilePlan(intents=tuple(intents), summary="test"))
    return rc, calls


def test_apply_absent_unit_is_a_clean_noop(monkeypatch):
    """A streambox box never installs some full-speaker units — stop
    intents against absent units must not flip the exit code (absent
    units are no-ops)."""
    from jasper.multiroom.reconcile import UnitIntent

    rc, calls = _apply_with_fake_systemctl(
        monkeypatch,
        [
            UnitIntent("ghost.service", "stop", "t"),
            UnitIntent("real.service", "stop", "t"),
        ],
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
    rc = _apply(
        ReconcilePlan(intents=(UnitIntent("x.service", "stop", "t"),), summary="t")
    )
    assert rc == 1


def test_apply_timeout_is_bounded_and_does_not_skip_later_intents(monkeypatch):
    """A wedged local unit job fails this intent without hanging direct CLI."""
    import subprocess as sp
    from jasper.multiroom.reconcile import ReconcilePlan, UnitIntent, _apply

    calls: list[tuple[list[str], float]] = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw["timeout"]))
        if argv[-1] == "wedged.service":
            raise sp.TimeoutExpired(argv, kw["timeout"])
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    rc = _apply(
        ReconcilePlan(
            intents=(
                UnitIntent("wedged.service", "stop", "t"),
                UnitIntent("healthy.service", "stop", "t"),
            ),
            summary="t",
        )
    )
    assert rc == 1
    assert calls == [
        (
            ["systemctl", "stop", "wedged.service"],
            reconcile_mod._SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        ),
        (
            ["systemctl", "stop", "healthy.service"],
            reconcile_mod._SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        ),
    ]


def test_main_follower_voice_env_kicks_aec_reconcile_with_park_flag(
    tmp_path,
    monkeypatch,
):
    """Bond-form on a follower: grouping-voice.env carries the validated
    park flag and the change is applied via ONE kick of
    jasper-aec-reconcile (the voice/bridge unit owner) — never a direct
    jasper-voice restart from this reconciler."""
    from jasper.multiroom.reconcile import VOICE_PARK_ENV

    _target, order = _patch_main_io(
        monkeypatch,
        tmp_path,
        _follower(leader_addr="192.168.1.50"),
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
    boot_id = "11111111-1111-4111-8111-111111111111"
    read_boot_id = lambda: boot_id
    _write_follower_status(
        active_follower=True,
        blocked_reason="",
        path=p,
        boot_id_reader=read_boot_id,
    )
    assert json.loads(open(p).read()) == {
        "active_follower": True,
        "active_leader": False,
        "blocked_reason": "",
        "boot_id": boot_id,
    }
    # An active LEADER runs camilla#2 as the bond leader (Slice 5).
    _write_follower_status(
        active_follower=False,
        active_leader=True,
        blocked_reason="",
        path=p,
        boot_id_reader=read_boot_id,
    )
    assert json.loads(open(p).read()) == {
        "active_follower": False,
        "active_leader": True,
        "blocked_reason": "",
        "boot_id": boot_id,
    }
    # Rewritten every reconcile — a later blocked state replaces the prior one.
    _write_follower_status(
        active_follower=False,
        blocked_reason="graph_unprovable",
        path=p,
        boot_id_reader=read_boot_id,
    )
    assert json.loads(open(p).read()) == {
        "active_follower": False,
        "active_leader": False,
        "blocked_reason": "graph_unprovable",
        "boot_id": boot_id,
    }


def test_write_follower_status_refuses_unbound_persistent_grant(tmp_path):
    import json

    p = str(tmp_path / "grouping-follower-status.json")
    assert (
        reconcile_mod._write_follower_status(
            active_follower=False,
            blocked_reason="role_transition_in_progress",
            local_sources_allowed=True,
            path=p,
            boot_id_reader=lambda: "malformed",
        )
        is False
    )
    status = json.loads(open(p).read())
    assert status["boot_id"] == ""
    assert status["local_sources_allowed"] is False


# ---------- main(): ACTIVE follower flow (Slice 3) ----------


def test_main_active_follower_prechecks_early_then_swaps_camilla_after_units(
    tmp_path,
    monkeypatch,
):
    """An active follower: the readiness GATE runs BEFORE the units (fail-safe),
    snapclient writes the grouping ring (not the FIFO), and the CamillaDSP swap
    runs AFTER the unit plan (so the ring has its writer)."""
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    monkeypatch.setattr(
        fc_mod,
        "precheck_active_follower_sync",
        lambda cfg_: order.append("precheck") or "grouping_follower.yml",
    )
    monkeypatch.setattr(
        fc_mod,
        "apply_prebuilt_follower_config_sync",
        lambda: order.append("camilla_active_follower") or "grouping_follower.yml",
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    # Gate before units; camilla swap after the unit plan.
    assert order.index("precheck") < order.index("apply")
    assert order.index("apply") < order.index("camilla_active_follower")
    # snapclient targets the grouping ring, not the dumb member's return ring.
    body = target.read_text()
    assert GROUPING_RING_PCM in body
    assert DAC_CONTENT_RING_PCM not in body
    # endpoint status persisted as active_crossover.
    status = tmp_path / "grouping-follower-status.json"
    assert '"active_follower": true' in status.read_text()


def test_main_active_follower_precheck_failure_falls_back_to_solo(
    tmp_path,
    monkeypatch,
):
    """If the readiness gate fails (can't make the driver-domain graph safe),
    the box does NOT bond — it fails safe to SOLO: no CamillaDSP follower swap,
    snapclient cleared, the active-solo restore runs, and the block reason is
    surfaced for /state (invariant 5 fail-closed + self-recovery)."""
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))

    def _boom(cfg_):
        raise fc_mod.ActiveFollowerError("graph_unprovable", "nope")

    monkeypatch.setattr(fc_mod, "precheck_active_follower_sync", _boom)
    monkeypatch.setattr(
        fc_mod,
        "apply_prebuilt_follower_config_sync",
        lambda: order.append("camilla_active_follower"),
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
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
    assert '"local_sources_allowed": true' in status


def test_refused_follower_restore_failure_keeps_sources_parked(
    tmp_path,
    monkeypatch,
):
    import json

    import jasper.multiroom.follower_config as fc_mod

    _patch_main_io(monkeypatch, tmp_path, _follower())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))

    def fail_precheck(_cfg):
        raise fc_mod.ActiveFollowerError("graph_unprovable", "nope")

    def fail_restore():
        raise RuntimeError("restore failed")

    monkeypatch.setattr(fc_mod, "precheck_active_follower_sync", fail_precheck)
    monkeypatch.setattr(fc_mod, "restore_active_follower_solo_sync", fail_restore)

    assert main(["--reason", "test"]) == 1
    status = json.loads((tmp_path / "grouping-follower-status.json").read_text())
    assert status["blocked_reason"] == "graph_unprovable"
    assert status["local_sources_allowed"] is False


# ---------- main(): ACTIVE leader flow (Slice 5 — two CamillaDSP) ----------


def _patch_active_leader(monkeypatch, order):
    """Stub the active-leader config arm + the camilla#2 unit lifecycle into the
    order recorder. main() from-imports these at call time, so patching the
    active_leader_config MODULE attributes (and the reconcile module helpers)
    intercepts them."""
    import jasper.multiroom.active_leader_config as alc_mod

    monkeypatch.setattr(
        alc_mod,
        "precheck_active_leader_sync",
        lambda cfg_: order.append("precheck") or ("bake.yml", "crossover.yml"),
    )
    monkeypatch.setattr(
        alc_mod,
        "apply_active_leader_bake_sync",
        lambda: order.append("bake") or "bake.yml",
    )
    monkeypatch.setattr(
        alc_mod,
        "seed_crossover_statefile",
        lambda *a, **k: order.append("seed") or "crossover-statefile.yml",
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_arm_crossover_unit",
        lambda: order.append("arm_camilla2") or True,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_disable_crossover_unit",
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
        lambda: (
            order.append("probe")
            or reconcile_mod._PcmHandleProbeResult(
                "released",
                "writer_lock_free",
                attempts=1,
                timeout_sec=0.8,
            )
        ),
    )
    return alc_mod


# --- the ACTIVE-ring writer-lock release barrier (audio-graph consolidation
# --- #2285, P9-C). These exercise the REAL primitive against REAL files: the
# --- probe is a non-blocking flock, so a second open file description in this
# --- same process is a faithful stand-in for a live writer (flock(2): "If a
# --- process uses open(2) to obtain more than one file descriptor for the same
# --- file, these file descriptors are treated independently by flock()").


#: The real bounded wait, captured before any test monkeypatches the module
#: attribute, so the end-to-end caller test can restore it.
_REAL_WAIT_FOR_RELEASE = reconcile_mod._wait_for_active_content_pcm_release


def _lock_file(tmp_path):
    """A stand-in for `<ring>.writer.lock` next to a ring file."""
    lock = tmp_path / "active-content.ring.writer.lock"
    lock.write_bytes(b"")
    return lock


def test_active_content_release_probe_reports_released_when_lock_is_free(tmp_path):
    """No writer holds the ACTIVE ring's writer lock -> `released`, and
    camilla#2 may arm."""
    result = reconcile_mod._wait_for_active_content_pcm_release(
        lock_path=str(_lock_file(tmp_path)),
        timeout_sec=0,
    )

    assert result.released
    assert result.reason == "writer_lock_free"
    assert result.attempts == 1
    assert result.lock_path == str(tmp_path / "active-content.ring.writer.lock")


def test_active_content_release_probe_is_a_barrier_not_a_handoff(tmp_path):
    """The probe DROPS the lock before returning. If it held the lock across
    the arm it would be the very thing camilla#2 then contends with, so a
    second probe immediately after a `released` one must also read `released`
    (and a real flock must be takeable by an independent holder)."""
    lock = _lock_file(tmp_path)

    first = reconcile_mod._wait_for_active_content_pcm_release(
        lock_path=str(lock), timeout_sec=0
    )
    second = reconcile_mod._wait_for_active_content_pcm_release(
        lock_path=str(lock), timeout_sec=0
    )

    assert first.released and second.released
    # And the lock is genuinely claimable afterwards by a would-be camilla#2.
    fd = os.open(str(lock), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_active_content_release_probe_reports_busy_while_a_writer_holds(tmp_path):
    """A live writer holding flock(LOCK_EX) -> `busy`, and the bounded wait
    times out busy so the active-leader arm fails closed."""
    lock = _lock_file(tmp_path)
    holder = os.open(str(lock), os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        once = reconcile_mod._probe_active_content_pcm_once(lock_path=str(lock))
        waited = reconcile_mod._wait_for_active_content_pcm_release(
            lock_path=str(lock), timeout_sec=0
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert once.busy and once.reason == "writer_lock_held"
    assert waited.busy
    assert waited.reason == "timeout"
    # The timeout detail names the last busy reason, so an operator reading only
    # the blocked log line still learns WHY it stayed busy.
    assert "writer_lock_held" in waited.detail


def test_active_content_release_probe_unknown_when_lock_file_absent(tmp_path):
    """An absent writer lock means no writer has ever attached this ring, so
    the probe cannot prove release: `unknown`, which fails closed."""
    result = reconcile_mod._wait_for_active_content_pcm_release(
        lock_path=str(tmp_path / "never-attached.ring.writer.lock"),
        timeout_sec=0,
    )

    assert result.unknown
    assert result.reason == "writer_lock_absent"


def test_active_content_release_probe_never_creates_the_lock_file(tmp_path):
    """RULED: the probe must NOT pass O_CREAT. The ioplug creates this file
    with O_CREAT and fchmod-heals its mode precisely because a wrong-mode
    creation by an out-of-unit first creator locks the renderer out
    permanently (the sticky directory bit stops it deleting the file). A
    prober that created it would BE that hazard."""
    absent = tmp_path / "never-attached.ring.writer.lock"

    result = reconcile_mod._probe_active_content_pcm_once(lock_path=str(absent))

    assert result.unknown
    assert not absent.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses the mode bits this asserts"
)
def test_active_content_release_probe_unknown_when_lock_is_unopenable(tmp_path):
    """EACCES is `unknown`, never `released`: 'we could not ask' must not be
    reported as 'nobody is holding it'."""
    lock = _lock_file(tmp_path)
    lock.chmod(0o000)
    try:
        result = reconcile_mod._wait_for_active_content_pcm_release(
            lock_path=str(lock), timeout_sec=0
        )
    finally:
        lock.chmod(0o600)

    assert result.unknown
    assert result.reason == "writer_lock_unopenable"


def test_active_content_writer_lock_path_is_derived_not_spelled():
    """The reconciler contends on exactly the path the C ioplug's writer holds
    — the ACTIVE ring file plus the shared suffix — with no second literal."""
    assert reconcile_mod.ACTIVE_CONTENT_WRITER_LOCK_PATH == ring_writer_lock_path(
        RING_ACTIVE_CONTENT_FILE
    )
    assert reconcile_mod.ACTIVE_CONTENT_WRITER_LOCK_PATH.endswith(
        RING_WRITER_LOCK_SUFFIX
    )


def test_main_active_leader_bakes_arms_camilla2_and_reseeds(tmp_path, monkeypatch):
    """An active leader: the readiness GATE runs BEFORE the units (fail-safe);
    after the units, camilla#1 bakes the wire, the crossover statefile is
    RE-SEEDED, then camilla#2 is armed (enable --now). snapclient writes the
    grouping ring (its own receiver), the leader hosts the stream, and the
    endpoint status persists active_leader=true."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
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
    assert order.index("seed") < order.index(
        "audio_hardware:grouping-active-leader-bake"
    )
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
    # snapclient targets the grouping ring (the leader is its own receiver),
    # not the dumb member's return ring; the leader still runs snapserver.
    body = target.read_text()
    assert GROUPING_RING_PCM in body
    assert DAC_CONTENT_RING_PCM not in body
    assert f"{SERVER_KEY}=" in body and SNAPFIFO in body
    # endpoint status persisted as an active LEADER.
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"active_leader": true' in status


def test_main_active_leader_already_armed_skips_release_probe(
    tmp_path,
    monkeypatch,
):
    """Idempotency: once camilla#2 is active it legitimately holds the ACTIVE
    ring's writer lock. A steady-state reconcile must not probe that lock as if
    it were camilla#1 that had not let go — the probe would read `busy` and
    tear down a healthy bond."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_unit_is_active",
        lambda unit: (
            unit
            in {
                reconcile_mod.SNAPSERVER_UNIT,
                reconcile_mod.CROSSOVER_UNIT,
            }
        ),
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "probe" not in order
    assert "arm_camilla2" not in order
    assert "stream_binding" in order
    assert GROUPING_RING_PCM in target.read_text()


def test_main_active_leader_precheck_failure_falls_back_to_solo(tmp_path, monkeypatch):
    """If the readiness gate fails (camilla#1 bake OR camilla#2 driver-domain graph
    can't be made safe), the box does NOT bond — it fails safe to SOLO: no bake,
    no camilla#2 arm, snapclient cleared, and the block reason is surfaced for
    /state (invariant 5 fail-closed + self-recovery)."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)

    def _boom(cfg_):
        raise alc_mod.ActiveLeaderError("crossover_graph_unprovable", "nope")

    monkeypatch.setattr(alc_mod, "precheck_active_leader_sync", _boom)
    # On a refused FIRST bond camilla#2 was never armed, so the fail-safe restore
    # takes the (untouched) solo-active follower path — stub it for hermeticity.
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
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


def test_main_active_leader_unbond_disables_camilla2_and_restores(
    tmp_path, monkeypatch
):
    """Unbond of an active leader (camilla#2 enabled is the discriminator): tear
    camilla#2 down + restore camilla#1 via the leader stash. The untouched
    active-follower restore path is NOT taken."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    # camilla#2 enabled => this box WAS an active leader.
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda query, unit: (
            query == "is-enabled" and unit == reconcile_mod.CROSSOVER_UNIT
        ),
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "active_speaker_baseline.yml",
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or None,
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "disable_camilla2" in order  # camilla#2 torn down
    assert "leader_restore" in order  # camilla#1 restored via the leader path
    assert "follower_restore" not in order  # the follower path is not taken
    # solo: snapcast cleared (no server, no client).
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"


def test_main_disabled_but_active_crossover_uses_leader_teardown(
    tmp_path,
    monkeypatch,
):
    """Runtime ownership wins when a partial prior disable cleared intent."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    states = iter((False, True, False))  # disabled, active, inactive after stop
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: next(states),
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "leader.yml",
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or "follower.yml",
    )

    assert main(["--reason", "test"]) == 0

    assert "disable_camilla2" in order
    assert "leader_restore" in order
    assert "follower_restore" not in order
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
    tmp_path,
    monkeypatch,
):
    """Solo-impact regression: a solo/active box whose camilla#2 is NOT enabled
    (never an active leader) takes the untouched active-follower restore path and
    never disables camilla#2 — the unit-enabled discriminator keeps the follower
    path byte-identical."""
    import jasper.multiroom.follower_config as fc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: False,
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or None,
    )

    rc = main(["--reason", "test"])

    assert rc == 0
    assert "follower_restore" in order  # the untouched follower path
    assert "disable_camilla2" not in order  # camilla#2 never touched
    assert "leader_restore" not in order


def test_main_unknown_topology_preserves_graph_before_any_mutation(
    tmp_path,
    monkeypatch,
    caplog,
):
    """A corrupt active/passive classifier can never authorize restoration."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod
    import jasper.multiroom.leader_config as leader_mod

    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(
        reconcile_mod,
        "_output_topology_state",
        lambda: (None, False),
    )
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "leader.yml",
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or "follower.yml",
    )
    monkeypatch.setattr(
        leader_mod,
        "restore_solo_config_sync",
        lambda: order.append("passive_restore") or "flat.yml",
    )

    with caplog.at_level("ERROR", logger=reconcile_mod.logger.name):
        rc = main(["--reason", "test"])

    assert rc == 1
    assert "outputd_restart" in order
    assert "apply" not in order
    assert "leader_restore" not in order
    assert "follower_restore" not in order
    assert "passive_restore" not in order
    outputd_env = (tmp_path / "grouping-outputd.env").read_text()
    assert f"{reconcile_mod.OUTPUTD_DAC_CONTENT_FIFO_ENV}=\n" in outputd_env
    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.active_restore_blocked" in record.message
    ]
    assert len(events) == 1
    assert "reason=active_speaker_topology_unknown" in events[0]
    assert "action=preserve_runtime_graph" in events[0]
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "active_speaker_topology_unknown"' in status


def test_main_solo_active_box_blocks_restore_when_crossover_state_unknown(
    tmp_path,
    monkeypatch,
    caplog,
):
    """Unknown camilla#2 ownership must never guess a solo restore path."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: None,
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "leader.yml",
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or "follower.yml",
    )

    with caplog.at_level("ERROR", logger=reconcile_mod.logger.name):
        rc = main(["--reason", "test"])

    assert rc == 1
    assert "disable_camilla2" not in order
    assert "leader_restore" not in order
    assert "follower_restore" not in order
    assert "write" not in order
    assert "outputd_restart" not in order
    assert "apply" not in order
    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.active_restore_blocked" in record.message
    ]
    assert len(events) == 1
    assert "reason=crossover_ownership_state_unknown" in events[0]
    assert "action=preserve_runtime_graph" in events[0]
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "crossover_ownership_state_unknown"' in status


def test_main_crossover_teardown_failure_preserves_runtime_graph(
    tmp_path,
    monkeypatch,
):
    """Camilla#1 cannot reclaim the DAC when camilla#2 failed to stop."""
    import jasper.multiroom.active_leader_config as alc_mod
    import jasper.multiroom.follower_config as fc_mod

    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    states = iter((True, True))
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: next(states),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_disable_crossover_unit",
        lambda: order.append("disable_failed") or False,
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "leader.yml",
    )
    monkeypatch.setattr(
        fc_mod,
        "restore_active_follower_solo_sync",
        lambda: order.append("follower_restore") or "follower.yml",
    )

    assert main(["--reason", "test"]) == 1

    assert "disable_failed" in order
    assert "leader_restore" not in order
    assert "follower_restore" not in order
    assert "write" not in order
    assert "apply" not in order
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "crossover_teardown_failed"' in status


def test_main_crossover_must_report_inactive_after_teardown(
    tmp_path,
    monkeypatch,
):
    """A successful disable command is insufficient without inactive proof."""
    import jasper.multiroom.active_leader_config as alc_mod

    _target, order = _patch_main_io(monkeypatch, tmp_path, _disabled())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    states = iter((True, True, True))  # still active after successful disable
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: next(states),
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "leader.yml",
    )

    assert main(["--reason", "test"]) == 1

    assert "disable_camilla2" in order
    assert "leader_restore" not in order
    assert "write" not in order
    assert "apply" not in order
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "crossover_inactive_state_unproven"' in status


def test_main_active_leader_skips_arm_when_bake_fails(tmp_path, monkeypatch):
    """JTS5 incident regression (2026-06-23): if the camilla#1 bake FAILS, it
    stays on its solo-active DAC baseline — so camilla#2 must NOT be armed. Arming
    it would make both CamillaDSP fight for the DAC and (camilla#1 carries
    StartLimitAction=reboot) reboot-loop the box. The arm is gated on bake success."""
    import jasper.multiroom.active_leader_config as alc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
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
    tmp_path,
    monkeypatch,
):
    """After the bake and inert camilla#2 statefile seed, outputd must
    re-converge to the active-content lane before camilla#2 can safely own the
    grouping ring. If that handoff fails, leave camilla#2 unarmed."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
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
    tmp_path,
    monkeypatch,
):
    """If a prior failed active-leader attempt left camilla#2 holding the active
    lane, reconcile first releases camilla#2 and reset-starts camilla#1. If
    camilla#1 still cannot come back, do not bake or re-arm camilla#2."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
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
    tmp_path,
    monkeypatch,
):
    """jts3 EBUSY regression (2026-06-24): a successful camilla#1 bake is not
    proof that camilla#1 has let go of the ACTIVE ring. If the positive handle
    probe times out busy — a writer still holds the ring's writer lock —
    camilla#2 is NOT armed; camilla#1 is restored to solo-active so the box
    keeps playing locally and a later reconcile can retry."""
    import jasper.multiroom.active_leader_config as alc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        lambda: (
            order.append("probe_busy")
            or reconcile_mod._PcmHandleProbeResult(
                "busy",
                "timeout",
                detail="writer_lock_held: [Errno 35] Resource temporarily unavailable",
                attempts=17,
                timeout_sec=0.8,
            )
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
    assert GROUPING_RING_PCM in body
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "active_content_pcm_busy"' in status
    assert '"active_leader": false' in status


def test_main_active_leader_fails_closed_when_writer_lock_absent(
    tmp_path,
    monkeypatch,
):
    """Hardening (P1 review #3): `unknown` is NOT positive proof of release, so
    the reconciler fails CLOSED — restores solo-active and leaves camilla#2
    un-armed, exactly like the busy path — rather than arming into a possible
    DAC fight. The concrete `unknown` here is an absent writer lock (a box whose
    ACTIVE lane is not a ring, so nothing can prove release); the rule is the
    same for every `unknown` reason: 'can't prove it' never arms."""
    import jasper.multiroom.active_leader_config as alc_mod

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        lambda: (
            order.append("probe_missing")
            or reconcile_mod._PcmHandleProbeResult(
                "unknown",
                "writer_lock_absent",
                detail="[Errno 2] No such file or directory",
                attempts=1,
                timeout_sec=0.8,
            )
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
    assert GROUPING_RING_PCM in target.read_text()


def test_main_active_leader_fails_closed_through_the_real_writer_lock_probe(
    tmp_path,
    monkeypatch,
):
    """End-to-end with the REAL probe, no probe mock: the caller must actually
    consult ACTIVE_CONTENT_WRITER_LOCK_PATH. A busy lock (a live writer still
    owns the ACTIVE ring) blocks the arm and restores solo-active.

    This is the wiring pin the mocked branch tests cannot be: they would still
    pass if the caller probed a path nothing writes."""
    import jasper.multiroom.active_leader_config as alc_mod

    lock = tmp_path / "active-content.ring.writer.lock"
    lock.write_bytes(b"")
    monkeypatch.setattr(
        reconcile_mod, "ACTIVE_CONTENT_WRITER_LOCK_PATH", str(lock)
    )
    monkeypatch.setattr(reconcile_mod, "ACTIVE_CONTENT_RELEASE_TIMEOUT_SEC", 0.0)

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _patch_active_leader(monkeypatch, order)
    # Undo the helper's probe mock so the REAL flock probe runs.
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        _REAL_WAIT_FOR_RELEASE,
    )
    monkeypatch.setattr(
        alc_mod,
        "restore_active_leader_solo_sync",
        lambda: order.append("leader_restore") or "active_speaker_baseline.yml",
    )

    holder = os.open(str(lock), os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = main(["--reason", "test"])
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert rc == 1
    assert "arm_camilla2" not in order
    assert "leader_restore" in order
    status = (tmp_path / "grouping-follower-status.json").read_text()
    assert '"blocked_reason": "active_content_pcm_busy"' in status
    assert target.read_text()


def test_main_active_leader_skips_bake_and_arm_when_snapserver_down(
    tmp_path,
    monkeypatch,
):
    """JTS5 incident regression: snapserver not active (no Snapcast installed, or
    it failed to start) means camilla#1's File-sink bake has no FIFO reader — so
    neither the bake NOR the camilla#2 arm runs. camilla#1 keeps the DAC on its
    solo baseline; no two-instance conflict, no reboot."""
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
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
        provision_mod,
        "ensure_snapcast_installed",
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
        provision_mod,
        "ensure_snapcast_installed",
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
        provision_mod,
        "ensure_snapcast_installed",
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
    assert (
        reconcile_mod._restart_unit(
            reconcile_mod.AEC_RECONCILE_UNIT,
            no_block=True,
        )
        is True
    )
    assert calls[0] == [
        "systemctl",
        "reset-failed",
        reconcile_mod.AEC_RECONCILE_UNIT,
    ]
    assert calls[1] == [
        "systemctl",
        "--no-block",
        "restart",
        reconcile_mod.AEC_RECONCILE_UNIT,
    ]


def test_restart_unit_active_only_never_resurrects_airplay_off(monkeypatch):
    """A grouping offset change refreshes AirPlay only when it is active."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert (
        reconcile_mod._restart_unit(
            reconcile_mod.SHAIRPORT_UNIT,
            active_only=True,
        )
        is True
    )
    assert calls[1] == [
        "systemctl",
        "try-restart",
        reconcile_mod.SHAIRPORT_UNIT,
    ]


def test_converge_sources_runs_fresh_pass_when_inactive(monkeypatch):
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:3] == [
            "systemctl",
            "show",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ]:
            return sp.CompletedProcess(argv, 0, stdout="inactive\n", stderr="")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._converge_sources_after_role(grouping_active=True, units_changed=False) is True
    assert calls == [
        [
            "systemctl",
            "reset-failed",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ],
        [
            "systemctl",
            "show",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
            "--property=ActiveState",
            "--value",
        ],
        [
            "systemctl",
            "start",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ],
    ]


def test_converge_sources_drains_old_activation_before_fresh_pass(monkeypatch):
    """An activating oneshot may have read the old role before grouping applied.

    A blocking start joins that job without interrupting it; only after it
    returns may the second blocking start run the required fresh pass.
    """
    import subprocess as sp

    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kw):
        calls.append((list(argv), dict(kw)))
        if argv[:3] == [
            "systemctl",
            "show",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ]:
            return sp.CompletedProcess(argv, 0, stdout="activating\n", stderr="")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._converge_sources_after_role(grouping_active=True, units_changed=False) is True

    argv = [call[0] for call in calls]
    assert argv[-2:] == [
        [
            "systemctl",
            "start",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ],
        [
            "systemctl",
            "start",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ],
    ]
    barrier_kwargs = calls[-2][1]
    assert barrier_kwargs["timeout"] == (
        reconcile_mod.SOURCE_RECONCILE_SYSTEMD_TIMEOUT_SECONDS + 5.0
    )
    assert barrier_kwargs["check"] is True
    assert calls[-1][1]["timeout"] == (
        reconcile_mod._SOURCE_RECONCILE_START_TIMEOUT_SEC
    )


def test_converge_sources_unknown_state_uses_safe_barrier(monkeypatch):
    """A failed state probe cannot be permission to risk coalescing old work."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[1:2] == ["show"]:
            assert kw["timeout"] == reconcile_mod._SYSTEMCTL_CONTROL_TIMEOUT_SEC
            raise sp.TimeoutExpired(argv, kw["timeout"])
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._converge_sources_after_role(grouping_active=True, units_changed=False) is True
    assert calls[-2:] == [
        ["systemctl", "start", reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT],
        [
            "systemctl",
            "start",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ],
    ]


def test_converge_sources_barrier_timeout_fails_without_fresh_pass(
    monkeypatch,
):
    """A wedged old source pass is not interrupted or falsely acknowledged."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[1:2] == ["show"]:
            return sp.CompletedProcess(argv, 0, stdout="activating\n", stderr="")
        if argv[1:2] == ["start"]:
            raise sp.TimeoutExpired(argv, kw["timeout"])
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert (
        reconcile_mod._converge_sources_after_role(
            grouping_active=True,
            units_changed=False,
        )
        is False
    )
    assert (
        calls.count(
            [
                "systemctl",
                "start",
                reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
            ]
        )
        == 1
    )
    assert not any("restart" in call for call in calls)


@pytest.mark.parametrize(
    ("grouping_active", "units_changed", "expect_start"),
    [
        pytest.param(False, False, False, id="solo_no_role_change_skips"),
        pytest.param(False, True, True, id="solo_unit_changed_runs_barrier"),
        pytest.param(True, False, True, id="grouping_active_always_runs_barrier"),
    ],
)
def test_converge_sources_barrier_gated_by_role_change(
    monkeypatch,
    grouping_active,
    units_changed,
    expect_start,
):
    """The post-role source barrier forces a ~30s audio-hardware-reconcile pass
    (Wants=/After=), so it must run only when something for source-intent to
    react to actually happened: grouping is active, or the unit plan moved a
    unit. A quiescent solo pass must not pay that cost every boot."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:3] == [
            "systemctl",
            "show",
            reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT,
        ]:
            return sp.CompletedProcess(argv, 0, stdout="inactive\n", stderr="")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    result = reconcile_mod._converge_sources_after_role(
        grouping_active=grouping_active,
        units_changed=units_changed,
    )
    assert result is True
    start_calls = [
        c
        for c in calls
        if c == ["systemctl", "start", reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT]
    ]
    assert bool(start_calls) is expect_start
    if not expect_start:
        assert calls == []


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


def test_restart_unit_reset_timeout_is_fail_soft(monkeypatch):
    """A wedged best-effort reset cannot consume the whole reconcile pass."""
    import subprocess as sp

    calls: list[tuple[list[str], float]] = []

    def fake_run(argv, **kw):
        calls.append((list(argv), kw["timeout"]))
        if argv[1] == "reset-failed":
            raise sp.TimeoutExpired(argv, kw["timeout"])
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert reconcile_mod._restart_unit("jasper-camilla.service") is True
    assert calls == [
        (
            ["systemctl", "reset-failed", "jasper-camilla.service"],
            reconcile_mod._SYSTEMCTL_CONTROL_TIMEOUT_SEC,
        ),
        (
            ["systemctl", "restart", "jasper-camilla.service"],
            reconcile_mod._SYSTEMCTL_BLOCKING_TIMEOUT_SEC,
        ),
    ]


@pytest.mark.parametrize(
    ("no_block", "expected_timeout"),
    [
        (False, reconcile_mod._SYSTEMCTL_BLOCKING_TIMEOUT_SEC),
        (True, reconcile_mod._SYSTEMCTL_CONTROL_TIMEOUT_SEC),
    ],
)
def test_restart_unit_timeout_matches_blocking_mode(
    monkeypatch,
    no_block,
    expected_timeout,
):
    """Detached manager requests stay short; blocking jobs get the full bound."""
    import subprocess as sp

    def fake_run(argv, **kw):
        if argv[1] == "reset-failed":
            return sp.CompletedProcess(argv, 0, stdout="", stderr="")
        assert kw["timeout"] == expected_timeout
        raise sp.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)
    assert (
        reconcile_mod._restart_unit(
            "jasper-outputd.service",
            no_block=no_block,
        )
        is False
    )


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


def test_restart_unit_contains_spawn_oserror(monkeypatch, caplog):
    """A restart spawn failure is visible and cannot abort the reconciler."""
    import subprocess as sp

    def fake_run(argv, **_kw):
        if argv[1] == "reset-failed":
            return sp.CompletedProcess(argv, 0, stdout="", stderr="")
        raise OSError("cannot allocate process")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("ERROR", logger=reconcile_mod.logger.name):
        assert reconcile_mod._restart_unit("jasper-outputd.service") is False

    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.unit_restart_failed" in record.message
    ]
    assert len(events) == 1
    assert "unit=jasper-outputd.service" in events[0]
    assert 'error="cannot allocate process"' in events[0]


def test_crossover_teardown_contains_spawn_oserror(monkeypatch, caplog):
    """A systemctl spawn failure becomes a blocked teardown, not an escape."""

    def fake_run(_argv, **kw):
        assert kw["timeout"] == reconcile_mod._SYSTEMCTL_BLOCKING_TIMEOUT_SEC
        raise OSError("cannot spawn systemctl")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("ERROR", logger=reconcile_mod.logger.name):
        assert reconcile_mod._disable_crossover_unit() is False

    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.crossover_unit_failed" in record.message
    ]
    assert len(events) == 1
    assert "action=disabled" in events[0]
    assert 'error="cannot spawn systemctl"' in events[0]


def test_unit_state_queries_share_exact_systemctl_contract(monkeypatch):
    import subprocess as sp

    calls: list[list[str]] = []
    results = iter(((0, "enabled\n"), (3, "inactive\n")))

    def fake_run(argv, **kw):
        calls.append(list(argv))
        assert kw == {
            "capture_output": True,
            "text": True,
            "timeout": reconcile_mod._SYSTEMCTL_CONTROL_TIMEOUT_SEC,
        }
        returncode, stdout = next(results)
        return sp.CompletedProcess(
            argv,
            returncode,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    assert (
        reconcile_mod._systemctl_unit_state(
            "is-enabled",
            "source.service",
        )
        is True
    )
    assert reconcile_mod._unit_is_active("audio.service") is False
    assert calls == [
        ["systemctl", "is-enabled", "source.service"],
        ["systemctl", "is-active", "audio.service"],
    ]


def test_active_speaker_topology_error_is_raw_unknown_but_legacy_false(
    monkeypatch,
    caplog,
):
    """Safety callers retain corrupt-topology uncertainty; old readers do not."""
    import jasper.output_topology as topology_mod

    def fail_load():
        raise topology_mod.OutputTopologyError("corrupt topology")

    monkeypatch.setattr(topology_mod, "load_output_topology_strict", fail_load)

    with caplog.at_level("WARNING", logger=reconcile_mod.logger.name):
        assert reconcile_mod._output_topology_state()[0] is None
        assert reconcile_mod.is_active_speaker_box() is False

    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.active_speaker_probe_failed" in record.message
    ]
    assert len(events) == 2
    assert all("corrupt topology" in event for event in events)


def test_unit_state_vocabulary_has_explicit_jts_intent_semantics(monkeypatch):
    """Every documented systemctl state is deliberately true/false/unknown."""
    import subprocess as sp

    current = [""]

    def fake_run(argv, **_kw):
        state = current[0]
        return sp.CompletedProcess(
            argv,
            0 if state in {"enabled", "enabled-runtime", "active"} else 1,
            stdout=f"{state}\n",
            stderr="",
        )

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    enabled_states = {
        "enabled": True,
        "enabled-runtime": True,
        "alias": False,
        "static": False,
        "indirect": False,
        "disabled": False,
        "generated": False,
        "transient": False,
        "linked": False,
        "linked-runtime": False,
        "masked": False,
        "masked-runtime": False,
        "not-found": False,
        "bad": None,
    }
    active_states = {
        "active": True,
        "inactive": False,
        "failed": False,
        "activating": None,
        "deactivating": None,
        "reloading": None,
        "refreshing": None,
        "maintenance": None,
        "unknown": None,
    }
    for state, expected in enabled_states.items():
        current[0] = state
        assert (
            reconcile_mod._systemctl_unit_state(
                "is-enabled",
                "test.service",
            )
            is expected
        ), state
    for state, expected in active_states.items():
        current[0] = state
        assert (
            reconcile_mod._systemctl_unit_state(
                "is-active",
                "test.service",
            )
            is expected
        ), state


def test_unit_state_completed_manager_error_is_unknown(monkeypatch, caplog):
    """A completed systemctl error cannot be interpreted as disabled."""
    import subprocess as sp

    monkeypatch.setattr(
        reconcile_mod.subprocess,
        "run",
        lambda argv, **_kw: sp.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="Failed to connect to bus: No medium found\n",
        ),
    )

    with caplog.at_level("WARNING", logger=reconcile_mod.logger.name):
        state = reconcile_mod._systemctl_unit_state(
            "is-enabled",
            reconcile_mod.CROSSOVER_UNIT,
        )

    assert state is None
    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.unit_state_probe_failed" in record.message
    ]
    assert len(events) == 1
    assert "rc=1" in events[0]
    assert "state=(none)" in events[0]
    assert "Failed to connect to bus" in events[0]


def test_unit_state_query_oserror_is_safe_false_and_observable(
    monkeypatch,
    caplog,
):
    """A spawn failure must not escape or masquerade as silent inactivity."""

    def fake_run(_argv, **_kw):
        raise OSError("cannot allocate process")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("WARNING", logger=reconcile_mod.logger.name):
        assert (
            reconcile_mod._systemctl_unit_state(
                "is-enabled",
                "raw.service",
            )
            is None
        )
        assert reconcile_mod._unit_is_active("audio.service") is False

    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.unit_state_probe_failed" in record.message
    ]
    assert len(events) == 2
    assert "unit=raw.service" in events[0]
    assert "query=is-enabled" in events[0]
    assert "unit=audio.service" in events[1]
    assert "query=is-active" in events[1]


def test_unit_state_query_timeout_is_unknown_and_observable(monkeypatch, caplog):
    """A stuck manager probe is finite and cannot masquerade as inactive."""
    import subprocess as sp

    def fake_run(argv, **kw):
        assert kw["timeout"] == reconcile_mod._SYSTEMCTL_CONTROL_TIMEOUT_SEC
        raise sp.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("WARNING", logger=reconcile_mod.logger.name):
        assert (
            reconcile_mod._systemctl_unit_state(
                "is-active",
                "audio.service",
            )
            is None
        )

    assert any(
        "event=multiroom.reconcile.unit_state_probe_failed" in record.message
        and "unit=audio.service" in record.message
        for record in caplog.records
    )


def test_unit_state_query_missing_systemctl_is_silent_false(
    monkeypatch,
    caplog,
):
    """The historical no-systemctl install-tier case remains a quiet miss."""

    def fake_run(_argv, **_kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("WARNING", logger=reconcile_mod.logger.name):
        assert (
            reconcile_mod._systemctl_unit_state(
                "is-enabled",
                "raw.service",
            )
            is None
        )
        assert reconcile_mod._unit_is_active("audio.service") is False

    assert not any(
        "event=multiroom.reconcile.unit_state_probe_failed" in record.message
        for record in caplog.records
    )


def test_ensure_unit_active_continues_after_probe_and_reset_oserrors(
    monkeypatch,
    caplog,
):
    """Probe/reset are fail-soft; the idempotent start is load-bearing."""
    import subprocess as sp

    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(list(argv))
        if argv[1] in {"is-active", "reset-failed"}:
            raise OSError(f"spawn failed for {argv[1]}")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("WARNING", logger=reconcile_mod.logger.name):
        assert (
            reconcile_mod._ensure_unit_active(
                "jasper-camilla.service",
                reason="active-leader-bake",
            )
            is True
        )

    assert calls == [
        ["systemctl", "is-active", "jasper-camilla.service"],
        ["systemctl", "reset-failed", "jasper-camilla.service"],
        ["systemctl", "start", "jasper-camilla.service"],
    ]
    assert any(
        "event=multiroom.reconcile.unit_state_probe_failed" in record.message
        for record in caplog.records
    )
    assert any(
        "event=multiroom.reconcile.reset_failed_error" in record.message
        for record in caplog.records
    )


def test_ensure_unit_active_contains_start_oserror(monkeypatch, caplog):
    """A failed recovery start returns False with one actionable event."""
    import subprocess as sp

    def fake_run(argv, **_kw):
        if argv[1] == "is-active":
            return sp.CompletedProcess(argv, 3)
        if argv[1] == "start":
            raise OSError("cannot spawn systemctl")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("ERROR", logger=reconcile_mod.logger.name):
        assert (
            reconcile_mod._ensure_unit_active(
                "jasper-camilla.service",
                reason="active-leader-bake",
            )
            is False
        )

    events = [
        record.message
        for record in caplog.records
        if "event=multiroom.reconcile.unit_start_failed" in record.message
    ]
    assert len(events) == 1
    assert "unit=jasper-camilla.service" in events[0]
    assert "reason=active-leader-bake" in events[0]
    assert 'error="cannot spawn systemctl"' in events[0]


def test_ensure_unit_active_contains_bounded_start_timeout(monkeypatch, caplog):
    """Direct/install recovery cannot hang forever behind a systemd job."""
    import subprocess as sp

    def fake_run(argv, **kw):
        if argv[1] == "is-active":
            return sp.CompletedProcess(argv, 3, stdout="inactive\n", stderr="")
        if argv[1] == "start":
            assert kw["timeout"] == reconcile_mod._SYSTEMCTL_BLOCKING_TIMEOUT_SEC
            raise sp.TimeoutExpired(argv, kw["timeout"])
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake_run)

    with caplog.at_level("ERROR", logger=reconcile_mod.logger.name):
        assert (
            reconcile_mod._ensure_unit_active(
                "jasper-camilla.service",
                reason="active-leader-bake",
            )
            is False
        )

    assert any(
        "event=multiroom.reconcile.unit_start_failed" in record.message
        and "jasper-camilla.service" in record.message
        for record in caplog.records
    )


# --- the coupling no longer refuses a bond, and the gate that still can ------
#
# The coupling refusal's subject was outputd's dac_content lane, and the cutover
# armed that lane onto the dac-content RING, which strands no second content
# source — so that gate now fires for nobody and the tests below pin it.
# `_patch_main_io` stubs `_output_topology_state` to (False, True), a PASSIVE
# box with a saved flat-capable layout: the DUMB member that carries the lane,
# i.e. the shape that used to be refused.
#
# The refusal that CAN still fire on a dumb member is the fourth arming gate —
# an outputd period the return ring's slot cannot carry — and the fail-safe
# fallback machinery is otherwise exercised through the ACTIVE follower's
# readiness gate (`_refuse_follower_bond`).


def _arm_ring_for_reconcile(monkeypatch):
    """Make main()'s read_persisted_coupling report shm_ring (ring-armed box)."""
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )


def _refuse_follower_bond(monkeypatch):
    """Make main() take a LIVE fail-safe refusal on a follower.

    The coupling gate refuses nobody since the cutover, so the refusal that
    still reaches `fall_back_to_solo` on this shape is the ACTIVE follower's
    readiness gate. Same fallback machinery, a reason that can actually happen —
    call AFTER `_patch_main_io` (it overrides the passive topology stub)."""
    import jasper.multiroom.follower_config as fc_mod

    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))

    def _boom(_cfg):
        raise fc_mod.ActiveFollowerError("graph_unprovable", "nope")

    monkeypatch.setattr(fc_mod, "precheck_active_follower_sync", _boom)
    monkeypatch.setattr(fc_mod, "restore_active_follower_solo_sync", lambda: None)


@pytest.mark.parametrize(
    "base_env,outputd_env,expect_armed",
    [
        # CASE A — the drift the plan's policy resolver cannot see: the DAC
        # floor says 128, but outputd.env still carries the packaged 1024 that
        # its own "rerun audio hardware reconcile" warning is about. outputd
        # LOADS 1024 and would bail on the ring's 128-frame slot, so the gate
        # must refuse.
        pytest.param("", "JASPER_OUTPUTD_PERIOD_FRAMES=1024\n", False, id="case_a"),
        # CASE B — an operator pin in jasper.env that the reconciler has already
        # superseded in outputd.env. outputd loads the LATER layer, 128, and
        # plays; a gate reading policy precedence would refuse a healthy box.
        pytest.param(
            "JASPER_OUTPUTD_PERIOD_FRAMES=256\n",
            "JASPER_OUTPUTD_PERIOD_FRAMES=128\n",
            True,
            id="case_b",
        ),
        # A value outputd would refuse outright: it never starts, so nothing may
        # be armed on it.
        pytest.param("", "JASPER_OUTPUTD_PERIOD_FRAMES=nope\n", False, id="unparseable"),
        # Blank/absent everywhere is outputd's packaged default (1024).
        pytest.param("", "", False, id="packaged_default"),
    ],
)
def test_the_period_gate_reads_what_outputd_loads_not_what_policy_intends(
    tmp_path, monkeypatch, base_env, outputd_env, expect_armed,
):
    """THE GATE'S INPUT IS THE DAEMON'S, layer for layer.

    The plan resolves a POLICY period (lab override > jasper.env > DAC floor >
    packaged default, with outputd.env feeding only warnings). outputd resolves
    a LOADED one: `env_u32` over jasper.env, then outputd.env, then
    grouping-outputd.env, later wins. Where they disagree the slot gate must
    follow the daemon, or it arms a box that bails EX_CONFIG (Case A) or refuses
    one that plays (Case B).
    """
    from jasper.audio_runtime_plan import outputd_period_frames_as_loaded
    from jasper.multiroom.dac_content_ring import dac_content_ring_servable

    base = tmp_path / "jasper.env"
    base.write_text(base_env, encoding="utf-8")
    outputd = tmp_path / "outputd.env"
    outputd.write_text(outputd_env, encoding="utf-8")
    grouping = tmp_path / "grouping-outputd.env"
    grouping.write_text("", encoding="utf-8")
    monkeypatch.setattr("jasper.env_load.BASE_ENV_PATH", str(base))
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd)
    )
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping)
    )

    period = reconcile_mod.box_outputd_period_frames()
    assert period == outputd_period_frames_as_loaded()
    assert dac_content_ring_servable(period) is expect_armed
    assert (
        reconcile_mod.member_lane_decision(
            _follower(), flat_output_allowed=True, outputd_period_frames=period
        ).armed
        is expect_armed
    )


@pytest.mark.parametrize("armed", [True, False], ids=["armed", "unarmed"])
def test_the_merged_env_outputd_starts_with_never_pairs_marker_and_bridge(
    tmp_path, monkeypatch, armed,
):
    """THE SHAPE OUTPUTD ACTUALLY BOOTS ON, through both env layers.

    `jasper-fanin-coupling-auto` writes JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring
    into outputd.env on EVERY pass, and the unit loads grouping-outputd.env
    after it. outputd refuses the marker beside a DECLARED bridge (EX_CONFIG,
    then RestartPreventExitStatus=78 — a parked daemon on a box the reconcile
    just reported bonded), and its `env_optional` read counts blank as
    undeclared. So the armed layer must CLEAR that value and the unarmed layer
    must leave it alone: without the marker outputd reads the same key with
    `env_str`, whose blank it parks on.
    """
    from jasper.env_load import outputd_reconciled_env
    from jasper.fanin_coupling import OUTPUTD_CONTENT_BRIDGE_ENV_VAR

    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}=shm_ring\n", encoding="utf-8"
    )
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text(
        "".join(
            f"{k}={v}\n"
            for k, v in bonded_grouping_env(
                _follower(), flat_output_allowed=armed
            ).items()
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping_env)
    )

    merged = outputd_reconciled_env(str(outputd_env))

    assert dac_content_lane_marker_armed(merged) is armed
    if armed:
        assert merged[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] == ""
    else:
        assert merged[OUTPUTD_CONTENT_BRIDGE_ENV_VAR] == "shm_ring"


@pytest.mark.parametrize(
    "profile_id",
    [p.id for p in _dac.all_profiles()],
    ids=[p.id for p in _dac.all_profiles()],
)
def test_every_dac_profile_arms_the_return_ring_exactly_when_its_period_fits(
    profile_id,
):
    """THE DAC-PROFILE x MARKER MATRIX (#3656).

    One slot of the dac-content return ring is one outputd PERIOD, and outputd
    refuses the mismatched pair at startup with EX_CONFIG under
    `RestartPreventExitStatus=78` — a parked daemon and a SILENT speaker. So for
    every DAC this fleet can run, the writer must arm the marker exactly when
    that box's resolved outputd period equals the ring's slot, and the member's
    snapclient must name the return-ring PCM on exactly the same cells.

    The period comes from the PRODUCTION resolver — the same layered
    env/DacProfile floor resolution the audio-hardware reconciler writes into
    `outputd.env` — so this matrix pins the real answer per profile rather than
    a restated table: every profile with a declared latency floor runs 128 and
    arms; the floorless HiFiBerry DAC8x Studio runs outputd's packaged 1024 and
    does not.
    """

    from jasper.audio_runtime_plan import resolve_outputd_period_setting
    from jasper.multiroom.reconcile import _assemble_args, outputd_grouping_env

    period = int(
        resolve_outputd_period_setting(
            base_env={},
            override_env={},
            generated_env={},
            base_label="base.env",
            override_label="overrides.json",
            generated_label="outputd.env",
            profile_id=profile_id,
        ).value
    )
    fits = period == DAC_CONTENT_RING_PERIOD_FRAMES

    cfg = _follower()
    dumb = outputd_grouping_env(
        cfg,
        active_endpoint=False,
        flat_output_allowed=True,
        outputd_period_frames=period,
    )
    assert dac_content_lane_marker_armed(dumb) is fits
    # The FIFO key is cleared on BOTH cells: arming two round-trip transports at
    # once is outputd's most fundamental refusal.
    assert dumb[reconcile_mod.OUTPUTD_DAC_CONTENT_FIFO_ENV] == ""

    # ...and the snapclient this box actually gets. A refused member is the
    # DISABLED cfg `fall_back_to_solo` installs, which is why the argv flips
    # with the marker rather than independently of it.
    bonded = cfg if fits else dataclasses.replace(cfg, enabled=False)
    client = _assemble_args(bonded)[CLIENT_KEY]
    assert (DAC_CONTENT_RING_PCM in client) is fits


def test_ring_armed_active_endpoint_may_bond(tmp_path, monkeypatch, caplog):
    """B1's subject, from the other direction: an ACTIVE-speaker box whose
    dac_content lane the writer clears is NOT refused by the ring gate.

    This is the cell the whole hazard PR exists to admit — a bonded, ring-armed
    active leader — and before the narrowing it was unreachable, which is why
    the coupling-blind program bake it exposes had to land in the same PR.

    Asserted as "the ring gate did not fire", not as "the bond succeeded": the
    reconcile runs on past it into the active-leader precheck, whose own gates
    (snapcast present, graphs re-proved) are a different subject and are not
    hermetic here. Under the pre-narrowing rule the gate fired FIRST and
    `fall_back_to_solo` skipped that precheck entirely, so a blocked_reason from
    any later gate is itself proof the ring gate let the box through.
    """
    _patch_main_io(monkeypatch, tmp_path, _leader())
    # ACTIVE box: roleful topology, so no flat DAC graph is permitted and the
    # dac_content lane is cleared by outputd_grouping_env.
    monkeypatch.setattr(reconcile_mod, "_output_topology_state", lambda: (True, False))
    _arm_ring_for_reconcile(monkeypatch)

    import logging

    with caplog.at_level(logging.WARNING):
        main([])

    assert not any(
        "event=multiroom.reconcile.ring_armed_bond_blocked" in record.message
        for record in caplog.records
    ), "the ring gate fired on a box whose dac_content lane is cleared"

    import json

    status = json.loads((tmp_path / "grouping-follower-status.json").read_text())
    assert status.get("blocked_reason") not in (
        "ring_armed_box_cannot_bond",  # the pre-narrowing token
        "fanin_shm_ring_unsupported_with_dac_content_lane",
    ), status


@pytest.mark.parametrize("coupling", ["shm_ring", "loopback"])
def test_a_dumb_member_bonds_under_either_coupling(tmp_path, monkeypatch, coupling):
    """THE CUTOVER: the fan-in coupling no longer gates a DUMB member's bond.

    A ring-armed box used to be REFUSED here, because its round-trip lane was a
    raw-PCM FIFO that needed outputd on the snd-aloop content PCM an armed ring
    moves CamillaDSP off — two content sources, one DAC. Armed onto the
    dac-content RETURN ring instead, outputd resolves its central `shm_ring` to
    None, so the ring strands nothing and the coupling matrix's one blocked cell
    no longer matches. `loopback` is the shape that always bonded; both now
    reach the same place."""
    import json

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: coupling,
    )

    assert main([]) == 0
    assert "camilla_bonded" in order
    # Bonded args, not the disabled (solo) shape — and onto the return ring.
    assert f"--soundcard {DAC_CONTENT_RING_PCM} --player alsa" in target.read_text()
    status = json.loads((tmp_path / "grouping-follower-status.json").read_text())
    assert not status.get("blocked_reason")


@pytest.mark.parametrize("period", [1024, None])
def test_a_period_that_cannot_carry_the_return_ring_stays_solo(
    tmp_path, monkeypatch, period,
):
    """THE FOURTH ARMING GATE, end to end.

    One slot of the dac-content return ring is one outputd period, and outputd
    bails EX_CONFIG on the mismatched pair under `RestartPreventExitStatus=78` —
    a parked daemon and a silent speaker. So a DUMB member whose box runs any
    other period (the floorless DAC8x Studio's packaged 1024) or whose period
    cannot be resolved at all does not arm: it falls back to solo, keeps playing
    its own content, and exits non-zero so the oneshot shows failed."""
    import json

    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(
        reconcile_mod, "box_outputd_period_frames", lambda: period
    )

    assert main([]) == 1
    assert "camilla_bonded" not in order
    assert target.read_text() == f"{SERVER_KEY}=\n{CLIENT_KEY}=\n"
    env = (tmp_path / "grouping-outputd.env").read_text()
    assert f"{DAC_CONTENT_LANE_ENV}=\n" in env
    status = json.loads((tmp_path / "grouping-follower-status.json").read_text())
    assert status.get("blocked_reason") == "dac_content_ring_period_mismatch"
    assert status.get("local_sources_allowed") is True


def test_refused_follower_status_allows_sources_after_solo_fallback(
    tmp_path,
    monkeypatch,
):
    """A refused follower is not left mute: the status main() persists un-parks
    local sources, and the guard that reads it agrees."""
    from jasper.multiroom.effective_role import (
        effective_local_sources_park_reason,
        read_effective_role_status,
    )

    requested = _follower(leader_addr="192.168.1.50")
    _patch_main_io(monkeypatch, tmp_path, requested)
    _refuse_follower_bond(monkeypatch)

    assert main([]) == 1
    status_path = tmp_path / "grouping-follower-status.json"
    status = read_effective_role_status(str(status_path))
    assert status["blocked_reason"] == "graph_unprovable"
    assert status["local_sources_allowed"] is True
    assert (
        effective_local_sources_park_reason(
            requested,
            status=status,
            boot_id_reader=lambda: status["boot_id"],
        )
        is None
    )


def test_refused_follower_stays_parked_until_solo_unit_plan_succeeds(
    tmp_path,
    monkeypatch,
):
    import json

    requested = _follower(leader_addr="192.168.1.50")
    _patch_main_io(monkeypatch, tmp_path, requested)
    _refuse_follower_bond(monkeypatch)
    status_path = tmp_path / "grouping-follower-status.json"

    def fail_plan(_decision):
        in_progress = json.loads(status_path.read_text())
        assert in_progress["local_sources_allowed"] is False
        return 1

    monkeypatch.setattr(reconcile_mod, "_apply", fail_plan)

    assert main([]) == 1
    final = json.loads(status_path.read_text())
    assert final["local_sources_allowed"] is False


def test_refused_follower_grant_write_failure_keeps_prior_deny(
    tmp_path,
    monkeypatch,
):
    import json

    requested = _follower(leader_addr="192.168.1.50")
    _patch_main_io(monkeypatch, tmp_path, requested)
    _refuse_follower_bond(monkeypatch)
    real_write = reconcile_mod._write_follower_status

    def fail_only_grant(**kwargs):
        if kwargs.get("local_sources_allowed") is True:
            return False
        return real_write(**kwargs)

    monkeypatch.setattr(
        reconcile_mod,
        "_write_follower_status",
        fail_only_grant,
    )

    assert main([]) == 1
    final = json.loads((tmp_path / "grouping-follower-status.json").read_text())
    assert final["local_sources_allowed"] is False


def test_loopback_box_still_bonds_normally(tmp_path, monkeypatch):
    # Control: a non-ring (loopback) box bonds as before — the gate is ring-only.
    target, order = _patch_main_io(monkeypatch, tmp_path, _leader())
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    rc = main([])
    assert rc == 0
    assert "camilla_bonded" in order
