# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The active-leader's camilla#1 program-domain bake — emitter half.

Covers the emitter for distributed-active Stage B (``docs/HANDOFF-distributed-active.md``,
"camilla#1 program bake — verifier exemption"). camilla#1 emits the PROGRAM
domain only — Layer B room correction + Layer C preference EQ + program headroom
— to a ``File`` sink writing the snapserver pipe, with ``enable_rate_adjust:
false`` and NO Layer A (no ``2->N`` split, no per-driver crossover / delay /
gain / limiter). The classifier-side keystone (the File-sink verifier exemption,
the ALSA-sink-still-blocked negative, and emitter<->verifier independence) lives
in ``test_active_speaker_runtime_contract.py``.
"""
from __future__ import annotations

import pytest
import yaml

from jasper.active_speaker import (
    ACTIVE_PROGRAM_BAKE_SOURCE,
    emit_active_speaker_program_bake_config,
)
from jasper.active_speaker.profile import ActiveSpeakerConfigError
from jasper.camilla_config_contract import PeqFilter
from jasper.multiroom.reconcile import SNAPFIFO
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SimpleEq, SoundProfile


def _profile() -> SoundProfile:
    # A non-flat preference profile so Layer C actually emits a band.
    return SoundProfile(enabled=True, simple_eq=SimpleEq(bass_db=6.0))


def _room() -> list[PeqFilter]:
    return [PeqFilter(freq=80.0, q=2.0, gain=-3.0)]


def _doc(**kw) -> dict:
    return yaml.safe_load(emit_active_speaker_program_bake_config(_profile(), **kw))


# --- the bake is a File/pipe sink, never a DAC -------------------------------


def test_playback_is_the_snapserver_file_pipe() -> None:
    playback = _doc()["devices"]["playback"]
    assert playback["type"] == "File"
    assert playback["filename"] == SNAPFIFO
    assert playback["channels"] == 2
    # A File sink must not declare an ALSA device — it is a pipe, not hardware.
    assert "device" not in playback


def test_pipe_is_shaped_like_the_leader_pipe_liveness_check_reads() -> None:
    # The exemption reuses jasper.multiroom.leader_config.playback_is_pipe; pin
    # that the emitted bake satisfies the SAME pipe-shape predicate, so the
    # exemption and the leader-pipe liveness check cannot disagree.
    from jasper.multiroom.leader_config import playback_is_pipe

    text = emit_active_speaker_program_bake_config(_profile())
    assert playback_is_pipe(text, SNAPFIFO) is True


def test_rate_adjust_is_off() -> None:
    # A File backend has no output clock for rate_adjust to steer; on the synced
    # active chain the one rate-tracker is upstream of camilla#1.
    assert _doc()["devices"]["enable_rate_adjust"] is False


def test_volume_ceiling_is_zero_db() -> None:
    assert _doc()["devices"]["volume_limit"] == 0.0


# --- NO Layer A: program domain only -----------------------------------------


def test_no_split_mixer() -> None:
    doc = _doc()
    # The only mixer is the identity master_gain; there is no 2->N driver split.
    assert list(doc["mixers"].keys()) == ["master_gain"]
    assert not any(name.startswith("split_active_") for name in doc["mixers"])


def test_no_per_driver_layer_a_filters() -> None:
    text = emit_active_speaker_program_bake_config(_profile(), room_peqs=_room())
    # Layer A artefacts the driver/baseline emitters produce must be absent:
    # the per-driver crossover, limiter, and the baseline's pre-split headroom
    # gain all belong to camilla#2.
    assert "split_active_" not in text
    assert "active_baseline_headroom" not in text
    assert "crossover" not in text
    assert "limiter" not in text


def test_program_domain_filters_are_present() -> None:
    # Layer B (room PEQ) and Layer C (preference EQ) DO ride the program bus.
    doc = _doc(room_peqs=_room())
    names = set(doc["filters"])
    assert any(n.startswith("room_peq_") for n in names), names
    assert any(n.startswith("sound_") for n in names), names


# --- the distinct DAC-less program-bake provenance marker --------------------


def test_carries_the_distinct_program_bake_source_marker() -> None:
    text = emit_active_speaker_program_bake_config(_profile())
    source_lines = [ln for ln in text.splitlines() if ln.startswith("# Source:")]
    assert source_lines == [f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}"]
    # And it must NOT still carry emit_sound_config's marker (the solo /sound +
    # correction program graphs share that assembly; the bake must be distinct).
    assert "# Source: jasper.sound.camilla_yaml.emit_sound_config" not in text


def test_program_domain_dsp_matches_emit_sound_config_byte_for_byte() -> None:
    # Reusing emit_sound_config's program assembly is the whole point: only the
    # provenance marker differs. Everything else (devices, filters, mixers,
    # pipeline) must be byte-identical to the File/pipe-sink emit_sound_config.
    profile, room = _profile(), _room()
    bake = emit_active_speaker_program_bake_config(profile, room_peqs=room)
    reference = emit_sound_config(
        profile,
        room_peqs=room,
        enable_rate_adjust=False,
        playback_pipe_path=SNAPFIFO,
    )
    normalised = bake.replace(
        f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}",
        "# Source: jasper.sound.camilla_yaml.emit_sound_config",
        1,
    )
    assert normalised == reference


# --- writing -----------------------------------------------------------------


def test_writes_group_readable(tmp_path) -> None:
    out = tmp_path / "grouping_leader.yml"
    emit_active_speaker_program_bake_config(_profile(), out_path=out)
    assert out.exists()
    assert (out.stat().st_mode & 0o777) == 0o640
    assert f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}" in out.read_text()


def test_missing_parent_dir_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        emit_active_speaker_program_bake_config(
            _profile(), out_path=tmp_path / "nope" / "grouping_leader.yml"
        )


def test_marker_restamp_failure_is_loud(monkeypatch) -> None:
    # The bake re-stamps emit_sound_config's `# Source:` line; if that upstream
    # marker ever changes shape the substitution must fail LOUD, never ship a
    # bake the verifier cannot route to the flat program path.
    import jasper.active_speaker.camilla_yaml as mod

    monkeypatch.setattr(
        mod, "emit_sound_config", lambda *a, **k: "devices:\n  samplerate: 48000\n"
    )
    with pytest.raises(ActiveSpeakerConfigError):
        emit_active_speaker_program_bake_config(_profile())
