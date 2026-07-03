# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in -> CamillaDSP coupling selector contracts."""

from __future__ import annotations

import pytest

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE,
    DEFAULT_PLAYBACK_FORMAT,
)
from jasper.fanin_coupling import (
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    COUPLING_TRANSPORT_PIPE,
    DEFAULT_FANIN_CAMILLA_PIPE,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    PIPE_WIRE_FORMAT,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    RING_CAPTURE_DEVICE,
    RING_PLAYBACK_DEVICE,
    RING_WIRE_FORMAT,
    capture_kwargs_for_coupling,
    is_shm_ring_coupling,
    is_transport_pipe_coupling,
    resolve_coupling,
    resolve_pipe_path,
    resolve_outputd_pipe_path,
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
    assert resolve_coupling(" TRANSPORT_PIPE ") == COUPLING_TRANSPORT_PIPE
    assert resolve_coupling("Transport_Pipe") == COUPLING_TRANSPORT_PIPE
    assert resolve_coupling(" SHM_RING ") == COUPLING_SHM_RING
    assert resolve_coupling("Shm_Ring") == COUPLING_SHM_RING


def test_is_shm_ring_coupling_predicate():
    assert is_shm_ring_coupling("shm_ring") is True
    assert is_shm_ring_coupling("loopback") is False
    assert is_shm_ring_coupling("transport_pipe") is False
    assert is_shm_ring_coupling(None) is False
    # A typo must never flip on the ring capture.
    assert is_shm_ring_coupling("ring") is False


def test_shm_ring_kwargs_are_full_ring_topology_capture_and_playback():
    # P2: shm_ring is the END-TO-END ring topology — Ring A capture
    # (jts_ring_capture) AND Ring B playback (jts_ring_playback), both S16_LE. The
    # two ends flip together; a half-ring config (ring capture + ALSA loopback
    # playback) would strand one end, so the emit kwargs MUST carry both devices.
    kwargs = capture_kwargs_for_coupling("shm_ring")
    assert kwargs == {
        "capture_device": RING_CAPTURE_DEVICE,
        "capture_format": RING_WIRE_FORMAT,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT,
    }
    # S16LE, NOT the transport_pipe S32 widening — an SHM ring has no page floor.
    assert RING_WIRE_FORMAT == "S16_LE"
    assert RING_CAPTURE_DEVICE == "jts_ring_capture"
    assert RING_PLAYBACK_DEVICE == "jts_ring_playback"


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
    assert DEFAULT_FANIN_RING_SLOTS == 8
    # A present-but-out-of-range or unparseable value FAILS LOUD (never a
    # silent clamp) — repo doctrine, and it must agree with the Rust daemon,
    # which anyhow::bail!s on the same range.
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
        "capture_format": RING_WIRE_FORMAT,
        "playback_device": RING_PLAYBACK_DEVICE,
        "playback_format": RING_WIRE_FORMAT,
    }


def test_shm_ring_armed_env_emits_ring_capture_device_s16le():
    # SF-2: the shm_ring capture kwargs DO flow through
    # coupling_capture_kwargs_from_env into the product emitters (transport_pipe
    # precedent) — this is deliberate coherence-when-armed. When the lab flag is
    # set in the env, a household /sound/ save emits a CamillaDSP config whose
    # ALSA capture device is jts_ring_capture + S16_LE, so the emitted config and
    # the running fan-in daemon name the SAME ring. (That device only RESOLVES
    # once the arm script has installed the ioplug conf.d block; until then the
    # flag stays unset, which is byte-identical to today — see
    # test_coupling_capture_kwargs_from_env_default_is_empty.)
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    armed_kwargs = coupling_capture_kwargs_from_env(
        {"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"}
    )
    cfg = emit_sound_config(SoundProfile(), profile_id="x", **armed_kwargs)

    capture_block = cfg.split("  capture:\n", 1)[1].split("\n  playback:\n", 1)[0]
    assert "type: Alsa" in capture_block
    assert f'device: "{RING_CAPTURE_DEVICE}"' in capture_block
    assert f"format: {RING_WIRE_FORMAT}" in capture_block
    # It is NOT the transport_pipe RawFile/pipe shape, and NOT the dsnoop default.
    assert "type: RawFile" not in capture_block
    assert 'device: "plug:jasper_capture"' not in capture_block
    # P2: the Ring B playback end flips together with capture — the emit names the
    # ring PLAYBACK device too, so the config is coherent end-to-end (not a
    # half-ring capture-only config that strands outputd).
    playback_block = cfg.split("\n  playback:\n", 1)[1].split("\nfilters:\n", 1)[0]
    assert "type: Alsa" in playback_block
    assert f'device: "{RING_PLAYBACK_DEVICE}"' in playback_block
    assert f"format: {RING_WIRE_FORMAT}" in playback_block
    assert 'device: "outputd_content_playback"' not in playback_block
    # S16_LE native — no S32 widening (an SHM ring has no FIFO page floor).
    assert RING_WIRE_FORMAT == "S16_LE"


