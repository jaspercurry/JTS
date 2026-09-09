# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.camilla_config_contract import (
    DEFAULT_CHUNKSIZE,
    DEFAULT_QUEUELIMIT,
    POST_DSP_PLAYBACK_DEVICES,
    UNPAIRED_POST_DSP_PLAYBACK_DEVICES,
    _OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE,
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_TARGET_LEVEL,
    PeqFilter,
    parse_camilla_devices_config,
    resolve_enable_rate_adjust,
    total_positive_boost_db,
)
from jasper.camilla_latency import resolve_camilla_latency_for_devices
from jasper.fanin_coupling import (
    DEFAULT_PLAYBACK_FORMAT,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_GEOMETRY,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
)


def test_pipe_sink_format_stays_narrow_while_the_alsa_lane_is_wide():
    """D4 (wide-output-path program): the pipe/File-sink format is its own
    constant, separate from the ALSA loopback lane's DEFAULT_PLAYBACK_FORMAT.
    PR-1 split them while both still read ``S16_LE``; PR-6 widened the ALSA lane
    and this is where that split stopped being latent. The pipe sink MUST stay
    narrow: snapserver's pipe source is a fixed wire contract
    (`sampleformat=48000:16:2`, jasper.multiroom.reconcile.snapserver_argv), so
    a shared name here would have silently corrupted every bonded leader's
    multiroom wire the moment the lane widened."""
    assert DEFAULT_PIPE_SINK_FORMAT == "S16_LE"
    assert DEFAULT_PLAYBACK_FORMAT == "S32_LE"
    assert DEFAULT_PIPE_SINK_FORMAT != DEFAULT_PLAYBACK_FORMAT


def test_pipe_sink_format_matches_snapserver_wire_contract():
    """NIT2 (PR-1 gate review): pin the promise between the two owners of the
    snapserver pipe wire format — DEFAULT_PIPE_SINK_FORMAT (this module) and
    the ``sampleformat=`` literal baked into
    jasper.multiroom.reconcile.snapserver_argv. They can't share code (one is
    a Python constant the CamillaDSP emitters read, the other is a literal
    inside a DIFFERENT daemon's argv builder), so this test pins them
    together the same way tests/test_wifi_profile_hardening_contract.py pins
    its three writers to one canonical contract: widening either side alone,
    without the other, fails a test naming the other.

    "16:2" is snapserver's own ``<bits>:<channels>`` syntax for its pipe
    source, not a JTS format literal — it can only mean ``S16_LE`` (no other
    live format has a 16-bit depth), so the assertion below is a genuine
    "iff": the argv carries ``48000:16:2`` iff the constant is ``S16_LE``.
    """
    from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC, GroupingConfig
    from jasper.multiroom.reconcile import snapserver_argv

    cfg = GroupingConfig(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="contract-test",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )
    argv_carries_16_2 = "sampleformat=48000:16:2" in " ".join(snapserver_argv(cfg))
    assert argv_carries_16_2 == (DEFAULT_PIPE_SINK_FORMAT == "S16_LE")


def test_camilla_emitters_emit_byte_identical_yaml_when_env_unset(monkeypatch):
    """The end-to-end byte-identical contract: the sound emitter with the None
    sentinel (env unset, no resolvable profile) must equal the pre-G7
    explicit-literal call."""
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)
    profile = SoundProfile()
    # At a sink with its own buffer. The ring end is the deliberate exception —
    # there the sentinel resolves the ring's geometry while an explicit caller
    # value still passes through, so the two calls are NOT byte-identical (see
    # test_a_ring_end_takes_the_whole_certified_geometry).
    explicit = emit_sound_config(
        profile,
        chunksize=DEFAULT_CHUNKSIZE,
        target_level=DEFAULT_TARGET_LEVEL,
        playback_device=NON_RING_SINK,
    )
    sentinel = emit_sound_config(profile, playback_device=NON_RING_SINK)
    assert sentinel == explicit


# --- ONE OWNER PER TRANSPORT: what a GENERATED CamillaDSP config carries -----
# Not "the resolver returns N" but "the config a daemon would load carries the
# geometry its transport certified". A ring end takes the whole
# RING_CAMILLA_GEOMETRY; an ordinary ALSA sink takes the box's global default.


# An ordinary ALSA sink — NOT one of the ring PCMs.
NON_RING_SINK = "hw:CARD=DAC8x,DEV=0"


