# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Mapping, Sequence

import yaml

from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    resolve_enable_rate_adjust,
)
from jasper.fanin_coupling import DEFAULT_PLAYBACK_FORMAT
from jasper.log_event import log_event

from ..camilla_names import output_commission_mute_name
from ..graph_safety import TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ
from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles

if TYPE_CHECKING:
    from ..branch_chain import CrossoverSection
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
    APPLIED_RESPONSE_FILTER_MODE,
    COMMISSIONING_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    _emit_commissioning_filter_definitions,
    _program_protection_name,
)
from .gates import (
    PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE,
    _assert_measurement_delays_bound,
    _assert_program_graph_proven,
    _assert_tweeter_crossover_hp_satisfies_floor,
    _assert_tweeter_outputs_protected,
    _assert_volume_limit,
    _validate_program_role_channels,
)
from .pipeline import (
    _emit_commissioning_pipeline,
    _emit_role_routed_mixer,
    _validated_measurement_trims,
    program_channel_count,
)
from .topology import _output_count

_PROGRAM_PROTECTION_RE = re.compile(r"^as_(woofer|tweeter)_program_protection_([0-9]+)$")


def protected_neutral_program_origin(
    raw: str | Mapping[str, Any],
) -> bool | None:
    """Classify this emitter's namespace: exact, partial, or unrelated."""
    try:
        config = yaml.safe_load(raw) if isinstance(raw, str) else raw
    except yaml.YAMLError:
        return False if "program_protection_" in str(raw) else None
    if not isinstance(config, Mapping):
        return None
    filters, pipeline = config.get("filters"), config.get("pipeline")
    mixers, devices = config.get("mixers"), config.get("devices")
    owns_namespace = isinstance(filters, Mapping) and any(
        _PROGRAM_PROTECTION_RE.fullmatch(str(name)) for name in filters
    )
    if not (
        isinstance(filters, Mapping) and isinstance(pipeline, list)
        and isinstance(mixers, Mapping) and isinstance(devices, Mapping)
    ):
        return False if owns_namespace else None
    if not owns_namespace:
        return None
    capture, playback = devices.get("capture", {}), devices.get("playback", {})
    if not isinstance(capture, Mapping) or not isinstance(playback, Mapping):
        return False
    output_count = playback.get("channels")
    if not isinstance(output_count, int) or output_count < 2 or capture.get("channels") != 2:
        return False
    roles = ("woofer", "tweeter")
    protections: dict[str, list[tuple[int, str]]] = {role: [] for role in roles}
    for name in filters:
        match = _PROGRAM_PROTECTION_RE.fullmatch(str(name))
        if match:
            protections[match.group(1)].append((int(match.group(2)), str(name)))
    for items in protections.values():
        if not items or sorted(index for index, _ in items) != list(range(len(items))):
            return False
    limiters = {role: f"as_{role}_startup_limiter" for role in roles}
    mutes = [output_commission_mute_name(index) for index in range(output_count)]
    protection_names = {name for items in protections.values() for _, name in items}
    if set(filters) != {
        "active_startup_headroom", *protection_names, *limiters.values(), *mutes,
    }:
        return False
    gain = {"type": "Gain", "parameters": {"gain": 0.0, "inverted": False, "mute": False}}
    limiter = {"type": "Limiter", "parameters": {"soft_clip": True, "clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB}}
    if (filters["active_startup_headroom"] != gain
            or any(filters[name] != gain for name in mutes)
            or any(filters[name] != limiter for name in limiters.values())):
        return False
    if len(pipeline) != output_count + 4 or not all(
        isinstance(step, Mapping) for step in pipeline
    ):
        return False
    channel_lists = [pipeline[index].get("channels", ()) for index in (2, 3)]
    role_steps = [
        {"type": "Filter", "channels": channels,
         "names": [*(name for _, name in sorted(protections[role])), limiters[role]]}
        for role, channels in zip(roles, channel_lists, strict=True)
    ]
    expected = [
        {"type": "Filter", "channels": [0, 1], "names": ["active_startup_headroom"]},
        {"type": "Mixer", "name": "split_active_2way"}, *role_steps,
        *({"type": "Filter", "channels": [index], "names": [name]}
          for index, name in enumerate(mutes)),
    ]
    channel_sets = [set(channels) for channels in channel_lists]
    mixer = mixers.get("split_active_2way")
    return (
        pipeline == expected and set(mixers) == {"split_active_2way"}
        and isinstance(mixer, Mapping)
        and mixer.get("channels") == {"in": 2, "out": output_count}
        and all(channel_sets) and not channel_sets[0] & channel_sets[1]
        and set.union(*channel_sets) == set(range(output_count))
    )


def emit_active_speaker_program_config(
    preset: ActiveSpeakerPreset,
    *,
    role_channels: dict[str, int],
    playback_device: str,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    protective_hp_min_corner_hz: float = TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    protective_hp_min_slope_db_per_octave: float = (
        PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE
    ),
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int | None = None,
    enable_rate_adjust: bool | None = None,
    inverted_roles: Sequence[str] = (),
    measurement_delays_us: Mapping[str, float] | None = None,
    measurement_level_trims_db: Mapping[str, float] | None = None,
    parked_target_ids: Collection[str] = (),
    out_path: str | Path | None = None,
) -> str:
    """Emit the static channel-routed program graph for CHECK/MEASURE playback.

    ``role_channels`` maps each driver role to the program-WAV channel carrying
    its stimulus (ch0 → woofer, ch1 → tweeter). The graph routes each program
    channel to that driver's PHYSICAL output path through a role-routed mixer,
    carries either the legacy target crossover or caller-supplied confirmed role
    protection plus the per-driver limiter, keeps the software volume ceiling
    non-positive, and stays static (no reload mid-program). The
    protected-neutral shape omits configured crossover, delay, linearization,
    bass, Room and preference filters.

    ``inverted_roles``, ``measurement_delays_us`` and
    ``measurement_level_trims_db`` are parameters of THIS measurement emitter
    and of nothing else: the applied and baseline emitters take their per-driver
    delay and gain from the profile's ``corrections`` and cannot reach these, so
    a swept coordinate can never leak into a graph a household plays. Empty or
    ``None`` keeps every existing program byte-identical.

    * ``inverted_roles`` is level-neutral — see :func:`_emit_role_routed_mixer`.
    * ``measurement_delays_us`` reaches the YAML through a single
      :func:`~jasper.camilla_emit.fmt` pass, the same formatter
      :func:`~jasper.active_speaker.delay_graph.quantized_delay_ms` is
      implemented as, so a proof recomputing from the same ``delay_us`` agrees
      exactly. Delays ride ahead of the protection sections; a pure delay
      commutes, so the position changes no magnitude.
    * ``measurement_level_trims_db`` lands on ONE seam, the role-routed mixer's
      per-source gain — the per-output commissioning gain is deliberately not
      also touched, or one decision would be applied twice. Attenuation only.

    Two fail-closed gates run before the graph can leave: a build-time proof
    that the selected tweeter HP satisfies the declared floor, and
    :func:`_assert_program_graph_proven` over the emitted text.
    """

    preset.validate()
    # Scope gate: ONE program channel per declared driver role — a 1-way passive
    # main or a 2-way. A 3-way needs a designed reshape (mid-band MESM schedule,
    # per-region alignment), not a silent generalization of this emitter.
    if preset.way_count not in (1, 2):
        raise ActiveSpeakerConfigError(
            "the crossover-measurement program graph is scoped to 1- and 2-way "
            f"presets; way_count={preset.way_count} requires a designed "
            "program reshape"
        )
    role_channels = _validate_program_role_channels(preset, role_channels, parked_target_ids)
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
    limiter_clip_limit_db = _finite_float(limiter_clip_limit_db, "limiter_clip_limit_db")
    protective_hp_min_corner_hz = _finite_float(
        protective_hp_min_corner_hz, "protective_hp_min_corner_hz"
    )
    protective_hp_min_slope_db_per_octave = _finite_float(
        protective_hp_min_slope_db_per_octave,
        "protective_hp_min_slope_db_per_octave",
    )
    _assert_volume_limit(volume_limit_db)
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    tweeter_hp_name = None
    if protection_sections_by_role is None:
        _assert_tweeter_crossover_hp_satisfies_floor(
            preset,
            min_corner_hz=protective_hp_min_corner_hz,
            min_slope_db_per_octave=protective_hp_min_slope_db_per_octave,
        )
    else:
        required_roles = set(required_driver_roles(preset.way_count))
        if set(protection_sections_by_role) != required_roles:
            raise ActiveSpeakerConfigError("program protection must cover every driver role")
        # Asked of the role that DECLARES one: a 1-way main has no tweeter for
        # a high-pass to protect, so the gate is absent, not waived
        # (``_assert_program_graph_proven`` agrees from the emitted text).
        if "tweeter" in required_roles:
            tweeter_hps = [
                (index, section)
                for index, section in enumerate(protection_sections_by_role["tweeter"])
                if section.highpass
            ]
            if len(tweeter_hps) != 1:
                raise ActiveSpeakerConfigError("program graph requires one tweeter protection high-pass")
            hp_index, hp_section = tweeter_hps[0]
            if hp_section.fc_hz < protective_hp_min_corner_hz or (
                hp_section.order * 6.0 < protective_hp_min_slope_db_per_octave
            ):
                # SOLE slope-floor enforcement on this path: it reaches the
                # journal exactly as its predecessor's refusal does.
                log_event(
                    logger, "active_speaker.program_emit_gate", level=logging.ERROR,
                    result="blocked_tweeter_protection_below_floor",
                    preset_id=preset.preset_id, fc_hz=f"{hp_section.fc_hz:g}",
                    order=hp_section.order)
                raise ActiveSpeakerConfigError("tweeter protection does not satisfy the program floor")
            tweeter_hp_name = _program_protection_name("tweeter", hp_index)

    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    program_channels = program_channel_count(role_channels)
    # Program headroom is the commissioning headroom (0 dB), so
    # the effective-peak ledger the session-volume plan and admission share is
    # main_volume + program peak with no hidden graph attenuation.
    audible = frozenset(range(output_count))
    filter_mode = (
        APPLIED_RESPONSE_FILTER_MODE
        if protection_sections_by_role is None else "protected_neutral"
    )
    filter_yaml = _emit_commissioning_filter_definitions(
        preset,
        startup_headroom_db=COMMISSIONING_HEADROOM_DB,
        limiter_clip_limit_db=limiter_clip_limit_db,
        audible_outputs=audible,
        audible_gain_db=0.0,
        filter_mode=filter_mode,
        protection_sections_by_role=protection_sections_by_role,
        measurement_delays_us=measurement_delays_us,
    )
    level_trims = _validated_measurement_trims(preset, measurement_level_trims_db)
    mixer_yaml = _emit_role_routed_mixer(
        preset, role_channels,
        apply_region_polarity=protection_sections_by_role is None,
        inverted_roles=inverted_roles,
        level_trims_db=level_trims,
    )
    pipeline_yaml = _emit_commissioning_pipeline(
        preset,
        filter_mode=filter_mode,
        protection_sections_by_role=protection_sections_by_role,
        measurement_delay_roles=frozenset(measurement_delays_us or ()),
        capture_channels=program_channels,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# role_channels={dict(sorted(role_channels.items()))}",
        f"# program_channels={program_channels}",
        f"# filter_mode={filter_mode}",
        # The graph SAYS which coordinate it carries, so a record naming it by
        # fingerprint reads without reconstructing the Delay filter body.
        # Emitted ONLY when there is one: an unconditional line would change the
        # bytes — and so the fingerprint — of every CHECK and MEASURE graph.
        *(
            [
                "# measurement_delays_us="
                + repr(dict(sorted(measurement_delays_us.items())))
            ]
            if measurement_delays_us
            else []
        ),
        # On the delay line's terms: emitted ONLY when a level match is
        # declared. The numbers come from the SAME validated mapping the mixer
        # gains did, so the graph states the trims it actually carries.
        *(
            ["# measurement_level_trims_db=" + repr(dict(sorted(level_trims.items())))]
            if level_trims
            else []
        ),
    ]
    if inverted_roles:
        # Emitted only when a branch is actually flipped, so a non-inverted emit
        # stays byte-identical; the graph then SAYS which branch carries the
        # reverse-null, beside the fingerprint a record names it by.
        metadata_comments.append(f"# inverted_roles={sorted(set(inverted_roles))}")
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker crossover-measurement program config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_program_config
{metadata_yaml}
# DO NOT HAND-EDIT. Static channel-routed program graph: program capture channel
# c is routed to every physical output of role role_channels^-1(c), each carrying
# its declared protection filter + soft-clip limiter. Played once (no reload
# mid-program) while a 2-channel program WAV sequences the driver stimuli by
# channel. The software volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
  capture:
    type: Alsa
    channels: {program_channels}
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

    # L0 emit gate (fail-closed): the shared per-output tweeter-protection re-proof.
    # A rear target with its own program channel is the take's second branch;
    # muting it would record silence. Every other rear output keeps the mute.
    yaml = _mute_unfitted_rear_outputs(
        yaml, preset, excited_target_ids=frozenset(role_channels),
    )
    _assert_tweeter_outputs_protected(yaml, preset)
    # Build-and-prove the program graph's return contract against graph_safety.
    _assert_program_graph_proven(
        yaml, preset, min_corner_hz=protective_hp_min_corner_hz,
        tweeter_hp_name=tweeter_hp_name,
    )
    _assert_measurement_delays_bound(
        yaml, measurement_delays_us, role_channels=role_channels, preset=preset,
    )

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_program_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d channels=%d",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            program_channels,
        )
    return yaml
