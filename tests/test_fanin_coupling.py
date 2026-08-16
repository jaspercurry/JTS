# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in -> CamillaDSP coupling selector contracts."""

from __future__ import annotations

import pytest

from jasper.fanin_coupling import (
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    RING_CAPTURE_DEVICE,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_ENABLE_RATE_ADJUST,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
    RING_PLAYBACK_DEVICE,
    RING_WIRE_FORMAT,
    RING_WIRE_FORMAT_WIDE,
    capture_kwargs_for_coupling,
    coupling_value_removed,
    is_shm_ring_coupling,
    resolve_coupling,
    resolve_ring_path,
    resolve_ring_slots,
)
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile


def test_resolve_coupling_defaults_to_loopback():
    assert resolve_coupling(None) == COUPLING_LOOPBACK
    assert resolve_coupling("") == COUPLING_LOOPBACK
    assert resolve_coupling("   ") == COUPLING_LOOPBACK


def test_resolve_coupling_accepts_explicit_transports_case_insensitive():
    assert resolve_coupling("loopback") == COUPLING_LOOPBACK
    assert resolve_coupling(" SHM_RING ") == COUPLING_SHM_RING
    assert resolve_coupling("Shm_Ring") == COUPLING_SHM_RING


def test_resolve_coupling_removed_transport_pipe_fails_safe_to_loopback():
    # transport_pipe was REMOVED 2026-07-11; a migrating box's persisted value
    # must fail safe to loopback, never crash or silently keep a deleted mode.
    assert resolve_coupling("transport_pipe") == COUPLING_LOOPBACK
    assert resolve_coupling(" TRANSPORT_PIPE ") == COUPLING_LOOPBACK
    # capture kwargs resolve loopback -> the byte-identical empty dict.
    assert capture_kwargs_for_coupling("transport_pipe") == {}


def test_coupling_value_removed_flags_removed_and_typo_values():
    # True iff present but unrecognized: the removed transport_pipe OR a typo.
    # Unset / empty / recognized tokens are NOT "removed".
    assert coupling_value_removed("transport_pipe") is True
    assert coupling_value_removed("fifo") is True
    assert coupling_value_removed("shm_ring") is False
    assert coupling_value_removed("loopback") is False
    assert coupling_value_removed(None) is False
    assert coupling_value_removed("") is False


def test_is_shm_ring_coupling_predicate():
    assert is_shm_ring_coupling("shm_ring") is True
    assert is_shm_ring_coupling("loopback") is False
    assert is_shm_ring_coupling("transport_pipe") is False
    assert is_shm_ring_coupling(None) is False
    # A typo must never flip on the ring capture.
    assert is_shm_ring_coupling("ring") is False


