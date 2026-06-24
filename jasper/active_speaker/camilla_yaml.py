# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit commissioning-safe CamillaDSP templates for active speakers.

This module is intentionally side-effect-light: it can build or write a
candidate YAML file, but it does not ask CamillaDSP to load it. Hardware
activation belongs behind later channel-identity and path-safety gates.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Sequence

from jasper.atomic_io import atomic_write_text
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_CHUNKSIZE,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TARGET_LEVEL,
    DEFAULT_VOLUME_LIMIT_DB,
    FilterSpec,
    PeqFilter,
    total_positive_boost_db,
)
from jasper.camilla_emit import (
    CHANNEL_SELECT_MIXER,
    emit_channel_select_mixer,
    emit_gain_filter,
    emit_linkwitz_riley,
    emit_mixer,
    emit_peaking_biquad,
    fmt,
    mono_sum_sources,
)
from jasper.camilla_stereo_prefix import emit_filter_spec
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile

from .profile import (
    ADJACENT_PAIRS_BY_WAY,
    SUB_CROSSOVER_ORDER,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    lowest_driver_role,
    required_driver_roles,
)
from .test_signal_plan import (
    protective_tweeter_highpass_frequency_hz,
)

logger = logging.getLogger(__name__)

ACTIVE_STARTUP_CONFIG_NAME = "active_speaker_startup.yml"
STARTUP_HEADROOM_DB = 40.0
COMMISSIONING_HEADROOM_DB = 0.0
STARTUP_MUTE_GAIN_DB = -120.0
STARTUP_LIMITER_CLIP_LIMIT_DB = -12.0
BASELINE_HEADROOM_DB = 0.0
BASELINE_LIMITER_CLIP_LIMIT_DB = -1.0
FORBIDDEN_ACTIVE_PLAYBACK_TOKENS = (
    DEFAULT_PLAYBACK_DEVICE,
    "jasper_out",
)

# The active-LEADER's camilla#1 program-domain bake (distributed-active Stage B).
# It emits ONLY the program domain (Layer B room correction + Layer C preference
# EQ + program headroom) to a ``File`` sink writing the snapserver pipe — NO
# Layer A (no 2->N split, no per-driver crossover/delay/gain/limiter; those live
# in camilla#2). This distinct ``# Source:`` marker is what the runtime verifier
# keys on to recognise the bake as a DAC-less program graph; the *safety* of the
# exemption keys on ``devices.playback.type == File`` (no DAC attached, so no
# driver can be over-driven), not on this string. Stamped over emit_sound_config's
# own marker so the bake stays distinguishable from the solo /sound + correction
# program graphs that share emit_sound_config's program assembly.
ACTIVE_PROGRAM_BAKE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config"
)

# Driver-domain-only (active follower) emit. A follower picks ONE inter-speaker
# channel of the leader's already-corrected stereo program, so the valid
# program-channel selections are left / right / a clip-safe mono sum. ``stereo``
# is passthrough (not a single-box pick) and ``sub`` is the wireless-sub member
# (gap 5) — both are out of scope for the follower driver-domain emit.
DRIVER_DOMAIN_PROGRAM_CHANNELS = ("left", "right", "mono")

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
        # A mono cabinet sums L+R to each driver via the shared clip-safe recipe
        # (the same one the inter-speaker channel-select uses); ``inverted``
        # carries this driver's polarity.
        return mono_sum_sources(inverted=inverted)
    raise ActiveSpeakerConfigError(f"unsupported layout {layout!r}")