def _sound_devices(**kwargs) -> dict:
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile

    return parse_camilla_devices_config(emit_sound_config(SoundProfile(), **kwargs))


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, (RING_CAMILLA_CHUNKSIZE, RING_CAMILLA_TARGET_LEVEL,
              RING_CAMILLA_QUEUELIMIT, False)),
        ({"playback_pipe_path": "/run/jasper-snapserver/snapfifo"},
         (RING_CAMILLA_CHUNKSIZE, RING_CAMILLA_TARGET_LEVEL,
          RING_CAMILLA_QUEUELIMIT, False)),
        ({"playback_device": NON_RING_SINK},
         (DEFAULT_CHUNKSIZE, DEFAULT_TARGET_LEVEL, DEFAULT_QUEUELIMIT, True)),
    ],
    ids=["ring-playback", "ring-capture-file-sink", "alsa-sink"],
)
def test_a_ring_end_takes_the_whole_certified_geometry(monkeypatch, kwargs, expected):
    """The four fields move TOGETHER, decided by the graph's governing device.

    chunk 128 is one ring slot, queuelimit 1 makes the slot handshake blocking,
    and rate_adjust is off because a ring PCM is an ioplug alsa-lib reports as
    card -1 — CamillaDSP builds no HCtl and can actuate nothing. Mixing a box
    floor's chunk/target into that pairing is what put a 1536-frame target on a
    256-frame ring. A File sink declares no ALSA buffer, so its ring CAPTURE
    governs; an ordinary ALSA sink governs its own graph and keeps the default.
    """
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    monkeypatch.delenv("JASPER_CAMILLA_TARGET_LEVEL", raising=False)

    parsed = _sound_devices(**kwargs)

    assert (
        parsed["chunksize"],
        parsed["target_level"],
        parsed["queuelimit"],
        parsed["enable_rate_adjust"],
    ) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("384", 384), ("", DEFAULT_CHUNKSIZE), ("  ", DEFAULT_CHUNKSIZE),
     ("bogus", DEFAULT_CHUNKSIZE), ("0", DEFAULT_CHUNKSIZE),
     ("-256", DEFAULT_CHUNKSIZE), ("1.5", DEFAULT_CHUNKSIZE)],
)
def test_the_operator_chunk_knob_is_a_positive_int_or_the_default(
    monkeypatch, raw, expected
):
    """A valid positive override is honored; anything else degrades to the
    default rather than emitting a config CamillaDSP would refuse to load."""
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", raw)

    chunksize, _, _ = resolve_camilla_latency_for_devices(
        capture_device=RING_CAPTURE_DEVICE, playback_device=NON_RING_SINK
    )

    assert chunksize == expected


