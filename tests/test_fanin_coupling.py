# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in -> CamillaDSP coupling selector contracts."""

from __future__ import annotations

import pytest

from jasper.fanin_coupling import (
    COUPLING_SHM_RING,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    RING_WIRE_FORMAT,
    RING_WIRE_FORMAT_WIDE,
    capture_kwargs_for_coupling,
    coupling_value_removed,
    resolve_coupling,
    resolve_ring_path,
    resolve_ring_slots,
    ring_capacity_frames,
)
from jasper.camilla_config_contract import parse_camilla_devices_config
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile

from .fanin_env_fixtures import declare_fanin_env


def test_resolve_coupling_names_the_ring_or_nothing():
    # ONE transport (ADR-0100): the resolver either names it or answers None.
    # None is "this file names no transport" — an unwritten key and a retired
    # token alike — never a second route; `coupling_value_removed` is what tells
    # those two apart.
    assert resolve_coupling(" SHM_RING ") == COUPLING_SHM_RING
    assert resolve_coupling("Shm_Ring") == COUPLING_SHM_RING
    for raw in ("loopback", "fifo", "pipe", "disabled", "transport_pipe",
                None, "", "  "):
        assert resolve_coupling(raw) is None


def test_coupling_value_removed_flags_every_token_but_the_ring():
    # True iff present but not in VALID_COUPLINGS. Since ADR-0100 that includes
    # `loopback` — the retired route a migrating box may still carry — alongside
    # the older removed tokens and any typo. Unset / empty is NOT "removed": an
    # absent key is the ordinary state of a box the reconciler has not written.
    assert coupling_value_removed("loopback") is True
    assert coupling_value_removed("transport_pipe") is True
    assert coupling_value_removed("fifo") is True
    assert coupling_value_removed("shm_ring") is False
    assert coupling_value_removed(None) is False
    assert coupling_value_removed("") is False


def test_shm_ring_kwargs_are_full_ring_topology_capture_and_playback():
    # P2: shm_ring is the END-TO-END ring topology — Ring A capture
    # (jts_ring_capture) AND Ring B playback (jts_ring_playback), both at the
    # box's resolved wire (resolve_ring_wire()). The two ends flip together; a
    # half-ring config (ring capture + ALSA loopback playback) would strand one
    # end, so the emit kwargs MUST carry both devices.
    kwargs = capture_kwargs_for_coupling()
    assert kwargs == {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": RING_WIRE_FORMAT_WIDE,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT_WIDE,
    }
    # S32_LE, NOT the historical S16LE default — resolve_ring_wire_format's
    # default flipped WIDE in PR #2601 (convergence design §3.2/B3): narrow was a
    # width REGRESSION on the loopback CamillaDSP->outputd hop the ring replaces,
    # which already carries DEFAULT_PLAYBACK_FORMAT (S32_LE). RING_WIRE_FORMAT
    # (S16_LE) still exists — it is now the NARROW rollback token an operator
    # pins via JASPER_FANIN_RING_WIRE_FORMAT, not the shipped/default wire — so
    # both tokens' literal spellings are pinned here, "narrow" and "wide" alike.
    assert RING_WIRE_FORMAT_WIDE == "S32_LE"
    assert RING_WIRE_FORMAT == "S16_LE"
    assert RING_CAPTURE_DEVICE == "jts_ring_capture"
    assert RING_PLAYBACK_DEVICE == "jts_ring_playback"


def test_content_lane_format_is_one_definition_for_both_ends_of_the_hop():
    """ONE function answers the CamillaDSP→outputd hop's width, so CamillaDSP's
    emitted `playback: format:` and outputd's requested
    JASPER_OUTPUTD_CONTENT_FORMAT cannot disagree.

    The answer is the ring wire's resolved format for this box — there is no
    second route to select (ADR-0100), and the function takes no token to
    pretend otherwise.
    """
    from jasper.fanin_coupling import content_lane_format_for_coupling

    assert content_lane_format_for_coupling() == RING_WIRE_FORMAT_WIDE
    # The answer is the kwargs' own value, not a second literal: it tracks
    # capture_kwargs_for_coupling, which is what actually reaches the emitters.
    assert (
        content_lane_format_for_coupling()
        == capture_kwargs_for_coupling()["playback_format"]
    )