def _emit_split_mixer(preset: ActiveSpeakerPreset) -> str:
    polarity = _role_polarity(preset)
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    # Active-speaker policy: build the (dest -> L/R-sum sources) map from
    # the preset's driver layout + per-driver polarity. The YAML spelling
    # is the shared emit_mixer; this routing is the assembly concern.
    mapping: list[tuple[int, list[tuple[int, float, bool]]]] = [
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
    labels = [output.label for output in outputs]
    sub = preset.local_subwoofer
    if sub is not None:
        # The local subwoofer taps the SAME full-range program as the mains: it
        # mono-sums L+R with the clip-safe -6.02 dB recipe. Its band-limiting
        # low-pass and excursion limiter live in the per-output pipeline chain,
        # NOT here — the mixer is pure routing.
        mapping.append((sub.physical_output_index, mono_sum_sources(inverted=False)))
        labels.append(sub.label)
    return emit_mixer(
        f"split_active_{preset.way_count}way",
        channels_in=2,
        channels_out=output_count,
        mapping=mapping,
        description=(
            f"{preset.channel_map.layout} source -> "
            f"{output_count} protected active outputs"
        ),
        labels=labels,
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


def _room_peq_name(index: int) -> str:
    return f"room_peq_{index}"


def _protective_tweeter_hp_name(role: str) -> str:
    return f"as_{_name_token(role)}_protective_hp"


# --- local-subwoofer + bass-management filter names ---------------------------
# The single home for the local-sub lane spellings. The sub output carries an LR4
# low-pass (band-limit) + non-positive baseline gain + soft-clip limiter
# (excursion); the mains' lowest driver carries the complementary LR4 high-pass
# (bass management). Mirrors the per-driver name helpers above so the verifier
# imports one alias per filter rather than re-deriving the format.


def _sub_lowpass_name() -> str:
    return "as_sub_lowpass"


def _sub_baseline_gain_name() -> str:
    return "as_sub_baseline_gain"


def _sub_baseline_limiter_name() -> str:
    return "as_sub_baseline_limiter"


def _sub_startup_mute_name() -> str:
    return "as_sub_startup_mute"


def _sub_startup_limiter_name() -> str:
    return "as_sub_startup_limiter"


def _bass_management_hp_name(role: str) -> str:
    """The complementary mains bass-management high-pass on the lowest driver."""
    return f"as_{_name_token(role)}_bass_mgmt_hp"


# --- public filter-name vocabulary -------------------------------------------
# The emitter owns the spelling of every filter name it writes. The
# verification side (runtime_contract / staging / commission_ramp, via
# graph_evidence) imports THESE aliases rather than hardcoding a literal like
# "as_tweeter_startup_limiter" or re-deriving the format, so a name change here
# can never silently desync a safety verifier from the graph it inspects (the
# verifier would otherwise look for a filter that no longer exists and fail
# closed, spuriously blocking commissioning). Aliases (not renames) keep the
# emitter's own call sites — and its emission behaviour — completely untouched.
driver_mute_name = _driver_mute_name
driver_limiter_name = _driver_limiter_name
driver_baseline_gain_name = _driver_baseline_gain_name
driver_baseline_limiter_name = _driver_baseline_limiter_name
protective_tweeter_hp_name = _protective_tweeter_hp_name
# Local-sub + bass-management aliases (same emitter-owned-spelling contract).
sub_lowpass_name = _sub_lowpass_name
sub_baseline_gain_name = _sub_baseline_gain_name
sub_baseline_limiter_name = _sub_baseline_limiter_name
sub_startup_mute_name = _sub_startup_mute_name
sub_startup_limiter_name = _sub_startup_limiter_name
bass_management_hp_name = _bass_management_hp_name

# The inter-speaker channel-select mixer name — emitter-owned graph vocabulary
# the verification side re-imports (via graph_evidence) rather than hardcoding,
# so a rename can never silently desync the runtime_contract driver-domain arm
# from the graph it inspects. The name is owned by the shared leaf
# (jasper.camilla_emit) and re-exported here so the active-speaker verifier has
# one import point.
channel_select_mixer_name = CHANNEL_SELECT_MIXER


def _protective_tweeter_hp_frequency(
    preset: ActiveSpeakerPreset,
    role: str,
) -> float | None:
    return protective_tweeter_highpass_frequency_hz(preset, role)


def _driver_filter_chain(preset: ActiveSpeakerPreset, role: str) -> list[str]:
    names: list[str] = []
    if _bass_management_active(preset, role):
        names.append(_bass_management_hp_name(role))
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


def _bass_management_active(preset: ActiveSpeakerPreset, role: str) -> bool:
    """True iff ``role`` is the lowest driver AND a local sub is present — the
    side whose lowest driver carries the complementary bass-management high-pass."""
    return (
        preset.local_subwoofer is not None
        and role == lowest_driver_role(preset.way_count)
    )


def _driver_baseline_filter_chain(preset: ActiveSpeakerPreset, role: str) -> list[str]:
    names: list[str] = []
    # Bass-management high-pass FIRST: the lowest driver's program is high-passed
    # at the sub crossover corner before its own crossover/delay/gain/limiter. The
    # sub low-pass at the same corner is the complementary lower half (see the sub
    # lane below) — together they are one crossover.
    if _bass_management_active(preset, role):
        names.append(_bass_management_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    names.append(_driver_delay_name(role))
    names.append(_driver_baseline_gain_name(role))
    names.append(_driver_baseline_limiter_name(role))
    return names


def _sub_baseline_filter_chain() -> list[str]:
    """The local-sub baseline lane: band-limit (LR4 low-pass), then the same
    per-driver protection a main gets (non-positive gain + soft-clip limiter)."""
    return [
        _sub_lowpass_name(),
        _sub_baseline_gain_name(),
        _sub_baseline_limiter_name(),
    ]


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
    lines.extend(_emit_bass_management_hp_definition(preset))
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
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_startup_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
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


def _emit_baseline_driver_definitions(
    preset: ActiveSpeakerPreset,
    *,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
) -> list[str]:
    """The driver-domain (Layer A) filter definitions shared by the solo/leader
    baseline and the follower's driver-domain-only graph.

    Emits the per-region Linkwitz-Riley crossover pair, then each driver's
    [delay, non-positive baseline gain, soft-clip limiter] chain. This is the
    *intra-speaker* half — it has no program-domain headroom and no preference
    EQ (those are program-domain, wired only by the baseline caller). The
    follower's driver-domain emit reuses this verbatim so the relocated Layer A
    is byte-for-byte the same protective chain a solo speaker runs.
    """
    lines: list[str] = []
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
    # Bass-management high-pass on the lowest driver (the complementary upper half
    # of the single sub crossover). Emitted only when a local sub is present.
    sub = preset.local_subwoofer
    lines.extend(_emit_bass_management_hp_definition(preset))
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
    # The local-sub lane definitions: LR4 low-pass (band-limit) + non-positive
    # baseline gain + soft-clip limiter (excursion), same protection a main gets.
    if sub is not None:
        lines.extend(_emit_sub_baseline_definitions(
            sub.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    return lines


def _emit_bass_management_hp_definition(preset: ActiveSpeakerPreset) -> list[str]:
    """The LR4 bass-management high-pass filter def on the lowest driver, or [].

    The complementary upper half of the single sub crossover at the sub corner.
    Shared by every emitter (startup/commissioning/baseline) so the HP corner +
    order have ONE definition that cannot drift between them."""
    sub = preset.local_subwoofer
    if sub is None:
        return []
    return emit_linkwitz_riley(
        _bass_management_hp_name(lowest_driver_role(preset.way_count)),
        highpass=True,
        freq_hz=sub.crossover_fc_hz,
        order=SUB_CROSSOVER_ORDER,
    )


def _emit_sub_startup_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub startup/commissioning lane definitions: LR4 low-pass +
    soft-clip limiter + hard mute.

    The sub starts muted for commissioning safety (mirrors a driver's startup
    mute). The band-limit (low-pass) and excursion limiter are still present so an
    un-muting Phase-B path arms an already-protected sub output."""
    return [
        *emit_linkwitz_riley(
            _sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *_emit_limiter_filter(
            _sub_startup_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
        *emit_gain_filter(_sub_startup_mute_name(), STARTUP_MUTE_GAIN_DB, mute=True),
    ]


def _sub_startup_filter_chain() -> list[str]:
    """The local-sub startup lane: band-limit, limiter, then the hard mute."""
    return [
        _sub_lowpass_name(),
        _sub_startup_limiter_name(),
        _sub_startup_mute_name(),
    ]


def _emit_sub_commissioning_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub commissioning lane definitions: LR4 low-pass + soft-clip
    limiter only.

    Mirrors a driver's commissioning definitions — the lane's own startup mute is
    dropped (the per-output commission mute does the muting), so no orphan mute
    filter is emitted. The band-limit + excursion limiter are still present so the
    sub output stays protected even when its per-output mute is later lifted."""
    return [
        *emit_linkwitz_riley(
            _sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *_emit_limiter_filter(
            _sub_startup_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
    ]


def _sub_commissioning_filter_chain() -> list[str]:
    """The local-sub commissioning lane: band-limit + excursion limiter only.

    Mirrors a driver's commissioning chain — the per-output commission mute
    (appended in the pipeline) replaces the lane's own startup mute, so exactly
    one physical output is excited through the real graph. The LR4 low-pass and
    soft-clip limiter are preserved so the sub output stays band-limited AND
    excursion-limited even when its per-output mute is later lifted to ramp it."""
    return [
        _sub_lowpass_name(),
        _sub_startup_limiter_name(),
    ]


def _emit_sub_baseline_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub baseline filter definitions: LR4 low-pass + gain + limiter.

    The sub protection mirrors the mains' per-driver chain (non-positive gain +
    soft-clip limiter); the band-limit is the LR4 low-pass at the bass-management
    corner. The subwoofer commissioning-tone bounds (50 Hz floor / 300 ms) live in
    ``driver_protection.driver_protection_profile('subwoofer')`` and gate the
    later audible ramp; the durable graph's protection is this gain<=0 + limiter.
    """
    return [
        *emit_linkwitz_riley(
            _sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *emit_gain_filter(_sub_baseline_gain_name(), 0.0),
        *_emit_limiter_filter(
            _sub_baseline_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
    ]


def _emit_baseline_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    baseline_headroom_db: float,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
) -> str:
    lines: list[str] = []
    room_peqs = tuple(room_peqs)
    for i, peq in enumerate(room_peqs, start=1):
        lines.extend(
            emit_peaking_biquad(
                _room_peq_name(i),
                freq=peq.freq,
                q=peq.q,
                gain=peq.gain,
            )
        )
    # Program-domain headroom for the pre-split room PEQ (Layer B) and
    # preference EQ (Layer C). This gain is the active graph's single place for
    # explicit common attenuation: baseline headroom, room-correction boost
    # headroom, plus the household's manual headroom / loudness-match
    # output_trim_db when a profile has EQ. Preference boosts themselves ride at
    # unity, matching the stereo /sound policy: boosts boost. Room-correction
    # boosts are different: correction can raise a known room band above unity,
    # so its worst-case positive boost is folded into this headroom gain rather
    # than emitted as a separate room_headroom filter.
    trim_db = max(0.0, output_trim_db) if preference_filters else 0.0
    total_headroom_db = (
        baseline_headroom_db
        + total_positive_boost_db(room_peqs)
        + trim_db
    )
    headroom_gain_db = 0.0 if total_headroom_db == 0 else -total_headroom_db
    lines.extend(
        emit_gain_filter(
            "active_baseline_headroom",
            headroom_gain_db,
        )
    )
    lines.extend(_emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=corrections,
    ))
    # Program-domain preference EQ (Layer C) definitions. Emitted via the shared
    # leaf emit_filter_spec (the same one emit_sound_config uses), so the active
    # and stereo paths spell a preference band identically. They are wired
    # pre-split — see _emit_baseline_pipeline.
    for spec in preference_filters:
        lines.extend(emit_filter_spec(spec))
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
    sub = preset.local_subwoofer
    if sub is not None:
        chain = ", ".join(_sub_startup_filter_chain())
        lines.extend([
            "  - type: Filter",
            f"    channels: [{sub.physical_output_index}]",
            f"    names: [{chain}]",
        ])
    return "\n".join(lines)


def _emit_baseline_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    room_peq_names: Sequence[str] = (),
    preference_filter_names: Sequence[str] = (),
) -> str:
    lines: list[str] = []
    # Room PEQs (Layer B) run on the stereo program bus before the common
    # active_baseline_headroom gain. The gain absorbs their positive-boost
    # headroom so the active path stays one-preamp-shaped.
    if room_peq_names:
        names = ", ".join(room_peq_names)
        lines.extend([
            "  - type: Filter",
            "    channels: [0, 1]",
            f"    names: [{names}]",
        ])
    lines.extend([
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_baseline_headroom]",
    ])
    # Preference EQ (Layer C) is a PROGRAM-domain transform: it rides the
    # stereo bus on channels [0, 1] strictly BEFORE the split mixer, so it is
    # upstream of every per-driver crossover, limiter, and tweeter high-pass.
    # That placement is what makes a preference boost safe — it can neither
    # move a crossover corner nor bypass a driver limiter. Emitted only when
    # present, so a flat profile is byte-identical to the pre-PR-3 baseline.
    if preference_filter_names:
        names = ", ".join(preference_filter_names)
        lines.extend([
            "  - type: Filter",
            "    channels: [0, 1]",
            f"    names: [{names}]",
        ])
    lines.extend([
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ])
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(_driver_baseline_filter_chain(preset, role))
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset))
    return "\n".join(lines)


def _sub_baseline_pipeline_lines(preset: ActiveSpeakerPreset) -> list[str]:
    """The sub's baseline pipeline Filter step (its own output channel), or []."""
    sub = preset.local_subwoofer
    if sub is None:
        return []
    chain = ", ".join(_sub_baseline_filter_chain())
    return [
        "  - type: Filter",
        f"    channels: [{sub.physical_output_index}]",
        f"    names: [{chain}]",
    ]


def _emit_driver_domain_pipeline(preset: ActiveSpeakerPreset) -> str:
    # Driver-domain-only (follower) pipeline. The inter-speaker channel-select
    # runs FIRST (a 2->2 Mixer that picks L/R/mono from the leader's corrected
    # stereo program), THEN the intra-speaker 2->N split, THEN each driver's
    # crossover/delay/gain/limiter chain — exactly channel_split.py's documented
    # composition order (inter-speaker axis before intra-speaker axis). There is
    # NO program-domain (channels [0, 1]) Filter step: the leader baked Layer
    # B/C, so this graph carries no headroom gain and no preference EQ — only
    # the protective driver chain. Both stages are Mixer steps, so the [0, 1]
    # bus is never the target of a Filter step here.
    lines = [
        "  - type: Mixer",
        f"    name: {CHANNEL_SELECT_MIXER}",
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
    lines.extend(_sub_baseline_pipeline_lines(preset))
    return "\n".join(lines)


def _output_count(preset: ActiveSpeakerPreset) -> int:
    indexes = [output.index for output in preset.channel_map.outputs]
    if preset.local_subwoofer is not None:
        indexes.append(preset.local_subwoofer.physical_output_index)
    return max(indexes) + 1


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
    # The bass-management HP is referenced by the lowest driver's commissioning
    # chain (it preserves the running graph's protection), so its definition must
    # be present here too.
    lines.extend(_emit_bass_management_hp_definition(preset))
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
    # The local-sub lane definitions (LR4 low-pass + soft-clip limiter): the sub
    # output is band-limited AND excursion-limited even in the commissioning graph,
    # exactly like the mains. Its muting is the per-output commission mask below
    # (the sub's own startup mute is not wired into the commissioning chain).
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_commissioning_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
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
    # The local sub's protective lane (band-limit + excursion limiter) on its own
    # output channel, BEFORE the per-output commission mute below — so the sub is
    # protected exactly like a driver when its mute is later lifted to ramp it.
    sub = preset.local_subwoofer
    if sub is not None:
        chain = ", ".join(_sub_commissioning_filter_chain())
        lines.extend([
            "  - type: Filter",
            f"    channels: [{sub.physical_output_index}]",
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
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Build an accepted active-speaker baseline candidate.

    Unlike the startup template, this YAML is not muted. It still preserves
    the JTS 0 dB volume ceiling, keeps per-driver limiters, and refuses
    positive per-driver correction gain.
    Callers own the acceptance evidence and explicit CamillaDSP apply step.

    ``room_peqs`` (Layer B) is the preserved room-correction PEQ set. Each
    filter is emitted on program channels [0, 1] before the split mixer, and any
    positive room-correction boost is folded into ``active_baseline_headroom``.
    That keeps the active path one-headroom-shaped while preserving the stereo
    correction safety policy.

    ``preference_filters`` (Layer C) is the program-domain preference EQ band
    list — the same ``FilterSpec`` objects ``build_sound_filters`` produces for
    the stereo emitter. When non-empty, each ``.active()`` band is emitted on
    the program channels [0, 1] strictly *before* the split mixer. Preference
    boosts ride at unity, matching ``emit_sound_config``: the active graph keeps
    them safe by placing them upstream of every crossover/limiter/tweeter HP and
    preserving the 0 dB volume ceiling. The empty default keeps every existing
    caller byte-identical.

    ``output_trim_db`` is the household's manual headroom + loudness-match
    attenuation (``jasper.sound.settings.output_trim_db``), folded into the same
    ``active_baseline_headroom`` gain so the active path honours it exactly like
    ``emit_sound_config``. It is applied only when ``preference_filters`` is
    non-empty (a flat profile can't clip from EQ and plays at unity), so the
    default keeps the no-EQ baseline byte-identical.
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
    output_trim_db = _finite_float(output_trim_db, "output_trim_db")
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

    # Drop inactive bands (a near-zero gain rounds to a no-op) exactly like the
    # stereo emitter's build_sound_filters does, so an "all flat" preference
    # profile emits nothing and stays byte-identical to the pre-PR-3 baseline.
    active_preference_filters = tuple(
        spec for spec in preference_filters if spec.active()
    )
    room_peqs = tuple(room_peqs)

    output_count = _output_count(preset)
    filter_yaml = _emit_baseline_filter_definitions(
        preset,
        baseline_headroom_db=baseline_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
        room_peqs=room_peqs,
        preference_filters=active_preference_filters,
        output_trim_db=output_trim_db,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_baseline_pipeline(
        preset,
        room_peq_names=[_room_peq_name(i) for i in range(1, len(room_peqs) + 1)],
        preference_filter_names=[spec.name for spec in active_preference_filters],
    )
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


def emit_active_speaker_driver_domain_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    program_channel: str,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Build a **driver-domain-only** active-speaker graph for a wireless follower.

    This is the active-crossover analogue of the dumb follower's channel-pick: an
    *endpoint-crossover* graph that runs only **Layer A** (the ``2->N`` split plus
    each driver's crossover / delay / non-positive gain / soft-clip limiter, with
    the tweeter band-limited by its crossover high-pass) on a stereo program the
    **leader already corrected** (Layer B room PEQ + Layer C preference EQ baked
    into the streamed program). It therefore emits **no** program-domain prefix —
    no ``active_baseline_headroom`` gain and no preference-EQ band — because that
    domain belongs to the leader's bake instance.

    The pipeline is ``channel_select (2->2 pick L/R/mono) -> split_active_<way>way
    (2->N) -> per-driver chain``: the inter-speaker channel-select runs FIRST
    (which channel of the pair this box plays), THEN the intra-speaker driver
    split — exactly ``jasper.multiroom.channel_split``'s documented composition
    order. ``program_channel`` is one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``
    (``left`` / ``right`` / ``mono``); the channel-select mixer is the shared
    ``emit_channel_select_mixer`` primitive, so a follower and a bonded member
    spell the pick identically.

    Like the baseline emitter it keeps the JTS 0 dB volume ceiling, per-driver
    soft-clip limiters, and non-positive per-driver correction gain, and it
    refuses the existing stereo outputd lane as a playback device. It does NOT
    load or reload CamillaDSP — the reconciler (gap 3, a later slice) owns
    pointing the capture at the round-trip loopback and the apply. ``corrections``
    carries the same commissioned per-driver delay/gain/polarity as the solo
    baseline (hardware truth, role-independent — gap 1), so the relocated Layer A
    is the same protective chain the speaker runs solo.
    """

    preset.validate()
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
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
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
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
    filter_yaml = "\n".join(_emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
    ))
    # channel_select FIRST (inter-speaker pick), then the intra-speaker split.
    mixer_yaml = "\n".join((
        emit_channel_select_mixer(program_channel),
        _emit_split_mixer(preset),
    ))
    pipeline_yaml = _emit_driver_domain_pipeline(preset)
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# program_channel={program_channel}",
    ]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker driver-domain config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_driver_domain_config
{metadata_yaml}
# This is a wireless follower's driver-domain-only Layer-A graph: it picks one
# inter-speaker channel of the leader's already-corrected stereo program, then
# runs the per-driver crossover/limiter chain. There is no program-domain
# headroom or preference EQ (the leader baked Layer B/C); outputs are not
# startup-muted, per-driver correction gain is non-positive, and the software
# volume ceiling remains 0 dB.

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
            "event=active_speaker_driver_domain_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d program_channel=%s",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            program_channel,
        )
    return yaml


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
    chunksize: int = DEFAULT_CHUNKSIZE,
    target_level: int = DEFAULT_TARGET_LEVEL,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    profile_id: str | None = None,
) -> str:
    """Build the active-LEADER's **program-domain-only** camilla#1 bake.

    Stage B (``docs/HANDOFF-distributed-active.md``, "camilla#1 program bake")
    splits an active *leader*'s DSP across two CamillaDSP instances. This emits
    the **program** half (camilla#1): Layer B room correction + Layer C
    preference EQ + program headroom, written to a ``File`` sink feeding the
    snapserver pipe (``SNAPFIFO``) so the follower(s) receive a corrected stereo
    wire. The **driver** half — the ``2->N`` split and every per-driver
    crossover / delay / gain / soft-clip limiter (Layer A) — lives in camilla#2
    and is **deliberately absent here**.

    It is a *separate* emit that **bypasses the graph carrier** (exactly like the
    follower's :func:`emit_active_speaker_driver_domain_config`): the carrier
    fence ``eq_on_active_bonded_member`` guards the interactive ``/sound`` EQ
    apply and is untouched by this path. The program assembly is
    :func:`jasper.sound.camilla_yaml.emit_sound_config`'s — reused verbatim with
    a ``File``/pipe sink and ``enable_rate_adjust=False`` (a ``File`` backend has
    no output clock for rate_adjust to steer; on the synced active chain the one
    rate-tracker is upstream) — so the baked correction is byte-for-byte the
    program graph the speaker already ships. Only the ``# Source:`` provenance
    marker differs: this config carries :data:`ACTIVE_PROGRAM_BAKE_SOURCE` so the
    runtime verifier recognises it as a DAC-less program bake.

    Safety is *by construction*: the playback is a pipe, not a DAC, so no driver
    can be over-driven and the full-range-to-tweeter invariant cannot be
    violated regardless of the saved speaker topology. The runtime verifier's
    matching exemption (:func:`jasper.active_speaker.runtime_contract.classify_camilla_graph`)
    keys on ``devices.playback.type == File`` — never on this marker — so an
    ALSA-sink program graph reaching the DAC stays blocked under a roleful
    topology.

    This emit does NOT load or reload CamillaDSP, and it does NOT wire camilla#1
    into the reconciler — that is a later Stage-B slice. ``out_path`` writes the
    YAML group-readably (0640) for callers that stage it; the default returns the
    text only.
    """

    # Import the snapserver pipe target lazily: it is the canonical camilla#1
    # sink and lives in the grouping reconciler (jasper.multiroom.reconcile),
    # whose module-load chain this read-heavy emitter must not pull eagerly. The
    # sibling jasper.multiroom.leader_config uses the same lazy-import idiom for
    # exactly this constant.
    from jasper.multiroom.reconcile import SNAPFIFO

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
        # The one rate-tracker on the synced active chain is upstream of
        # camilla#1; a File sink has no output clock to steer anyway. The
        # emit_sound_config pipe-sink guard enforces this pairing.
        enable_rate_adjust=False,
        playback_pipe_path=SNAPFIFO,
    )

    # Re-stamp provenance so the bake is distinguishable from the solo /sound +
    # correction program graphs that share emit_sound_config's assembly. Fail
    # loud if the upstream marker ever changes shape (a silent miss would ship a
    # bake the verifier can't route to the flat program path).
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


def _atomic_write_text(path: Path, text: str) -> None:
    # Active-speaker configs are read by both root-owned CamillaDSP helpers and
    # the non-root jasper-web commissioning route. Keep them group-readable.
    atomic_write_text(path, text, mode=0o640)


def active_speaker_startup_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / ACTIVE_STARTUP_CONFIG_NAME