def test_the_operator_knobs_cannot_reach_a_ring_end(monkeypatch):
    """The knobs tune a graph the BOX owns; the ring's geometry is not the
    box's to tune, so an operator (or a stale reconciled env) cannot put an
    unopenable chunk on the transport."""
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "1024")
    monkeypatch.setenv("JASPER_CAMILLA_TARGET_LEVEL", "4096")

    parsed = _sound_devices()

    assert parsed["playback_device"] == RING_PLAYBACK_DEVICE
    assert parsed["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert parsed["target_level"] == RING_CAMILLA_TARGET_LEVEL


def test_fresh_flat_outputd_cutover_takes_the_ring_geometry(monkeypatch, tmp_path):
    """The flat boot graph passes the certified pairing EXPLICITLY.

    It is one of the two end-to-end ring graphs that hand
    RING_CAMILLA_GEOMETRY straight to the emitter instead of resolving it, so
    it must land on the same values the resolver answers for a ring end — two
    routes to one geometry, never two geometries.
    """
    monkeypatch.setenv("JASPER_CAMILLA_CHUNKSIZE", "1024")
    monkeypatch.setenv("JASPER_CAMILLA_TARGET_LEVEL", "4096")

    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    out = tmp_path / "outputd-cutover.yml"
    parsed = parse_camilla_devices_config(
        emit_flat_outputd_cutover_config(out_path=out)
    )
    assert out.exists()
    assert parsed["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert parsed["target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert parsed["playback_device"] == RING_PLAYBACK_DEVICE


@pytest.mark.parametrize(
    ("playback_device", "rate_adjust"),
    [
        (RING_PLAYBACK_DEVICE, False),
        (RING_ACTIVE_PLAYBACK_DEVICE, False),
        (None, False),
        ("hw:CARD=Dac,DEV=0", True),
    ],
    ids=["ring-b", "ring-active", "file", "alsa-dac"],
)
def test_enable_rate_adjust_follows_the_sink(playback_device, rate_adjust):
    """The sink decides, never the graph's role: a File sink (``None``) has no
    output clock to steer, and a ring PCM is an ioplug alsa-lib reports as card
    -1, so CamillaDSP builds no HCtl and can actuate nothing. An ordinary ALSA
    sink can.
    """

    assert resolve_enable_rate_adjust(playback_device) is rate_adjust


def test_total_positive_boost_db_sums_only_boosts():
    # The canonical audio-safety primitive: worst-case additive boost.
    # Cuts are ignored; the result is the headroom a config must reserve so
    # boosts can't clip above unity. Shared by the emitter trim and the PEQ
    # boost-cap check, so pin it here.
    assert total_positive_boost_db([]) == 0.0
    assert total_positive_boost_db([PeqFilter(80, 4, -6.0)]) == 0.0  # cuts-only
    assert total_positive_boost_db(
        [PeqFilter(45, 5, 2.0), PeqFilter(80, 6, -4.0), PeqFilter(120, 4, 1.0)]
    ) == 3.0  # +2 and +1 stack; the -4 cut is not subtracted


def test_parse_camilla_devices_config_extracts_clock_and_outputd_lanes() -> None:
    parsed = parse_camilla_devices_config(
        """
        ---
        devices:
          samplerate: 48000
          chunksize: 1024
          target_level: 2048
          volume_limit: 0.0
          capture:
            type: Alsa
            channels: 2
            device: "plug:jasper_capture"
          playback:
            type: Alsa
            channels: 2
            device: "outputd_content_playback"
        filters:
          flat:
            type: Gain
        """
    )

    assert parsed == {
        "samplerate": 48000,
        "chunksize": 1024,
        "target_level": 2048,
        "volume_limit": 0.0,
        "capture_channels": 2,
        "capture_device": "plug:jasper_capture",
        "capture_type": "Alsa",
        "playback_channels": 2,
        "playback_device": "outputd_content_playback",
        "playback_type": "Alsa",
    }


def test_parse_camilla_devices_config_rejects_ambiguous_volume_limit() -> None:
    assert "volume_limit" not in parse_camilla_devices_config(
        "devices:\n"
        "  volume_limit: 0.0\n"
        "  volume_limit: 9.0\n"
    )
    assert "volume_limit" not in parse_camilla_devices_config(
        "devices:\n"
        "  volume_limit: 0.0\n"
        "devices: {volume_limit: 9.0}\n"
    )
    for value in ("nan", "inf", "-inf"):
        assert "volume_limit" not in parse_camilla_devices_config(
            f"devices:\n  volume_limit: {value}\n"
        )


def test_parse_camilla_devices_config_ignores_nested_volume_limit() -> None:
    for nested_block in ("playback", "metadata"):
        parsed = parse_camilla_devices_config(
            "devices:\n"
            f"  {nested_block}:\n"
            "    volume_limit: 0.0\n"
        )

        assert "volume_limit" not in parsed




def test_the_certified_ring_pairing_agrees_with_the_sink_rule():
    """RING_CAMILLA_GEOMETRY is measured data passed whole by the end-to-end
    ring graphs; its rate-adjust member must be what the sink rule resolves for
    the ring it was certified on, or the two owners drift."""

    assert RING_CAMILLA_GEOMETRY["enable_rate_adjust"] is resolve_enable_rate_adjust(
        RING_ACTIVE_PLAYBACK_DEVICE
    )


def test_the_unpaired_endpoints_are_exactly_the_ones_the_map_does_not_pair():
    """`transport_coherence_report` routes a playback device on the ABSENCE of a
    pairing, so the two sets must partition the post-DSP endpoints — a member in
    both would let one device be simultaneously paired and unpaired."""

    paired = set(_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE)

    assert paired <= POST_DSP_PLAYBACK_DEVICES
    assert paired & UNPAIRED_POST_DSP_PLAYBACK_DEVICES == set()
    assert paired | UNPAIRED_POST_DSP_PLAYBACK_DEVICES == POST_DSP_PLAYBACK_DEVICES