def test_resolve_coupling_unknown_and_old_fifo_fail_safe_to_loopback():
    assert resolve_coupling("fifo") == COUPLING_LOOPBACK
    assert resolve_coupling("pipe") == COUPLING_LOOPBACK
    assert resolve_coupling("disabled") == COUPLING_LOOPBACK


def test_is_transport_pipe_coupling_predicate():
    assert is_transport_pipe_coupling("transport_pipe") is True
    assert is_transport_pipe_coupling("loopback") is False
    assert is_transport_pipe_coupling(None) is False
    assert is_transport_pipe_coupling("fifo") is False


def test_resolve_pipe_paths_default_and_override():
    assert resolve_pipe_path(None) == DEFAULT_FANIN_CAMILLA_PIPE
    assert resolve_pipe_path("") == DEFAULT_FANIN_CAMILLA_PIPE
    assert resolve_pipe_path("   ") == DEFAULT_FANIN_CAMILLA_PIPE
    assert resolve_pipe_path("  /run/custom.pipe ") == "/run/custom.pipe"
    assert resolve_outputd_pipe_path(None) == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE
    assert resolve_outputd_pipe_path("") == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE
    assert (
        resolve_outputd_pipe_path(" /run/jasper-outputd/custom.pipe ")
        == "/run/jasper-outputd/custom.pipe"
    )


def test_loopback_capture_kwargs_are_empty():
    assert capture_kwargs_for_coupling(None) == {}
    assert capture_kwargs_for_coupling("loopback") == {}
    assert capture_kwargs_for_coupling("garbage") == {}
    assert capture_kwargs_for_coupling("fifo") == {}


def test_transport_pipe_kwargs_are_dual_pipe_shape():
    kwargs = capture_kwargs_for_coupling("transport_pipe")

    assert kwargs == {
        "capture_pipe_path": DEFAULT_FANIN_CAMILLA_PIPE,
        "playback_pipe_path": DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE,
        "resampler_type": None,
        "resampler_profile": None,
        "enable_rate_adjust": False,
        "transport_paced_pipe": True,
        "playback_format": DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
    }


def test_transport_pipe_kwargs_honor_path_overrides():
    kwargs = capture_kwargs_for_coupling(
        "transport_pipe",
        pipe_path="/run/custom-capture.pipe",
        outputd_pipe_path="/run/custom-outputd.pipe",
    )

    assert kwargs["capture_pipe_path"] == "/run/custom-capture.pipe"
    assert kwargs["playback_pipe_path"] == "/run/custom-outputd.pipe"


def test_transport_pipe_wire_formats_match_camilla_contract():
    assert PIPE_WIRE_FORMAT == DEFAULT_CAPTURE_FORMAT
    assert DEFAULT_PLAYBACK_FORMAT == "S16_LE"
    assert DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT == "S32_LE"


def test_loopback_coupling_is_byte_identical_to_no_coupling():
    profile = SoundProfile()
    baseline = emit_sound_config(profile, profile_id="x")
    coupled = emit_sound_config(
        profile,
        profile_id="x",
        **capture_kwargs_for_coupling("loopback"),
    )
    assert coupled == baseline


