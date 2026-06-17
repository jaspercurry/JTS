"""Emit commissioning-safe CamillaDSP templates for active speakers.

This module is intentionally side-effect-light: it can build or write a
candidate YAML file, but it does not ask CamillaDSP to load it. Hardware
activation belongs behind later channel-identity and path-safety gates.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from pathlib import Path

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CHUNKSIZE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
    DEFAULT_VOLUME_LIMIT_DB,
)
from jasper.camilla_emit import (
    emit_gain_filter,
    emit_linkwitz_riley,
    emit_mixer,
    fmt,
)

from .profile import (
    ADJACENT_PAIRS_BY_WAY,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    required_driver_roles,
)

logger = logging.getLogger(__name__)

ACTIVE_STARTUP_CONFIG_NAME = "active_speaker_startup.yml"
STARTUP_HEADROOM_DB = 40.0
STARTUP_MUTE_GAIN_DB = -120.0
STARTUP_LIMITER_CLIP_LIMIT_DB = -12.0
BASELINE_HEADROOM_DB = 12.0
BASELINE_LIMITER_CLIP_LIMIT_DB = -1.0
PROTECTIVE_TWEETER_HP_MULTIPLIER = 2.0
FORBIDDEN_ACTIVE_PLAYBACK_TOKENS = (
    DEFAULT_PLAYBACK_DEVICE,
    "jasper_out",
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def _name_token(value: str) -> str:
    token = _SAFE_NAME_RE.sub("_", value).strip("_").lower()
    return token or "unnamed"


def _yaml_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActiveSpeakerConfigError(f"{field_name} is required")
    out = value.strip()
    if any(ch in out for ch in ('"', "\n", "\r")):
        raise ActiveSpeakerConfigError(f"{field_name} contains unsafe YAML characters")
    return out


def _forbidden_playback_token(playback_device: str) -> str | None:
    lowered = playback_device.lower()
    for token in FORBIDDEN_ACTIVE_PLAYBACK_TOKENS:
        if token.lower() in lowered:
            return token
    return None


def _finite_float(value: float, field_name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError(f"{field_name} must be numeric") from e
    if not math.isfinite(out):
        raise ActiveSpeakerConfigError(f"{field_name} must be finite")
    return out


def _positive_int(value: int, field_name: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError(f"{field_name} must be an integer") from e
    if out <= 0:
        raise ActiveSpeakerConfigError(f"{field_name} must be positive")
    return out


def _emit_delay_filter(name: str, delay_ms: float = 0.0) -> list[str]:
    return [
        f"  {name}:",
        "    type: Delay",
        "    parameters:",
        f"      delay: {fmt(delay_ms)}",
        "      unit: ms",
    ]


def _emit_limiter_filter(
    name: str,
    *,
    clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    soft_clip: bool = True,
) -> list[str]:
    soft_clip_s = "true" if soft_clip else "false"
    return [
        f"  {name}:",
        "    type: Limiter",
        "    parameters:",
        f"      soft_clip: {soft_clip_s}",
        f"      clip_limit: {fmt(clip_limit_db)}",
    ]


def _ordered_regions(preset: ActiveSpeakerPreset) -> list[CrossoverRegion]:
    by_pair = {
        (region.lower_driver, region.upper_driver): region
        for region in preset.crossover_regions
    }
    return [by_pair[pair] for pair in ADJACENT_PAIRS_BY_WAY[preset.way_count]]


def _role_polarity(preset: ActiveSpeakerPreset) -> dict[str, bool]:
    polarity: dict[str, bool] = {}
    for region in preset.crossover_regions:
        for role, value in (
            (region.lower_driver, region.lower_polarity),
            (region.upper_driver, region.upper_polarity),
        ):
            inverted = value == "inverted"
            previous = polarity.setdefault(role, inverted)
            if previous != inverted:
                raise ActiveSpeakerConfigError(
                    f"driver {role} has inconsistent polarity across crossover regions"
                )
    for role in required_driver_roles(preset.way_count):
        polarity.setdefault(role, False)
    return polarity


def _channels_for_role(preset: ActiveSpeakerPreset, role: str) -> list[int]:
    return sorted(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == role
    )


def _mixer_sources(
    side: str,
    layout: str,
    *,
    inverted: bool,
) -> list[tuple[int, float, bool]]:
    if layout == "stereo":
        if side == "left":
            return [(0, 0.0, inverted)]
        if side == "right":
            return [(1, 0.0, inverted)]
        raise ActiveSpeakerConfigError(f"unsupported stereo side {side!r}")
    if layout == "mono":
        # Safe mono sum. Two -6 dB feeds keep correlated L+R from gaining.
        return [(0, -6.0, inverted), (1, -6.0, inverted)]
    raise ActiveSpeakerConfigError(f"unsupported layout {layout!r}")


def _emit_split_mixer(preset: ActiveSpeakerPreset) -> str:
    polarity = _role_polarity(preset)
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = len(outputs)
    # Active-speaker policy: build the (dest -> L/R-sum sources) map from
    # the preset's driver layout + per-driver polarity. The YAML spelling
    # is the shared emit_mixer; this routing is the assembly concern.
    mapping = [
        (
            output.index,
            _mixer_sources(
                output.side,
                preset.channel_map.layout,
                inverted=polarity[output.driver_role],
            ),
        )
        for output in outputs
    ]
    return emit_mixer(
        f"split_active_{preset.way_count}way",
        channels_in=2,
        channels_out=output_count,
        mapping=mapping,
        description=(
            f"{preset.channel_map.layout} source -> "
            f"{output_count} protected active outputs"
        ),
        labels=[output.label for output in outputs],
    )


def _crossover_filter_name(
    role: str,
    region: CrossoverRegion,
    *,
    highpass: bool,
) -> str:
    suffix = "hp" if highpass else "lp"
    return f"as_{_name_token(role)}_{_name_token(region.id)}_{suffix}"


def _driver_delay_name(role: str) -> str:
    return f"as_{_name_token(role)}_delay"


def _driver_mute_name(role: str) -> str:
    return f"as_{_name_token(role)}_startup_mute"


def _driver_limiter_name(role: str) -> str:
    return f"as_{_name_token(role)}_startup_limiter"


def _driver_baseline_gain_name(role: str) -> str:
    return f"as_{_name_token(role)}_baseline_gain"


def _driver_baseline_limiter_name(role: str) -> str:
    return f"as_{_name_token(role)}_baseline_limiter"


def _protective_tweeter_hp_name(role: str) -> str:
    return f"as_{_name_token(role)}_protective_hp"


def _protective_tweeter_hp_frequency(
    preset: ActiveSpeakerPreset,
    role: str,
) -> float | None:
    if role != "tweeter":
        return None
    fc_values = [
        region.fc_hz
        for region in preset.crossover_regions
        if region.upper_driver == "tweeter"
    ]
    if not fc_values:
        return None
    return max(fc_values) * PROTECTIVE_TWEETER_HP_MULTIPLIER


def _driver_filter_chain(preset: ActiveSpeakerPreset, role: str) -> list[str]:
    names: list[str] = []
    protective_freq = _protective_tweeter_hp_frequency(preset, role)
    if protective_freq is not None:
        names.append(_protective_tweeter_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    names.append(_driver_delay_name(role))
    names.append(_driver_mute_name(role))
    names.append(_driver_limiter_name(role))
    return names


def _driver_baseline_filter_chain(preset: ActiveSpeakerPreset, role: str) -> list[str]:
    names: list[str] = []
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    names.append(_driver_delay_name(role))
    names.append(_driver_baseline_gain_name(role))
    names.append(_driver_baseline_limiter_name(role))
    return names


def _emit_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_startup_headroom", -startup_headroom_db))
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    for role in required_driver_roles(preset.way_count):
        protective_freq = _protective_tweeter_hp_frequency(preset, role)
        if protective_freq is not None:
            lines.extend(emit_linkwitz_riley(
                _protective_tweeter_hp_name(role),
                highpass=True,
                freq_hz=protective_freq,
                order=4,
            ))
        lines.extend(_emit_delay_filter(_driver_delay_name(role)))
        lines.extend(emit_gain_filter(
            _driver_mute_name(role),
            STARTUP_MUTE_GAIN_DB,
            mute=True,
        ))
        lines.extend(_emit_limiter_filter(
            _driver_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    return "\n".join(lines)


def _correction_value(
    corrections: dict[str, dict[str, float | bool]],
    role: str,
    field: str,
    default: float,
) -> float:
    value = corrections.get(role, {}).get(field)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _correction_bool(
    corrections: dict[str, dict[str, float | bool]],
    role: str,
    field: str,
) -> bool:
    return bool(corrections.get(role, {}).get(field))


def _emit_baseline_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    baseline_headroom_db: float,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_baseline_headroom", -baseline_headroom_db))
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    for role in required_driver_roles(preset.way_count):
        delay_ms = _correction_value(corrections, role, "delay_ms", 0.0)
        gain_db = _correction_value(corrections, role, "gain_db", 0.0)
        inverted = _correction_bool(corrections, role, "inverted")
        lines.extend(_emit_delay_filter(_driver_delay_name(role), delay_ms=delay_ms))
        lines.extend(emit_gain_filter(
            _driver_baseline_gain_name(role),
            gain_db,
            inverted=inverted,
        ))
        lines.extend(_emit_limiter_filter(
            _driver_baseline_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    return "\n".join(lines)


def _emit_pipeline(preset: ActiveSpeakerPreset) -> str:
    lines = [
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_startup_headroom]",
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ]
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(_driver_filter_chain(preset, role))
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    return "\n".join(lines)


def _emit_baseline_pipeline(preset: ActiveSpeakerPreset) -> str:
    lines = [
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_baseline_headroom]",
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ]
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(_driver_baseline_filter_chain(preset, role))
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    return "\n".join(lines)


def _output_count(preset: ActiveSpeakerPreset) -> int:
    return max(output.index for output in preset.channel_map.outputs) + 1


def emit_active_speaker_startup_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_headroom_db: float = STARTUP_HEADROOM_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Build a muted/protected active-speaker startup template.

    The returned YAML is a candidate for later validation and manual
    inspection. This function deliberately does not load or reload
    CamillaDSP. The caller must also provide an explicit active-hardware
    playback device so the current stereo outputd lane is never used by
    accident.
    """

    preset.validate()
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker templates require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    startup_headroom_db = _finite_float(startup_headroom_db, "startup_headroom_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    output_count = _output_count(preset)
    filter_yaml = _emit_filter_definitions(
        preset,
        startup_headroom_db=startup_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_pipeline(preset)
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker startup config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config
{metadata_yaml}
# DO NOT HAND-EDIT or load automatically. This template is for hardware
# bring-up only: all per-driver outputs start muted, tweeter paths include
# an extra protective high-pass, and the software volume ceiling remains 0 dB.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: true
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


def output_commission_mute_name(index: int) -> str:
    """The per-physical-output commission-mute filter name for ``index``.

    Public because the protected-staging software guard
    (``jasper.active_speaker.staging``) references these by index to prove a
    driver's output is muted — the name is a cross-module contract, so the
    emitter owns it and the guard reads it rather than re-deriving the spelling.
    """
    return f"as_out{index}_commission_mute"


def audible_outputs_for_role(preset: ActiveSpeakerPreset, role: str) -> frozenset[int]:
    """All physical output indices carrying ``role`` (both sides for stereo).

    A convenience for callers isolating a whole role (e.g. a mono test, or the
    summed check). Single-output isolation is just ``{index}``.
    """
    return frozenset(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == role
    )


def _commissioning_driver_filter_chain(
    preset: ActiveSpeakerPreset,
    role: str,
) -> list[str]:
    """The startup chain minus the per-role mute.

    Commissioning isolates one *physical output* (not a whole role — a stereo
    woofer pair shares a role), so the role-level startup mute is dropped and a
    per-output mute layer is applied in the pipeline instead. Everything that
    protects a driver — protective tweeter high-pass, crossover, delay,
    limiter — is preserved exactly as the running graph has it.
    """
    role_mute = _driver_mute_name(role)
    return [name for name in _driver_filter_chain(preset, role) if name != role_mute]


def _emit_commissioning_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
    audible_outputs: frozenset[int],
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_startup_headroom", -startup_headroom_db))
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    for role in required_driver_roles(preset.way_count):
        protective_freq = _protective_tweeter_hp_frequency(preset, role)
        if protective_freq is not None:
            lines.extend(emit_linkwitz_riley(
                _protective_tweeter_hp_name(role),
                highpass=True,
                freq_hz=protective_freq,
                order=4,
            ))
        lines.extend(_emit_delay_filter(_driver_delay_name(role)))
        lines.extend(_emit_limiter_filter(
            _driver_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    # Per-output commissioning mute: only audible outputs pass; the rest are
    # hard-muted so exactly one physical driver is excited through the real
    # graph. Default (empty set) is fully muted — the safe initial load.
    # Audible outputs carry ``audible_gain_db`` as their per-output gain — the
    # Stage-5 ramp variable. It defaults to the silent mute floor
    # (``STARTUP_MUTE_GAIN_DB``), so an un-ramped commission load arms the target
    # at {gain: -120, mute: off} (silent); the Stage-5 gate raises it within the
    # commissioning level envelope. Muted outputs always stay at the -120 dB
    # mute floor regardless of ``audible_gain_db``.
    for index in range(_output_count(preset)):
        is_audible = index in audible_outputs
        lines.extend(emit_gain_filter(
            output_commission_mute_name(index),
            audible_gain_db if is_audible else STARTUP_MUTE_GAIN_DB,
            mute=not is_audible,
        ))
    return "\n".join(lines)


def _emit_commissioning_pipeline(preset: ActiveSpeakerPreset) -> str:
    lines = [
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_startup_headroom]",
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ]
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(_commissioning_driver_filter_chain(preset, role))
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    for index in range(_output_count(preset)):
        lines.extend([
            "  - type: Filter",
            f"    channels: [{index}]",
            f"    names: [{output_commission_mute_name(index)}]",
        ])
    return "\n".join(lines)


def emit_active_speaker_commissioning_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    audible_outputs: frozenset[int] | set[int] | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_headroom_db: float = STARTUP_HEADROOM_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Build the **production** active-speaker graph with a per-output mask.

    This is the single-audio-path commissioning config: the same protected
    graph the speaker runs (volume ceiling 0 dB, startup headroom, protective
    tweeter high-pass, per-driver limiters), but with each physical output
    individually mutable. Loading it with ``audible_outputs={k}`` excites
    exactly one driver through its real crossover/limiter chain; an empty set
    is fully muted (the safe initial load); the full set is every driver live
    for the summed check.

    It replaces the old direct-DAC diagnostic bypass: validation now happens on
    the production path, so the commissioned config *is* what gets frozen as the
    durable profile. Like the other emitters it does not load or reload
    CamillaDSP, and it refuses the existing stereo outputd lane as a playback
    device.
    """

    preset.validate()
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker templates require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    startup_headroom_db = _finite_float(startup_headroom_db, "startup_headroom_db")
    limiter_clip_limit_db = _finite_float(limiter_clip_limit_db, "limiter_clip_limit_db")
    audible_gain_db = _finite_float(audible_gain_db, "audible_gain_db")
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )
    # Structural bound only: the per-output audible gain is an attenuation, so it
    # never exceeds the 0 dB ceiling and never drops below the -120 dB mute floor.
    # The tighter commissioning *level envelope* (the audible test range and the
    # per-step ramp limit) is owned by the Stage-5 ramp gate, not this primitive.
    if audible_gain_db < STARTUP_MUTE_GAIN_DB or audible_gain_db > 0:
        raise ActiveSpeakerConfigError(
            f"audible_gain_db must be between {STARTUP_MUTE_GAIN_DB:.0f} and 0 dB"
        )

    output_count = _output_count(preset)
    audible: frozenset[int] = frozenset(audible_outputs or ())
    for index in audible:
        if not isinstance(index, int) or not 0 <= index < output_count:
            raise ActiveSpeakerConfigError(
                f"audible_outputs index {index!r} out of range for "
                f"{output_count} outputs"
            )

    filter_yaml = _emit_commissioning_filter_definitions(
        preset,
        startup_headroom_db=startup_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
        audible_outputs=audible,
        audible_gain_db=audible_gain_db,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_commissioning_pipeline(preset)
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# audible_outputs={sorted(audible)}",
        f"# audible_gain_db={fmt(audible_gain_db)}",
    ]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker commissioning config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_commissioning_config
{metadata_yaml}
# DO NOT HAND-EDIT. Single-audio-path bring-up: the production graph with a
# per-output mute mask so one driver at a time is tested through its real
# crossover/limiter chain. Tweeter paths keep an extra protective high-pass and
# the software volume ceiling stays 0 dB.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: true
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

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_commissioning_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d audible=%s",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            sorted(audible),
        )
    return yaml


def emit_active_speaker_baseline_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    baseline_headroom_db: float = BASELINE_HEADROOM_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Build an accepted active-speaker baseline candidate.

    Unlike the startup template, this YAML is not muted. It still preserves
    the JTS 0 dB volume ceiling, includes conservative headroom, keeps
    per-driver limiters, and refuses positive per-driver correction gain.
    Callers own the acceptance evidence and explicit CamillaDSP apply step.
    """

    preset.validate()
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker baselines require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    baseline_headroom_db = _finite_float(baseline_headroom_db, "baseline_headroom_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if baseline_headroom_db < 0 or baseline_headroom_db > 40:
        raise ActiveSpeakerConfigError("baseline_headroom_db must be between 0 and 40")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    safe_corrections: dict[str, dict[str, float | bool]] = {}
    for role, values in (corrections or {}).items():
        if role not in required_driver_roles(preset.way_count):
            continue
        if not isinstance(values, dict):
            continue
        gain_db = _correction_value({role: values}, role, "gain_db", 0.0)
        delay_ms = _correction_value({role: values}, role, "delay_ms", 0.0)
        if gain_db > 0:
            raise ActiveSpeakerConfigError(
                f"baseline correction gain for {role} must not be positive"
            )
        if delay_ms < 0 or delay_ms > 20:
            raise ActiveSpeakerConfigError(
                f"baseline delay for {role} must be between 0 and 20 ms"
            )
        safe_corrections[role] = {
            "gain_db": gain_db,
            "delay_ms": delay_ms,
            "inverted": bool(values.get("inverted")),
        }

    output_count = _output_count(preset)
    filter_yaml = _emit_baseline_filter_definitions(
        preset,
        baseline_headroom_db=baseline_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_baseline_pipeline(preset)
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker baseline config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config
{metadata_yaml}
# This is a candidate speaker baseline: crossover filters are active, outputs
# are not startup-muted, per-driver correction gain is non-positive, and the
# software volume ceiling remains 0 dB.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: true
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

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_baseline_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
        )
    return yaml


def _atomic_write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        f.write(text)
        tmp_name = f.name
    os.replace(tmp_name, path)


def active_speaker_startup_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / ACTIVE_STARTUP_CONFIG_NAME