def test_shm_ring_kwargs_are_full_ring_topology_capture_and_playback():
    # P2: shm_ring is the END-TO-END ring topology — Ring A capture
    # (jts_ring_capture) AND Ring B playback (jts_ring_playback), both at the
    # box's resolved wire (resolve_ring_wire()). The two ends flip together; a
    # half-ring config (ring capture + ALSA loopback playback) would strand one
    # end, so the emit kwargs MUST carry both devices.
    kwargs = capture_kwargs_for_coupling("shm_ring")
    assert kwargs == {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": RING_WIRE_FORMAT_WIDE,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT_WIDE,
        "chunksize": RING_CAMILLA_CHUNKSIZE,
        "target_level": RING_CAMILLA_TARGET_LEVEL,
        "queuelimit": RING_CAMILLA_QUEUELIMIT,
        "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
    }
    # S32_LE, NOT the historical S16LE default — resolve_ring_wire_format's
    # default flipped WIDE 2026-08-11 (convergence design §3.2/B3): narrow was a
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
    """wide-output-path PR-6: ONE function answers the CamillaDSP→outputd hop's
    width, so CamillaDSP's emitted `playback: format:` and outputd's requested
    JASPER_OUTPUTD_CONTENT_FORMAT cannot disagree.

    loopback answers the box-wide program lane (the emitters' own default, since
    loopback kwargs are deliberately empty); shm_ring answers
    resolve_ring_wire()'s resolved format for this box. Since the ring wire's
    resolver default flipped WIDE (2026-08-11, convergence design §3.2/B3), an
    undeclared box's shm_ring answer now EQUALS loopback's
    DEFAULT_PLAYBACK_FORMAT — the "shm_ring coupling FORCES the lane narrow"
    asymmetry the PR-6 ring ruling used to describe is GONE; only an operator's
    explicit JASPER_FANIN_RING_WIRE_FORMAT=S16_LE pin still narrows the ring
    below loopback's width. Fail-safe values follow resolve_coupling.
    """
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
    from jasper.fanin_coupling import content_lane_format_for_coupling

    assert content_lane_format_for_coupling("shm_ring") == RING_WIRE_FORMAT_WIDE
    assert content_lane_format_for_coupling("loopback") == DEFAULT_PLAYBACK_FORMAT
    # The two now agree on an undeclared box (both S32_LE) — proof the old
    # narrow-vs-wide asymmetry is gone, not just that each resolves to something.
    assert content_lane_format_for_coupling("shm_ring") == DEFAULT_PLAYBACK_FORMAT
    # Unset / empty / typo / the removed transport_pipe all resolve loopback.
    for raw in (None, "", "   ", "ring", "transport_pipe"):
        assert content_lane_format_for_coupling(raw) == DEFAULT_PLAYBACK_FORMAT
    # The ring answer is the kwargs' own value, not a second literal: it tracks
    # capture_kwargs_for_coupling, which is what actually reaches the emitters.
    assert (
        content_lane_format_for_coupling("shm_ring")
        == capture_kwargs_for_coupling("shm_ring")["playback_format"]
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


def test_coupling_capture_kwargs_from_env_shm_ring_returns_full_ring_kwargs():
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    kwargs = coupling_capture_kwargs_from_env(
        {"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"}
    )
    assert kwargs == {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": RING_WIRE_FORMAT_WIDE,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT_WIDE,
        "chunksize": RING_CAMILLA_CHUNKSIZE,
        "target_level": RING_CAMILLA_TARGET_LEVEL,
        "queuelimit": RING_CAMILLA_QUEUELIMIT,
        "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
    }


def test_shm_ring_armed_env_emits_ring_capture_device_s32le():
    # SF-2: the shm_ring capture kwargs DO flow through
    # coupling_capture_kwargs_from_env into the product emitters (transport_pipe
    # precedent) — this is deliberate coherence-when-armed. When the ring coupling is
    # set in the env, a household /sound/ save emits a CamillaDSP config whose
    # ALSA capture device is jts_ring_capture + the box's resolved wire format, so
    # the emitted config and the running fan-in daemon name the SAME ring. (That
    # device only RESOLVES once the arm script has installed the ioplug conf.d
    # block; until then the flag stays unset, which is byte-identical to today —
    # see test_coupling_capture_kwargs_from_env_default_is_empty.)
    #
    # Renamed from ...s16le: an undeclared box now resolves S32_LE (the
    # resolver's default flipped WIDE 2026-08-11). S16_LE survives only as the
    # operator's explicit JASPER_FANIN_RING_WIRE_FORMAT rollback pin — see
    # test_resolve_ring_wire_is_the_same_wide_wire_on_every_topology for the
    # per-topology walk that used to carry this test's old name.
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    armed_kwargs = coupling_capture_kwargs_from_env(
        {"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"}
    )
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
    assert "chunksize: 128" in cfg
    assert "target_level: 128" in cfg
    assert "queuelimit: 1" in cfg
    assert "enable_rate_adjust: false" in cfg


def test_live_env_path_reads_coupling_file_fresh_not_os_environ(monkeypatch):
    # BLOCKER 1: the wizard processes (jasper-web /sound/, jasper-correction-web
    # /correction/) do NOT EnvironmentFile= fanin.env, so os.environ lacks
    # JASPER_FANIN_CAMILLA_COUPLING even on an armed box. Reading the token from
    # os.environ would emit a LOOPBACK capture/playback config and silently revert
    # CamillaDSP off the rings. The live-env path (env=None) must resolve the
    # coupling FILE-FRESH from the persisted fanin.env — the same SSOT the daemons
    # and reconciler read — so an armed box's /sound/ save emits the full ring
    # topology. Mirrors the os.environ-stale fix pattern for the voice provider.
    from jasper import fanin_coupling

    # Armed persisted file; os.environ carries NOTHING (the wizard's real shape).
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    monkeypatch.delenv("JASPER_FANIN_CAMILLA_COUPLING", raising=False)

    kwargs = fanin_coupling.coupling_capture_kwargs_from_env()

    # Format values are RING_WIRE_FORMAT_WIDE, not this test's subject: it pins
    # that the COUPLING token is read file-fresh, and the box declares no
    # JASPER_FANIN_RING_WIRE_FORMAT here, so the format axis resolves the
    # resolver's ordinary default.
    assert kwargs == {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": RING_WIRE_FORMAT_WIDE,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT_WIDE,
        "chunksize": RING_CAMILLA_CHUNKSIZE,
        "target_level": RING_CAMILLA_TARGET_LEVEL,
        "queuelimit": RING_CAMILLA_QUEUELIMIT,
        "enable_rate_adjust": RING_CAMILLA_ENABLE_RATE_ADJUST,
    }


def test_live_env_path_loopback_when_file_disarmed_is_byte_identical(monkeypatch):
    # The file-fresh fallback stays fail-SAFE: a disarmed box (loopback fanin.env,
    # or the read fail-safing to loopback) resolves to {} on the live path, byte-
    # identical to today. This is the guard that the Blocker-1 fix does not turn a
    # non-armed box's ordinary /sound/ save into a ring config.
    from jasper import fanin_coupling

    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    monkeypatch.delenv("JASPER_FANIN_CAMILLA_COUPLING", raising=False)

    assert fanin_coupling.coupling_capture_kwargs_from_env() == {}


def test_explicit_env_mapping_stays_authoritative_ignores_file(monkeypatch):
    # An EXPLICIT env mapping (the plan/binder path passes dict(os.environ) right
    # after the reconciler pre-synced it; unit tests pass a controlled env) must
    # NOT read the file — the caller's env wins. Prove a shm_ring fanin.env on disk
    # does not leak into an explicit loopback env. (Also fails loudly if the file
    # reader is consulted at all: it raises here.)
    from jasper import fanin_coupling

    def _boom(*a, **k):
        raise AssertionError("explicit-env path must not read the persisted file")

    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling", _boom
    )

    assert fanin_coupling.coupling_capture_kwargs_from_env({}) == {}
    assert (
        fanin_coupling.coupling_capture_kwargs_from_env(
            {"JASPER_FANIN_CAMILLA_COUPLING": "loopback"}
        )
        == {}
    )