def test_shm_ring_ring_path_and_slots_resolve_with_fail_safe_defaults():
    assert resolve_ring_path(None) == DEFAULT_FANIN_RING_PATH
    assert resolve_ring_path("") == DEFAULT_FANIN_RING_PATH
    assert resolve_ring_path("   ") == DEFAULT_FANIN_RING_PATH
    assert resolve_ring_path("  /dev/shm/jts-ring/lab.ring ") == "/dev/shm/jts-ring/lab.ring"
    assert DEFAULT_FANIN_RING_PATH == "/dev/shm/jts-ring/program.ring"

    # Unset / empty / whitespace-only -> the validated default (matches Rust
    # env_u32's empty-is-default handling).
    assert resolve_ring_slots(None) == DEFAULT_FANIN_RING_SLOTS
    assert resolve_ring_slots("") == DEFAULT_FANIN_RING_SLOTS
    assert resolve_ring_slots("   ") == DEFAULT_FANIN_RING_SLOTS
    assert resolve_ring_slots("  16 ") == 16
    assert resolve_ring_slots("2") == 2
    assert DEFAULT_FANIN_RING_SLOTS == 2
    # A present-but-out-of-range or unparseable value FAILS LOUD (never a
    # silent clamp) — repo doctrine, and it must agree with the Rust daemon,
    # which returns a config-class Err on the same range (jasper-fanin then
    # exits 78 and the unit parks rather than restart-looping).
    for bad in ("1", "0", "17", "100", "-1", "garbage", "8.5"):
        with pytest.raises(ValueError):
            resolve_ring_slots(bad)


def test_ring_kwargs_emit_ring_capture_device_s32le():
    # SF-2: the ring capture kwargs DO flow through
    # coupling_capture_kwargs_from_env into the product emitters (transport_pipe
    # precedent) — this is deliberate coherence. A household /sound/ save emits a
    # CamillaDSP config whose ALSA capture device is jts_ring_capture + the box's
    # resolved wire format, so the emitted config and the running fan-in daemon
    # name the SAME ring. (That device only RESOLVES once the arm script has
    # installed the ioplug conf.d block.)
    #
    # Renamed from ...s16le: an undeclared box now resolves S32_LE (the
    # resolver's default flipped WIDE in PR #2601). S16_LE survives only as the
    # operator's explicit JASPER_FANIN_RING_WIRE_FORMAT rollback pin — see
    # test_resolve_ring_wire_is_the_same_wide_wire_on_every_topology for the
    # per-topology walk that used to carry this test's old name.
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    armed_kwargs = coupling_capture_kwargs_from_env()
    cfg = emit_sound_config(SoundProfile(), profile_id="x", **armed_kwargs)

    capture_block = cfg.split("  capture:\n", 1)[1].split("\n  playback:\n", 1)[0]
    assert "type: Alsa" in capture_block
    assert f'device: "{RING_CAPTURE_DEVICE}"' in capture_block
    assert f"format: {RING_WIRE_FORMAT_WIDE}" in capture_block
    # It is NOT the transport_pipe RawFile/pipe shape, and NOT the dsnoop default.
    assert "type: RawFile" not in capture_block
    assert 'device: "plug:jasper_capture"' not in capture_block
    # P2: the Ring B playback end flips together with capture — the emit names the
    # ring PLAYBACK device too, so the config is coherent end-to-end (not a
    # half-ring capture-only config that strands outputd).
    playback_block = cfg.split("\n  playback:\n", 1)[1].split("\nfilters:\n", 1)[0]
    assert "type: Alsa" in playback_block
    assert f'device: "{RING_PLAYBACK_DEVICE}"' in playback_block
    assert f"format: {RING_WIRE_FORMAT_WIDE}" in playback_block
    assert 'device: "outputd_content_playback"' not in playback_block
    # S32_LE on an undeclared box — matches loopback's DEFAULT_PLAYBACK_FORMAT
    # rather than narrowing the hop the ring replaces (convergence design
    # §3.2/B3). RING_WIRE_FORMAT (S16_LE) is the operator's narrow rollback
    # token now, not what an armed-but-undeclared box emits.
    assert RING_WIRE_FORMAT_WIDE == "S32_LE"
    # The coupling carries DEVICES, not geometry: this emit resolves its own
    # chunk through resolve_camilla_latency_for_devices, which clamps a ring end
    # to what the transport can negotiate. The VALUE is the box's floor and
    # varies; the bound does not.
    devices = parse_camilla_devices_config(cfg)
    assert devices["chunksize"] <= ring_capacity_frames()


