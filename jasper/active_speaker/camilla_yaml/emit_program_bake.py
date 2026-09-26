# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
)
from jasper.multiroom.snapfifo import SNAPFIFO
from jasper.biquad import PeqFilter
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile

from ..profile import ActiveSpeakerConfigError
from .document import _atomic_write_text, logger

# The active-LEADER's camilla#1 program-domain bake: ONLY the program domain
# (Layer B + Layer C + program headroom) to a ``File`` sink writing the
# snapserver pipe; Layer A lives in camilla#2. The runtime verifier keys on this
# marker to recognise a DAC-less program bake, but the exemption's SAFETY keys
# on ``devices.playback.type == File``, never on this string.
ACTIVE_PROGRAM_BAKE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config"
)


# The exact ``# Source:`` line emit_sound_config stamps. We rewrite it to the
# bake's own marker, so the substitution is a 1:1 swap; assert it fired rather
# than silently shipping the wrong provenance if that emitter's header changes.
_SOUND_SOURCE_LINE = "# Source: jasper.sound.camilla_yaml.emit_sound_config"

_PROGRAM_BAKE_SOURCE_LINE = f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}"


def emit_active_speaker_program_bake_config(
    profile: SoundProfile,
    *,
    room_peqs: list[PeqFilter] | None = None,
    output_trim_db: float = 0.0,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    profile_id: str | None = None,
) -> str:
    """Build the active-LEADER's **program-domain-only** camilla#1 bake.

    The **program** half of a leader's split DSP: Layer B room correction +
    Layer C preference EQ + program headroom, written to a ``File`` sink feeding
    the snapserver pipe so the followers receive a corrected stereo wire. The
    **driver** half (Layer A) lives in camilla#2 and is deliberately absent.

    The program assembly reuses :func:`jasper.sound.camilla_yaml.emit_sound_config` with a
    ``File``/pipe sink, so the baked correction is byte-for-byte the program
    graph the speaker already ships; only the ``# Source:`` marker differs.

    Safety is BY CONSTRUCTION: the playback is a pipe, not a DAC, so no driver
    can be over-driven regardless of the saved topology — and the runtime
    verifier's matching exemption keys on ``devices.playback.type == File``,
    never on the marker, so an ALSA-sink program graph reaching the DAC stays
    blocked under a roleful topology.

    This does NOT load or reload CamillaDSP and does NOT wire camilla#1 into the
    reconciler. ``out_path`` writes the YAML group-readably (0640).
    """

    program_yaml = emit_sound_config(
        profile,
        room_peqs=room_peqs,
        capture_device=capture_device,
        capture_format=capture_format,
        sample_rate=sample_rate,
        chunksize=chunksize,
        target_level=target_level,
        volume_limit_db=volume_limit_db,
        profile_id=profile_id,
        output_trim_db=output_trim_db,
        playback_pipe_path=SNAPFIFO,
    )

    # Re-stamp provenance so the bake is distinguishable from the solo /sound +
    # correction program graphs that share emit_sound_config's assembly. Fail
    # loud if the upstream marker changes shape: a silent miss would ship a bake
    # the verifier cannot route to the flat program path.
    if _SOUND_SOURCE_LINE not in program_yaml:
        raise ActiveSpeakerConfigError(
            "program bake could not re-stamp the source marker: "
            "emit_sound_config no longer emits the expected '# Source:' line"
        )
    yaml = program_yaml.replace(_SOUND_SOURCE_LINE, _PROGRAM_BAKE_SOURCE_LINE, 1)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_program_bake_config_written path=%s pipe=%s "
            "room_peqs=%d output_trim=%.3f",
            out_path,
            SNAPFIFO,
            len(room_peqs or []),
            output_trim_db,
        )
    return yaml