def test_transport_pipe_emits_rawfile_capture_file_playback_no_resampler():
    cfg = emit_sound_config(
        SoundProfile(),
        profile_id="x",
        **capture_kwargs_for_coupling("transport_pipe"),
    )

    assert "enable_rate_adjust: false" in cfg
    assert DEFAULT_FANIN_CAMILLA_PIPE in cfg
    assert DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE in cfg
    assert 'device: "plug:jasper_capture"' not in cfg
    assert 'device: "outputd_content_playback"' not in cfg
    assert "type: AsyncSinc" not in cfg
    assert "type: AsyncPoly" not in cfg

    capture_block = cfg.split("  capture:\n", 1)[1].split("\n  playback:\n", 1)[0]
    playback_block = cfg.split("  playback:\n", 1)[1].split("\n\nfilters:\n", 1)[0]
    assert "type: RawFile" in capture_block
    assert f"format: {DEFAULT_CAPTURE_FORMAT}" in capture_block
    assert "type: File" in playback_block
    assert f"format: {DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT}" in playback_block


def test_transport_pipe_does_not_trip_aloop_oscillation_guard():
    from jasper.camilla_config_contract import (
        snd_aloop_rate_adjust_oscillation_reason,
    )

    cfg = emit_sound_config(
        SoundProfile(),
        **capture_kwargs_for_coupling("transport_pipe"),
    )
    assert snd_aloop_rate_adjust_oscillation_reason(cfg) is None


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


def test_coupling_capture_kwargs_from_env_transport_pipe_uses_default_paths():
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    kwargs = coupling_capture_kwargs_from_env(
        {"JASPER_FANIN_CAMILLA_COUPLING": "transport_pipe"}
    )
    assert kwargs["capture_pipe_path"] == DEFAULT_FANIN_CAMILLA_PIPE
    assert kwargs["playback_pipe_path"] == DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE
    assert kwargs["resampler_type"] is None
    assert kwargs["enable_rate_adjust"] is False


def test_coupling_capture_kwargs_from_env_transport_pipe_honors_path_overrides():
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env

    kwargs = coupling_capture_kwargs_from_env(
        {
            "JASPER_FANIN_CAMILLA_COUPLING": "transport_pipe",
            "JASPER_FANIN_CAMILLA_PIPE": "  /run/soak-capture.pipe ",
            OUTPUTD_PIPE_PATH_ENV_VAR: " /run/soak-output.pipe ",
        }
    )

    assert kwargs["capture_pipe_path"] == "/run/soak-capture.pipe"
    assert kwargs["playback_pipe_path"] == "/run/soak-output.pipe"


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
    # rate_match is a separate deferred lab bridge the coupling plane does not
    # own — it fail-safes to direct here (the raw string is what the route policy
    # rejects, see the audio_runtime_plan tests).
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
    # transport_pipe owns a DIFFERENT outputd key (the local content pipe), so it
    # maps to direct on the content-bridge axis.
    assert outputd_content_bridge_for_coupling("transport_pipe") == OUTPUTD_CONTENT_BRIDGE_DIRECT
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


def test_ring_pair_is_coherent_only_for_matched_ends():
    from jasper.fanin_coupling import ring_pair_is_coherent

    # Coherent: both ring, or neither.
    assert ring_pair_is_coherent("shm_ring", "shm_ring") is True
    assert ring_pair_is_coherent("loopback", "direct") is True
    assert ring_pair_is_coherent("transport_pipe", "direct") is True
    # PARTIAL flips (strand one ring end) are NOT coherent.
    assert ring_pair_is_coherent("shm_ring", "direct") is False
    assert ring_pair_is_coherent("loopback", "shm_ring") is False
    assert ring_pair_is_coherent("transport_pipe", "shm_ring") is False
    # A None coupling resolves to loopback -> pairs with direct only.
    assert ring_pair_is_coherent(None, "direct") is True
    assert ring_pair_is_coherent(None, "shm_ring") is False
