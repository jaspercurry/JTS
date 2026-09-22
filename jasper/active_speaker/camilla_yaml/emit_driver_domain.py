# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    DRIVER_DOMAIN_PAIR_TRIM_FILTER,
    resolve_enable_rate_adjust,
)
from jasper.camilla_emit import emit_channel_select_mixer, emit_gain_filter
from jasper.fanin_coupling import DEFAULT_PLAYBACK_FORMAT

from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset
from .decorate_dynamic_bass import _with_dynamic_bass
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
from .filters import (
    BASELINE_LIMITER_CLIP_LIMIT_DB,
    _emit_baseline_driver_definitions,
    _validated_driver_corrections,
)
from .gates import (
    _assert_tweeter_crossover_honours_declared_floor,
    _assert_tweeter_outputs_protected,
    _assert_volume_limit,
)
from .pipeline import _emit_driver_domain_pipeline, _emit_split_mixer
from .topology import _output_count

# Driver-domain-only (active follower) emit: a follower picks ONE inter-speaker
# channel of the leader's corrected stereo program, so the valid selections are
# left / right / a clip-safe mono sum. ``stereo`` (passthrough) is out of scope
# here.
DRIVER_DOMAIN_PROGRAM_CHANNELS = ("left", "right", "mono")


def emit_active_speaker_driver_domain_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    program_channel: str,
    pair_trim_db: float = 0.0,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int | None = None,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
    bass_extension: Mapping[str, Any] | None = None,
) -> str:
    """Build a **driver-domain-only** active-speaker graph for a wireless follower.

    An *endpoint-crossover* graph running only **Layer A** — the ``2->N`` split
    plus each driver's crossover / delay / non-positive gain / soft-clip
    limiter, tweeter band-limited by its crossover high-pass — on a stereo
    program the **leader already corrected**. It emits NO program-domain prefix
    (no ``active_baseline_headroom``, no preference EQ): that domain belongs to
    the leader's bake instance.

    The pipeline is ``channel_select (2->2 pick L/R/mono) -> optional
    pair_balance_trim -> split_active_<way>way (2->N) -> per-driver chain``.
    ``program_channel`` is one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``; the
    channel-select mixer is the shared ``emit_channel_select_mixer`` primitive,
    so a follower and a bonded member spell the pick identically.

    Like the baseline emitter it keeps the 0 dB volume ceiling, per-driver
    limiters and non-positive correction gain, and refuses the stereo outputd
    lane. It does NOT load or reload CamillaDSP. ``corrections`` carries the same
    commissioned per-driver delay/gain/polarity as the solo baseline, so the
    relocated Layer A is the chain the speaker runs solo.
    """

    preset.validate()
    # Same L0 emit gate as the solo baseline: a bonded member's driver domain
    # runs the identical protective chain on the identical drivers.
    _assert_tweeter_crossover_honours_declared_floor(preset)
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker baselines require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    if program_channel not in DRIVER_DOMAIN_PROGRAM_CHANNELS:
        raise ActiveSpeakerConfigError(
            f"program_channel must be one of {DRIVER_DOMAIN_PROGRAM_CHANNELS}, "
            f"not {program_channel!r}"
        )
    pair_trim_db = _finite_float(pair_trim_db, "pair_trim_db")
    if pair_trim_db < 0.0 or pair_trim_db > 120.0:
        raise ActiveSpeakerConfigError("pair_trim_db must be between 0 and 120 dB")
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize, target_level, queuelimit = _camilla_latency(
        capture_device, playback_device, chunksize, target_level, queuelimit
    )
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    _assert_volume_limit(volume_limit_db)
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    safe_corrections = _validated_driver_corrections(preset, corrections)

    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    filter_lines = _emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
    )
    filter_lines.extend(
        emit_gain_filter(DRIVER_DOMAIN_PAIR_TRIM_FILTER, -pair_trim_db)
    )
    filter_yaml = "\n".join(filter_lines)
    # channel_select FIRST (inter-speaker pick), then the intra-speaker split.
    # apply_region_polarity=False: this graph carries polarity through
    # ``safe_corrections``, so the mixer must stay a no-op inverter.
    mixer_yaml = "\n".join((
        emit_channel_select_mixer(program_channel),
        _emit_split_mixer(preset, apply_region_polarity=False),
    ))
    pipeline_yaml = _emit_driver_domain_pipeline(
        preset,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# program_channel={program_channel}",
        f"# pair_trim_db={pair_trim_db:.3f}",
    ]
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker driver-domain config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_driver_domain_config
{metadata_yaml}
# This is a wireless follower's driver-domain-only Layer-A graph: it picks one
# inter-speaker channel of the leader's already-corrected stereo program, then
# runs the per-driver crossover/limiter chain. There is no program-domain
# headroom or preference EQ (the leader baked Layer B/C); outputs are not
# startup-muted, per-driver correction gain is non-positive, and the software
# volume ceiling remains non-positive.

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

    # L0 emit gate (fail-closed): the follower runs Layer A on the leader's
    # corrected program, so its tweeter output must still carry the crossover /
    # protective high-pass.
    _assert_tweeter_outputs_protected(yaml, preset)

    yaml = _mute_unfitted_rear_outputs(_with_dynamic_bass(yaml, preset, bass_extension), preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_driver_domain_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d program_channel=%s",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            program_channel,
        )
    return yaml