def test_capture_kwargs_from_env_are_the_ring_with_no_coupling_declared_at_all(
    monkeypatch,
):
    """THE PIN for ADR-0100's capture seam: the answer is UNCONDITIONAL.

    The emitters that reach this — jasper-web `/sound/`, jasper-correction-web
    `/sound/room/`, and the correction session's mid-EQ-apply re-emit — do not
    `EnvironmentFile=` fanin.env, so `os.environ` carries no coupling token, and
    a box that has never been written carries none on disk either. While the
    loopback route existed that resolved to `{}` and the caller kept its dsnoop
    defaults. With the ring the only transport, a `{}` fallthrough would emit a
    graph whose capture names a lane nothing writes — a dead-lane CamillaDSP
    config, applied silently in the middle of an EQ save. So: no env, no file,
    the full ring topology.
    """
    from jasper import fanin_coupling

    # Nothing declares a coupling: not the process env, and not the file — which
    # this test also proves is never consulted.
    def _boom(*a, **k):
        raise AssertionError("the capture kwargs must not depend on a coupling token")

    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling", _boom
    )
    monkeypatch.delenv("JASPER_FANIN_CAMILLA_COUPLING", raising=False)

    assert fanin_coupling.coupling_capture_kwargs_from_env() == {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": RING_WIRE_FORMAT_WIDE,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT_WIDE,
    }


@pytest.mark.parametrize(
    "fanin_text,wide",
    [
        ("", True),
        ("JASPER_FANIN_CAMILLA_COUPLING=\n", True),
        ("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n", True),
        ("JASPER_FANIN_CAMILLA_COUPLING=loopback\n", False),
        ("JASPER_FANIN_CAMILLA_COUPLING=wat\n", False),
    ],
    ids=["absent_key", "empty", "declared", "retired_token", "typo"],
)
def test_assistant_wire_width_reads_the_file_through_the_shared_predicate(
    monkeypatch, tmp_path, fanin_text, wide
):
    """The FILE-default half of `assistant_wire_is_wide`, on a wide-format box.

    ADR-0100 left one transport, so `jasper-fanin` serves an absent key, an empty
    value and the token alike; only a value it REFUSES (exit 78, the unit parks)
    takes the box off the wide wire. Requiring the literal token here resolved
    NARROW on every unwritten box while the daemon on it ran WIDE (#3655).
    """
    from jasper import fanin_coupling

    declare_fanin_env(monkeypatch, tmp_path, fanin_text)

    assert (
        fanin_coupling.assistant_wire_is_wide(wire_format=RING_WIRE_FORMAT_WIDE)
        is wide
    )


# --- Ring B (outputd content bridge) vocabulary + coherence (P2) -------------


def test_outputd_bridge_is_ring_truth_table_matches_the_daemon():
    """The absence answer, pinned against the daemon it has to agree with.

    UNDECLARED IS THE RING on both sides: `Config::from_env` defaults
    `JASPER_OUTPUTD_CONTENT_BRIDGE` to `shm_ring`, so a surface that read
    absence the other way called a healthy box's transport split — a PLAYING
    speaker reported SILENT. Everything else answers away from the ring, which
    is also what outputd does with those values: it parks.
    """
    from jasper.fanin_coupling import outputd_bridge_is_ring

    # Undeclared, in all three shapes a caller can hand over.
    for undeclared in (None, "", "   "):
        assert outputd_bridge_is_ring(undeclared) is True, repr(undeclared)
    # The daemon's own alias set, case- and whitespace-insensitive.
    for alias in ("shm_ring", "SHM_RING", " Shm_Ring ", "shmring", "ring"):
        assert outputd_bridge_is_ring(alias) is True, alias
    # The retired route, its parse aliases, the deleted lab bridge, and a typo.
    for off in ("direct", "off", "disabled", "rate_match", "ratematch", "wat"):
        assert outputd_bridge_is_ring(off) is False, off


