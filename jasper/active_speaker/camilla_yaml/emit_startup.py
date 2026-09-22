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
    resolve_enable_rate_adjust,
)
from jasper.fanin_coupling import DEFAULT_PLAYBACK_FORMAT

from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset
from .decorate_rear import _mute_unfitted_rear_outputs
from .devices import (
    _assert_ring_playback_width,
    _camilla_latency,
    _finite_float,
    _positive_int,
    _yaml_string,
    forbidden_playback_token,
)
from .document import _atomic_write_text, logger
from .filters import STARTUP_HEADROOM_DB, STARTUP_LIMITER_CLIP_LIMIT_DB, _emit_filter_definitions
from .gates import _assert_tweeter_outputs_protected, _assert_volume_limit
from .pipeline import _emit_pipeline, _emit_split_mixer
from .topology import _output_count


def emit_active_speaker_startup_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_headroom_db: float = STARTUP_HEADROOM_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int | None = None,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
) -> str:
    """Build a muted/protected active-speaker startup template.

    A candidate for later validation; this does not load or reload CamillaDSP.
    The caller must name an explicit active-hardware playback device so the
    stereo outputd lane is never used by accident.
    """

    preset.validate()
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker templates require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize, target_level, queuelimit = _camilla_latency(
        capture_device, playback_device, chunksize, target_level, queuelimit
    )
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    startup_headroom_db = _finite_float(startup_headroom_db, "startup_headroom_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    _assert_volume_limit(volume_limit_db)
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    filter_yaml = _emit_filter_definitions(
        preset,
        startup_headroom_db=startup_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_pipeline(preset)
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker startup config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config
{metadata_yaml}
# DO NOT HAND-EDIT or load automatically. This template is for hardware
# bring-up only: all per-driver outputs start muted, tweeter paths include
# an extra protective high-pass, and the software volume ceiling remains
# non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # L0 emit gate (fail-closed): a startup graph still wires the crossover /
    # protective high-pass on the tweeter channel even though it starts muted, so
    # re-prove that protection before the config can leave the emitter.
    yaml = _mute_unfitted_rear_outputs(yaml, preset)
    _assert_tweeter_outputs_protected(yaml, preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_startup_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
        )
    return yaml
