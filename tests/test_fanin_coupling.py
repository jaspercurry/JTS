# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the fan-in → CamillaDSP coupling selector and its byte-identical
loopback default.

The load-bearing claim: with ``JASPER_FANIN_CAMILLA_COUPLING`` unset or
``loopback``, the emitted CamillaDSP capture block is **byte-for-byte** the same
as a call that never touched the coupling helper. With ``fifo`` it flips to a
File capture + async resampler + rate-adjust (the lean-lane File-capture shape).
"""
from __future__ import annotations

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
)
from jasper.fanin_coupling import (
    COUPLING_FIFO,
    COUPLING_LOOPBACK,
    DEFAULT_FANIN_CAMILLA_FIFO,
    FIFO_WIRE_FORMAT,
    capture_kwargs_for_coupling,
    is_fifo_coupling,
    resolve_coupling,
    resolve_fifo_path,
)
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile


# ---- resolve_coupling: fail-safe normalization ----------------------------


def test_resolve_coupling_defaults_to_loopback():
    assert resolve_coupling(None) == COUPLING_LOOPBACK
    assert resolve_coupling("") == COUPLING_LOOPBACK
    assert resolve_coupling("   ") == COUPLING_LOOPBACK


def test_resolve_coupling_accepts_both_transports_case_insensitive():
    assert resolve_coupling("loopback") == COUPLING_LOOPBACK
    assert resolve_coupling(" FIFO ") == COUPLING_FIFO
    assert resolve_coupling("FiFo") == COUPLING_FIFO


def test_resolve_coupling_unknown_value_fails_safe_to_loopback():
    # A typo must never silently flip the shared realtime capture; fail safe.
    assert resolve_coupling("fifoo") == COUPLING_LOOPBACK
    assert resolve_coupling("pipe") == COUPLING_LOOPBACK
    assert resolve_coupling("disabled") == COUPLING_LOOPBACK


def test_is_fifo_coupling_predicate():
    assert is_fifo_coupling("fifo") is True
    assert is_fifo_coupling("loopback") is False
    assert is_fifo_coupling(None) is False
    assert is_fifo_coupling("garbage") is False


# ---- resolve_fifo_path -----------------------------------------------------


def test_resolve_fifo_path_default_and_override():
    assert resolve_fifo_path(None) == DEFAULT_FANIN_CAMILLA_FIFO
    assert resolve_fifo_path("") == DEFAULT_FANIN_CAMILLA_FIFO
    assert resolve_fifo_path("   ") == DEFAULT_FANIN_CAMILLA_FIFO
    assert resolve_fifo_path("  /run/custom.pipe ") == "/run/custom.pipe"


# ---- capture_kwargs_for_coupling ------------------------------------------


def test_loopback_capture_kwargs_are_empty():
    # The empty-dict contract: loopback adds NOTHING, so every existing caller
    # is unchanged when the flag is unset.
    assert capture_kwargs_for_coupling(None) == {}
    assert capture_kwargs_for_coupling("loopback") == {}
    assert capture_kwargs_for_coupling("garbage") == {}


def test_fifo_capture_kwargs_are_file_capture_shape():
    kwargs = capture_kwargs_for_coupling("fifo")
    assert kwargs["capture_pipe_path"] == DEFAULT_FANIN_CAMILLA_FIFO
    assert kwargs["resampler_type"] == DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE
    assert kwargs["enable_rate_adjust"] is True


def test_fifo_capture_kwargs_honor_path_override():
    kwargs = capture_kwargs_for_coupling("fifo", fifo_path="/run/custom.pipe")
    assert kwargs["capture_pipe_path"] == "/run/custom.pipe"


def test_fifo_wire_format_matches_shared_capture_format():
    # The format-split invariant: fan-in widens S16->S32 so the pipe carries the
    # same width as the shared ALSA capture (S32_LE). If these ever diverge the
    # File capture would misread the pipe.
    assert FIFO_WIRE_FORMAT == DEFAULT_CAPTURE_FORMAT


# ---- byte-identical default proof (the safety contract) --------------------


def test_loopback_coupling_is_byte_identical_to_no_coupling():
    profile = SoundProfile()
    # Baseline: a plain emit that knows nothing about the coupling helper.
    baseline = emit_sound_config(profile, profile_id="x")
    # Coupling=loopback: apply the resolved kwargs (which are {}), so the result
    # MUST be byte-for-byte identical.
    loopback_kwargs = capture_kwargs_for_coupling("loopback")
    coupled = emit_sound_config(profile, profile_id="x", **loopback_kwargs)
    assert coupled == baseline


def test_loopback_default_unset_flag_is_byte_identical():
    profile = SoundProfile()
    baseline = emit_sound_config(profile, profile_id="x")
    unset_kwargs = capture_kwargs_for_coupling(None)
    coupled = emit_sound_config(profile, profile_id="x", **unset_kwargs)
    assert coupled == baseline


def test_fifo_coupling_emits_file_capture_and_resampler():
    profile = SoundProfile()
    fifo_kwargs = capture_kwargs_for_coupling("fifo")
    cfg = emit_sound_config(profile, profile_id="x", **fifo_kwargs)
    # File capture pointed at the fan-in pipe, NOT the dsnoop ALSA device.
    assert "type: File" in cfg
    assert DEFAULT_FANIN_CAMILLA_FIFO in cfg
    assert 'device: "plug:jasper_capture"' not in cfg
    # Async resampler + rate-adjust on, the clockless-File-capture safe shape.
    assert f"type: {DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE}" in cfg
    assert "enable_rate_adjust: true" in cfg
    # Capture format is the shared S32_LE wire format.
    assert f"format: {FIFO_WIRE_FORMAT}" in cfg


def test_fifo_coupling_capture_is_a_valid_file_capture_per_emitter_guards():
    # emit_sound_config raises if the File-capture invariants are violated;
    # capture_kwargs_for_coupling MUST always satisfy them. A passing emit is
    # the proof the kwargs are self-consistent.
    profile = SoundProfile()
    fifo_kwargs = capture_kwargs_for_coupling("fifo")
    # Should not raise.
    emit_sound_config(profile, **fifo_kwargs)


def test_fifo_coupling_does_not_trip_oscillation_guard():
    # Spec §3: the snd-aloop rate_adjust + async-resampler oscillation guard
    # EXEMPTS File captures (a clockless File capture REQUIRES the async
    # resampler + rate-adjust). The fifo coupling is a File capture, so the
    # guard must return None for it.
    from jasper.camilla_config_contract import (
        snd_aloop_rate_adjust_oscillation_reason,
    )

    fifo_kwargs = capture_kwargs_for_coupling("fifo")
    cfg = emit_sound_config(SoundProfile(), **fifo_kwargs)
    assert snd_aloop_rate_adjust_oscillation_reason(cfg) is None