@pytest.mark.parametrize(
    "env,expected",
    [
        pytest.param({}, True, id="undeclared_is_the_ring"),
        pytest.param(
            {"JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring"}, True, id="declared"
        ),
        pytest.param({"JASPER_OUTPUTD_CONTENT_BRIDGE": "direct"}, False, id="retired"),
        pytest.param({"JASPER_OUTPUTD_DAC_CONTENT_LANE": "1"}, False, id="marker"),
        pytest.param(
            {"JASPER_OUTPUTD_DAC_CONTENT_LANE": "on"}, False, id="marker_word"
        ),
        pytest.param({"JASPER_OUTPUTD_DAC_CONTENT_LANE": "0"}, True, id="marker_off"),
        pytest.param(
            {"JASPER_OUTPUTD_DAC_CONTENT_LANE": ""}, True, id="marker_cleared"
        ),
    ],
)
def test_the_central_ring_predicate_reads_both_keys(env, expected):
    """The bridge key alone stopped answering this once the marker SELECTED.

    An armed dac-content marker resolves `ContentBridgeMode::DacContentRing`, so
    outputd attaches the bonded return ring and leaves `shm_ring` unattached —
    with the bridge key ABSENT, which the bridge predicate alone reads as the
    ring. The marker is read through outputd's `env_bool` accept-set, so a
    CLEARED key (the grouping reconciler's own disable spelling) is not armed.
    """
    from jasper.fanin_coupling import outputd_content_is_central_ring

    assert outputd_content_is_central_ring(env) is expected


@pytest.mark.parametrize(
    "bridge,served",
    [
        pytest.param(None, True, id="absent"),
        pytest.param("", True, id="blank_clears_layer_one"),
        pytest.param("   ", True, id="whitespace_is_blank"),
        pytest.param("shm_ring", False, id="what_coupling_auto_writes"),
        pytest.param("direct", False, id="any_declared_value"),
    ],
)
def test_served_and_contradicted_split_every_marker_armed_box(bridge, served):
    """OUTPUTD'S ACCEPTANCE, MIRRORED KEY FOR KEY.

    outputd reads the bridge with `env_optional` (blank == undeclared) and bails
    EX_CONFIG on the marker beside a DECLARED one. The two predicates partition
    the armed boxes so no surface has to guess which side it is on.
    """
    from jasper.fanin_coupling import (
        dac_content_marker_contradicted,
        dac_content_ring_served,
    )

    env = {"JASPER_OUTPUTD_DAC_CONTENT_LANE": "1"}
    if bridge is not None:
        env["JASPER_OUTPUTD_CONTENT_BRIDGE"] = bridge

    assert dac_content_ring_served(env) is served
    assert dac_content_marker_contradicted(env) is (not served)
    # Neither answers True without the marker at all.
    unarmed = {k: v for k, v in env.items() if k != "JASPER_OUTPUTD_DAC_CONTENT_LANE"}
    assert dac_content_ring_served(unarmed) is False
    assert dac_content_marker_contradicted(unarmed) is False


def test_the_marker_predicate_reads_the_key_the_ring_module_owns():
    """One key, one owner: the reader and the writer's constant must agree."""
    from jasper.fanin_coupling import dac_content_lane_marker_armed
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_LANE_ENV

    assert DAC_CONTENT_LANE_ENV == "JASPER_OUTPUTD_DAC_CONTENT_LANE"
    assert dac_content_lane_marker_armed({DAC_CONTENT_LANE_ENV: "1"}) is True
    assert dac_content_lane_marker_armed({DAC_CONTENT_LANE_ENV: "0"}) is False


