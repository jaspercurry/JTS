# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""fan-in -> CamillaDSP coupling selector contracts."""

from __future__ import annotations

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT,
    DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE,
    DEFAULT_PLAYBACK_FORMAT,
)
from jasper.fanin_coupling import (
    COUPLING_LOOPBACK,
    COUPLING_TRANSPORT_PIPE,
    DEFAULT_FANIN_CAMILLA_PIPE,
    PIPE_WIRE_FORMAT,
    OUTPUTD_PIPE_PATH_ENV_VAR,
    capture_kwargs_for_coupling,
    is_transport_pipe_coupling,
    resolve_coupling,
    resolve_pipe_path,
    resolve_outputd_pipe_path,
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