def test_resolve_coupling_unknown_and_old_fifo_fail_safe_to_loopback():
    assert resolve_coupling("fifo") == COUPLING_LOOPBACK
    assert resolve_coupling("pipe") == COUPLING_LOOPBACK
    assert resolve_coupling("disabled") == COUPLING_LOOPBACK


def test_loopback_capture_kwargs_are_empty():
    assert capture_kwargs_for_coupling(None) == {}
    assert capture_kwargs_for_coupling("loopback") == {}
    assert capture_kwargs_for_coupling("garbage") == {}
    assert capture_kwargs_for_coupling("fifo") == {}


def test_loopback_coupling_is_byte_identical_to_no_coupling():
    profile = SoundProfile()
    baseline = emit_sound_config(profile, profile_id="x")
    coupled = emit_sound_config(
        profile,
        profile_id="x",
        **capture_kwargs_for_coupling("loopback"),
    )
    assert coupled == baseline


def test_coupling_capture_kwargs_from_env_default_is_empty():
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    assert coupling_capture_kwargs_from_env({}) == {}
    assert coupling_capture_kwargs_from_env({"JASPER_FANIN_CAMILLA_COUPLING": ""}) == {}
    assert (
        coupling_capture_kwargs_from_env({"JASPER_FANIN_CAMILLA_COUPLING": "loopback"})
        == {}
    )
    assert (
        coupling_capture_kwargs_from_env({"JASPER_FANIN_CAMILLA_COUPLING": "fifo"})
        == {}
    )