def test_outputd_bridge_ring_aliases_match_the_rust_accept_set():
    """Drift pin: a Rust arm this predicate does not know reads as NOT the ring.

    `Config::from_env` and this predicate are the two answers to one question,
    and the Rust side is the one that decides what actually runs — so a widened
    match arm there with no counterpart here would report a box as off the
    transport it is demonstrably serving.
    """
    from pathlib import Path
    import re

    from jasper.fanin_coupling import _OUTPUTD_RING_BRIDGE_SPELLINGS

    config_rs = (
        Path(__file__).resolve().parents[1]
        / "rust/jasper-outputd/src/config.rs"
    ).read_text(encoding="utf-8")
    arm = re.search(
        r'^\s*((?:"[a-z_]+"\s*\|\s*)*"[a-z_]+")\s*=>\s*ContentBridgeMode::ShmRing,',
        config_rs,
        re.MULTILINE,
    )
    assert arm is not None, "the ShmRing parse arm moved; re-anchor this pin"
    rust_spellings = set(re.findall(r'"([a-z_]+)"', arm.group(1)))
    assert rust_spellings == set(_OUTPUTD_RING_BRIDGE_SPELLINGS), (
        rust_spellings,
        set(_OUTPUTD_RING_BRIDGE_SPELLINGS),
    )


def test_resolve_outputd_ring_path_and_slots_fail_safe():
    from jasper.fanin_coupling import (
        DEFAULT_OUTPUTD_RING_PATH,
        DEFAULT_OUTPUTD_RING_SLOTS,
        resolve_outputd_ring_path,
        resolve_outputd_ring_slots,
    )

    assert resolve_outputd_ring_path(None) == DEFAULT_OUTPUTD_RING_PATH
    assert resolve_outputd_ring_path("  ") == DEFAULT_OUTPUTD_RING_PATH
    assert resolve_outputd_ring_path(" /dev/shm/x.ring ") == "/dev/shm/x.ring"
    assert DEFAULT_OUTPUTD_RING_PATH == "/dev/shm/jts-ring/content.ring"

    assert resolve_outputd_ring_slots(None) == DEFAULT_OUTPUTD_RING_SLOTS
    assert resolve_outputd_ring_slots("") == DEFAULT_OUTPUTD_RING_SLOTS
    assert resolve_outputd_ring_slots("2") == 2
    assert resolve_outputd_ring_slots(" 16 ") == 16
    assert DEFAULT_OUTPUTD_RING_SLOTS == 2
    # Out-of-range / unparseable fail loud (mirror the Rust MIN/MAX; no clamp).
    for bad in ("1", "0", "17", "-1", "garbage", "2.5"):
        with pytest.raises(ValueError):
            resolve_outputd_ring_slots(bad)


# --- resolve_ring_wire: one resolution, four declarers -------------------------


def test_resolve_ring_wire_answers_the_shipped_geometry_with_no_topology():
    from jasper.fanin_coupling import (
        RING_A_CHANNELS,
        RING_SLOT_FRAMES,
        resolve_ring_wire,
    )

    wire = resolve_ring_wire()
    # WIDE: the shipped conf.d now DECLARES S32_LE explicitly in every block
    # (deploy/alsa/conf.d/60-jts-ring.conf), matching resolve_ring_wire_format's
    # own default for an undeclared box — "shipped geometry" and "the resolver's
    # default" are the same answer by construction, not two facts that happen to
    # agree.
    assert wire.sample_format == RING_WIRE_FORMAT_WIDE
    assert wire.ring_a_channels == RING_A_CHANNELS
    assert wire.ring_b_channels == RING_A_CHANNELS
    assert wire.period_frames == RING_SLOT_FRAMES