def test_member_kwargs_are_pipe_sink_detects_grouped_sink():
    from jasper.fanin_coupling import member_kwargs_are_pipe_sink

    assert member_kwargs_are_pipe_sink(None) is False
    assert member_kwargs_are_pipe_sink({}) is False
    assert (
        member_kwargs_are_pipe_sink(
            {"enable_rate_adjust": True, "playback_pipe_path": None}
        )
        is False
    )
    assert (
        member_kwargs_are_pipe_sink(
            {"enable_rate_adjust": False, "playback_pipe_path": "/run/snapfifo"}
        )
        is True
    )
    assert member_kwargs_are_pipe_sink({"enable_rate_adjust": False}) is True


# --- Ring B (outputd content bridge) vocabulary + coherence (P2) -------------


def test_resolve_outputd_content_bridge_fail_safe_and_tokens():
    from jasper.fanin_coupling import (
        OUTPUTD_CONTENT_BRIDGE_DIRECT,
        OUTPUTD_CONTENT_BRIDGE_SHM_RING,
        resolve_outputd_content_bridge,
    )

    assert resolve_outputd_content_bridge(None) == OUTPUTD_CONTENT_BRIDGE_DIRECT
    assert resolve_outputd_content_bridge("") == OUTPUTD_CONTENT_BRIDGE_DIRECT
    assert resolve_outputd_content_bridge(" DIRECT ") == OUTPUTD_CONTENT_BRIDGE_DIRECT
    assert resolve_outputd_content_bridge("Shm_Ring") == OUTPUTD_CONTENT_BRIDGE_SHM_RING
    # The REMOVED rate_match bridge fail-safes to direct here, matching the Rust
    # daemon's own removed-value arm. The RAW string is what the route policy
    # rejects — see test_audio_runtime_plan.py's refusal test.
    assert resolve_outputd_content_bridge("rate_match") == OUTPUTD_CONTENT_BRIDGE_DIRECT
    assert resolve_outputd_content_bridge("garbage") == OUTPUTD_CONTENT_BRIDGE_DIRECT


def test_outputd_content_bridge_for_coupling_pairs_ring_with_ring():
    from jasper.fanin_coupling import (
        OUTPUTD_CONTENT_BRIDGE_DIRECT,
        OUTPUTD_CONTENT_BRIDGE_SHM_RING,
        outputd_content_bridge_for_coupling,
    )

    assert outputd_content_bridge_for_coupling("shm_ring") == OUTPUTD_CONTENT_BRIDGE_SHM_RING
    assert outputd_content_bridge_for_coupling("loopback") == OUTPUTD_CONTENT_BRIDGE_DIRECT
    assert outputd_content_bridge_for_coupling(None) == OUTPUTD_CONTENT_BRIDGE_DIRECT


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


def test_ring_pair_intent_is_coherent_only_for_matched_ends():
    from jasper.fanin_coupling import ring_pair_intent_is_coherent

    # Coherent: both ring, or neither.
    assert ring_pair_intent_is_coherent("shm_ring", "shm_ring") is True
    assert ring_pair_intent_is_coherent("loopback", "direct") is True
    # PARTIAL flips (strand one ring end) are NOT coherent.
    assert ring_pair_intent_is_coherent("shm_ring", "direct") is False
    assert ring_pair_intent_is_coherent("loopback", "shm_ring") is False
    # A None coupling resolves to loopback -> pairs with direct only.
    assert ring_pair_intent_is_coherent(None, "direct") is True
    assert ring_pair_intent_is_coherent(None, "shm_ring") is False


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
    default flipped WIDE (2026-08-11), no topology resolves S16_LE any more —
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
    kwargs = fc.capture_kwargs_for_coupling(COUPLING_SHM_RING)
    assert kwargs["capture_format"] == "S32_LE"
    assert kwargs["playback_format"] == "S32_LE"
    assert fc.content_lane_format_for_coupling(COUPLING_SHM_RING) == "S32_LE"