def test_resolve_ring_wire_is_the_same_wide_wire_on_every_topology():
    """DORMANCY BAR, RE-POINTED to the new invariant.

    Renamed from ..._is_narrow_stereo_on_every_topology: since the resolver's
    default flipped WIDE (PR #2601), no topology resolves S16_LE any more —
    the invariant this walk protects was never "the wire is narrow", it was
    "the format axis is not per-topology". :func:`resolve_ring_wire`'s own
    docstring says why: ``sample_format`` comes from
    :func:`read_declared_ring_wire_format` alone, resolved once per box before
    any topology is even consulted, while only ``ring_b_channels`` (and
    ``ring_active_channels``) vary with the topology argument. This walk keeps
    proving that split holds — a resolver that let sample_format leak a
    per-topology branch would flip that box's emitted config, conf.d and
    outputd env in one deploy, silently, on exactly one topology shape.
    """
    from jasper.fanin_coupling import RING_A_CHANNELS, resolve_ring_wire
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _full_range_mono,
        _full_range_stereo,
        _subwoofer_topology,
        _topology,
    )
    from tests.test_runtime_contract_ring import _dual_apple_stereo

    for label, topology in (
        ("none", None),
        ("unconfigured", _topology([])),
        ("full_range_stereo", _full_range_stereo()),
        ("full_range_mono", _full_range_mono()),
        ("subwoofer", _subwoofer_topology()),
        ("composite", _dual_apple_stereo()),
        ("active_2_way", _active_topology("stereo", "active_2_way")),
    ):
        wire = resolve_ring_wire(topology)
        assert wire.sample_format == RING_WIRE_FORMAT_WIDE, label
        assert wire.ring_a_channels == RING_A_CHANNELS, label
        assert wire.ring_b_channels == RING_A_CHANNELS, label


def test_resolve_ring_wire_reads_ring_b_channels_from_the_topology(monkeypatch):
    """The Ring B channel axis is genuinely wired to the topology resolver.

    Without this, "resolve_ring_wire consults the topology" is unfalsifiable
    while every topology answers the same width: the dormancy bar above passes
    identically for a resolver that ignores its argument. Patching the topology
    answer is what separates the two.
    """
    import jasper.active_speaker.runtime_contract as rc
    from jasper.fanin_coupling import RING_A_CHANNELS, resolve_ring_wire
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    topology = _full_range_stereo()
    monkeypatch.setattr(rc, "ring_channels_for_topology", lambda _t: 6)
    wire = resolve_ring_wire(topology)
    assert wire.ring_b_channels == 6
    # Ring A is NOT a topology axis — the program upstream of Camilla is stereo.
    assert wire.ring_a_channels == RING_A_CHANNELS


def test_resolve_ring_wire_falls_back_to_the_shipped_width_for_no_ring_topology(
    monkeypatch,
):
    # An ineligible topology has no ring width; the wire still describes what
    # the conf.d on that box declares, which is the shipped stereo geometry.
    # Refusing to ARM is the preflights' job, not the resolver's.
    import jasper.active_speaker.runtime_contract as rc
    from jasper.fanin_coupling import RING_A_CHANNELS, resolve_ring_wire
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    monkeypatch.setattr(rc, "ring_channels_for_topology", lambda _t: None)
    wire = resolve_ring_wire(_full_range_stereo())
    assert wire.ring_b_channels == RING_A_CHANNELS


def test_ring_wire_formats_are_exactly_the_two_the_ioplug_accepts():
    """The Python vocabulary must be the C parser's, token for token.

    A token the ioplug rejects is a conf.d that fails `open()` with -EINVAL; a
    token it accepts and Python cannot name is a wire no Python surface can
    report. Both are the same drift.
    """
    from pathlib import Path

    from jasper.fanin_coupling import RING_WIRE_FORMATS

    c_src = (
        Path(__file__).resolve().parents[1]
        / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"
    ).read_text(encoding="utf-8")
    assert set(RING_WIRE_FORMATS) == {"S16_LE", "S32_LE"}
    for token in RING_WIRE_FORMATS:
        assert f'strcmp(format_name, "{token}")' in c_src, token
    assert 'format %s unsupported (S16_LE|S32_LE)' in c_src


def test_capture_kwargs_take_their_format_from_the_resolver(monkeypatch):
    # The emitted config's width is the resolver's answer, not a constant the
    # emitter re-derives. Patch the resolver; both ring ends must follow.
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S32_LE",
            ring_a_channels=2,
            ring_b_channels=6,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    kwargs = fc.capture_kwargs_for_coupling()
    assert kwargs["capture_format"] == "S32_LE"
    assert kwargs["playback_format"] == "S32_LE"
    assert fc.content_lane_format_for_coupling() == "S32_LE"
