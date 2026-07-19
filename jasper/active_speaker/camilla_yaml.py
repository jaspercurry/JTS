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
from typing import TYPE_CHECKING, Any, Sequence

import yaml

from jasper.atomic_io import atomic_write_text
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    FilterSpec,
    PeqFilter,
    resolve_camilla_chunksize,
    resolve_camilla_target_level,
    total_positive_boost_db,
)
from jasper.camilla_emit import (
    CHANNEL_SELECT_MIXER,
    emit_butterworth_highpass,
    emit_channel_select_mixer,
    emit_gain_filter,
    emit_linkwitz_riley,
    emit_linkwitz_transform_biquad,
    emit_mixer,
    emit_peaking_biquad,
    fmt,
    mono_sum_sources,
)
from jasper.camilla_stereo_prefix import emit_filter_spec
from jasper.log_event import log_event
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile

from .graph_safety import (
    TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    bass_extension_block_valid,
    filter_param_matches,
    output_highpass_protected,
    pipeline_reference_closure_errors,
    tweeter_guard_present,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)
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

if TYPE_CHECKING:
    from jasper.bass_extension.profile import BassExtensionProfile

ACTIVE_STARTUP_CONFIG_NAME = "active_speaker_startup.yml"
STARTUP_HEADROOM_DB = 40.0
COMMISSIONING_HEADROOM_DB = 0.0
STARTUP_MUTE_GAIN_DB = -120.0
STARTUP_LIMITER_CLIP_LIMIT_DB = -12.0
COMMISSIONING_FILTER_MODE = "protected_startup"
APPLIED_RESPONSE_FILTER_MODE = "applied_crossover_response"
BASELINE_HEADROOM_DB = 0.0
BASELINE_LIMITER_CLIP_LIMIT_DB = -1.0
BASS_EXTENSION_LT_FILTER = "bass_ext_lt"
BASS_EXTENSION_SUBSONIC_FILTER = "bass_ext_subsonic"
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

# S1 (G7 chunksize-knob safety): the follower driver-domain graph captures the
# leader's stream from an snd-aloop loopback whose period underruns (EPIPE)
# below ~1024 frames. The JASPER_CAMILLA_CHUNKSIZE knob is deliberately tunable
# low for the direct-DAC output paths, but this loopback CAPTURE is floored so a
# latency-tuned box cannot underrun its follower capture. Equals DEFAULT_CHUNKSIZE
# today, but it is the loopback's own minimum — not the shipped default — so it
# is named separately and lives next to the only emitter that needs it.
FOLLOWER_LOOPBACK_MIN_CHUNKSIZE = 1024

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


def _bass_extension_emission(
    preset: ActiveSpeakerPreset,
    profile: BassExtensionProfile | None,
) -> dict[str, Any] | None:
    """Return the already-evaluated sealed natural block, or no block."""

    if profile is None or profile.status != "accepted":
        return None
    adapter_id = str(profile.enclosure["adapter_id"])
    if adapter_id != "sealed_v1":
        return None
    if any(target.subsonic is None for target in profile.targets):
        raise ActiveSpeakerConfigError(
            "sealed bass-extension profile requires subsonic protection on every target"
        )
    natural = profile.targets[-1]
    if natural.target_id != "natural" or natural.qp is None:
        raise ActiveSpeakerConfigError("sealed bass-extension natural target is invalid")
    owner = profile.bass_owner
    roles = tuple(str(role) for role in owner["roles"])
    channels = tuple(int(channel) for channel in owner["channels"])
    kind = str(owner["kind"])
    if kind == "woofer_way" and len(roles) == 1:
        expected = tuple(_channels_for_role(preset, roles[0]))
    elif kind == "local_sub" and roles == ("subwoofer",):
        sub = preset.local_subwoofer
        expected = () if sub is None else (sub.physical_output_index,)
    else:
        expected = ()
    if not expected or channels != expected:
        raise ActiveSpeakerConfigError(
            "bass-extension owner does not match the emitted active-speaker graph"
        )
    subsonic = dict(natural.subsonic or {})
    if (
        subsonic.get("type") != "ButterworthHighpass"
        or type(subsonic.get("order")) is not int
    ):
        raise ActiveSpeakerConfigError("bass-extension subsonic filter is unsupported")
    return {
        "kind": kind,
        "roles": roles,
        "channels": channels,
        "natural": natural,
        "subsonic": subsonic,
    }


def _bass_extension_profile_summary(
    block: dict[str, Any] | None,
) -> dict[str, Any]:
    if block is None:
        return {"runtime_block_required": False}
    natural = block["natural"]
    return {
        "runtime_block_required": True,
        "bass_owner_channels": list(block["channels"]),
        "natural": {
            "fp_hz": natural.fp_hz,
            "qp": natural.qp,
            "boost_headroom_db": natural.boost_headroom_db,
            "subsonic": dict(block["subsonic"]),
        },
    }


def _emit_bass_extension_definitions(block: dict[str, Any] | None) -> list[str]:
    if block is None:
        return []
    natural = block["natural"]
    subsonic = block["subsonic"]
    return [
        *emit_linkwitz_transform_biquad(
            BASS_EXTENSION_LT_FILTER,
            freq_act=natural.fp_hz,
            q_act=natural.qp,
            freq_target=natural.fp_hz,
            q_target=natural.qp,
        ),
        *emit_butterworth_highpass(
            BASS_EXTENSION_SUBSONIC_FILTER,
            freq=float(subsonic["freq"]),
            order=subsonic["order"],
        ),
    ]


def _bass_extension_chain_names(
    block: dict[str, Any] | None,
    *,
    role: str | None = None,
    local_sub: bool = False,
) -> list[str]:
    if block is None:
        return []
    owns = (
        block["kind"] == "local_sub"
        if local_sub
        else block["kind"] == "woofer_way" and role in block["roles"]
    )
    return (
        [BASS_EXTENSION_LT_FILTER, BASS_EXTENSION_SUBSONIC_FILTER]
        if owns
        else []
    )


def _assert_bass_extension_safe(
    yaml_text: str,
    preset: ActiveSpeakerPreset,
    block: dict[str, Any] | None,
) -> None:
    view = view_from_emitted_text(yaml_text)
    evidence = bass_extension_block_valid(
        view, _bass_extension_profile_summary(block)
    )
    limiter_ok = True
    if block is not None:
        channels = frozenset(block["channels"])
        if block["kind"] == "local_sub":
            limiter_name = _sub_baseline_limiter_name()
        else:
            limiter_name = _driver_baseline_limiter_name(block["roles"][0])
        limiter_ok = filter_param_matches(
            view,
            limiter_name,
            filter_type="Limiter",
            params={
                "clip_limit": BASELINE_LIMITER_CLIP_LIMIT_DB,
                "soft_clip": True,
            },
        )
        owner_steps = [step for step in view.pipeline_steps if step.channels == channels]
        limiter_ok = limiter_ok and len(owner_steps) == 1
        if limiter_ok:
            names = owner_steps[0].names
            required = (
                BASS_EXTENSION_LT_FILTER,
                BASS_EXTENSION_SUBSONIC_FILTER,
                limiter_name,
            )
            limiter_ok = all(name in names for name in required)
            if limiter_ok:
                limiter_ok = (
                    names.index(BASS_EXTENSION_LT_FILTER)
                    < names.index(BASS_EXTENSION_SUBSONIC_FILTER)
                    < names.index(limiter_name)
                )
    if evidence.valid and limiter_ok:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result="blocked_bass_extension",
        preset_id=preset.preset_id,
        reason=evidence.reason or "baseline_limiter_invalid",
    )
    raise ActiveSpeakerConfigError(
        "emitted bass-extension block failed independent safety proof"
    )


def _assert_tweeter_outputs_protected(yaml_text: str, preset: ActiveSpeakerPreset) -> None:
    """Fail-closed L0 emit gate: refuse a graph with an unprotected tweeter output.

    Runs on every active-speaker graph THIS module emits, right before it is
    returned or written, so an unprotected-tweeter graph can never leave the
    emitter (let alone be loaded). It re-proves — against the emitted text, not
    the emitter's own construction — that every physical output the preset
    assigns a ``tweeter`` (compression-driver) role carries a protective
    high-pass: its crossover high-pass and/or a dedicated protective high-pass.

    This closes the L0 hearing-safety hole (docs/HANDOFF-audio-measurement-core.md):
    a compression driver is ~25 dB more sensitive than the woofer, so a graph
    that routes full-range program to a tweeter output with no high-pass is a
    shrill / hot-tweeter hazard. The active emitters wire that protection by
    construction; this gate makes the guarantee ENFORCED (a future refactor that
    dropped the tweeter high-pass would fail loudly here rather than ship a
    dangerous graph). A preset with no tweeter role (a passive full-range or
    woofer-only shape) has nothing to protect, so the gate is a no-op — it never
    over-blocks. Observability: a block emits ``event=active_speaker.emit_gate``
    before raising, so the refusal is never silent.
    """
    tweeter_channels = _channels_for_role(preset, "tweeter")
    if not tweeter_channels:
        return
    view = view_from_emitted_text(yaml_text)
    unprotected = unprotected_tweeter_outputs(
        view, tweeter_channels=set(tweeter_channels)
    )
    if not unprotected:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result="blocked_unprotected_tweeter",
        preset_id=preset.preset_id,
        outputs=",".join(str(index + 1) for index in unprotected),
    )
    raise ActiveSpeakerConfigError(
        "refusing to emit an active-speaker graph that sends full-range program "
        "to a tweeter/compression-driver output without a protective high-pass on "
        "DAC output(s) " + ", ".join(str(index + 1) for index in unprotected)
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


def _emit_split_mixer(
    preset: ActiveSpeakerPreset,
    *,
    apply_region_polarity: bool = True,
) -> str:
    # Always run the cross-region polarity reduction — it is also the
    # consistency guard (a role inverted in one region but not another raises).
    # Only its result is optionally suppressed as the mixer's inversion source:
    # the baseline/driver-domain emitters carry polarity through ``corrections``
    # (a per-driver Gain filter, see ``_emit_baseline_driver_definitions``)
    # instead, so the mixer must stay a no-op inverter there or the two would
    # cancel each other out (double inversion == net non-inverted).
    region_polarity = _role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
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
driver_delay_name = _driver_delay_name
driver_baseline_gain_name = _driver_baseline_gain_name
driver_baseline_limiter_name = _driver_baseline_limiter_name
protective_tweeter_hp_name = _protective_tweeter_hp_name


def crossover_highpass_for_role(
    preset: ActiveSpeakerPreset, role: str
) -> tuple[str, float, int] | None:
    """Return the applied crossover high-pass protecting ``role``."""

    for region in _ordered_regions(preset):
        if region.upper_driver == role:
            return (
                _crossover_filter_name(role, region, highpass=True),
                region.fc_hz,
                region.order,
            )
    return None


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


def _driver_baseline_filter_chain(
    preset: ActiveSpeakerPreset,
    role: str,
    bass_extension: dict[str, Any] | None = None,
) -> list[str]:
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
    names.extend(_bass_extension_chain_names(bass_extension, role=role))
    names.append(_driver_delay_name(role))
    names.append(_driver_baseline_gain_name(role))
    names.append(_driver_baseline_limiter_name(role))
    return names


def _sub_baseline_filter_chain(
    bass_extension: dict[str, Any] | None = None,
) -> list[str]:
    """The local-sub baseline lane: band-limit (LR4 low-pass), then the same
    per-driver protection a main gets (non-positive gain + soft-clip limiter)."""
    return [
        _sub_lowpass_name(),
        *_bass_extension_chain_names(bass_extension, local_sub=True),
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


def _validated_driver_corrections(
    preset: ActiveSpeakerPreset,
    corrections: dict[str, dict[str, float | bool]] | None,
) -> dict[str, dict[str, float | bool]]:
    """Normalize the final per-driver correction gate shared by both emitters."""

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
    return safe_corrections


def _emit_baseline_driver_definitions(
    preset: ActiveSpeakerPreset,
    *,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
    bass_extension: dict[str, Any] | None = None,
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
    lines.extend(_emit_bass_extension_definitions(bass_extension))
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
    bass_extension: dict[str, Any] | None = None,
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
        bass_extension=bass_extension,
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
    bass_extension: dict[str, Any] | None = None,
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
        chain = ", ".join(
            _driver_baseline_filter_chain(preset, role)
            if bass_extension is None
            else _driver_baseline_filter_chain(preset, role, bass_extension)
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset, bass_extension))
    return "\n".join(lines)


def _sub_baseline_pipeline_lines(
    preset: ActiveSpeakerPreset,
    bass_extension: dict[str, Any] | None = None,
) -> list[str]:
    """The sub's baseline pipeline Filter step (its own output channel), or []."""
    sub = preset.local_subwoofer
    if sub is None:
        return []
    chain = ", ".join(_sub_baseline_filter_chain(bass_extension))
    return [
        "  - type: Filter",
        f"    channels: [{sub.physical_output_index}]",
        f"    names: [{chain}]",
    ]


def _emit_driver_domain_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    pair_trim_db: float = 0.0,
    bass_extension: dict[str, Any] | None = None,
) -> str:
    # Driver-domain-only (follower) pipeline. The inter-speaker channel-select
    # runs FIRST (a 2->2 Mixer that picks L/R/mono from the leader's corrected
    # stereo program), THEN the optional pair-balance trim on the selected stereo
    # bus, THEN the intra-speaker 2->N split, THEN each driver's
    # crossover/delay/gain/limiter chain. Exactly one helper owns this ordering so
    # an edit to the safety-critical Layer-A pipeline cannot fork the trimmed and
    # untrimmed cases.
    lines = [
        "  - type: Mixer",
        f"    name: {CHANNEL_SELECT_MIXER}",
    ]
    lines.extend([
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [pair_balance_trim]",
    ])
    lines.extend([
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ])
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(
            _driver_baseline_filter_chain(preset, role)
            if bass_extension is None
            else _driver_baseline_filter_chain(preset, role, bass_extension)
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset, bass_extension))
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
    chunksize: int | None = None,
    target_level: int | None = None,
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
    # CamillaDSP latency knobs (G7): None → env-or-default at call time so a
    # JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} override applies on the next
    # regeneration. Unset env → the literal defaults (byte-identical YAML).
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
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
# an extra protective high-pass, and the software volume ceiling remains
# non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
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

    # L0 emit gate (fail-closed): a startup graph still wires the crossover /
    # protective high-pass on the tweeter channel even though it starts muted, so
    # re-prove that protection before the config can leave the emitter.
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
    *,
    filter_mode: str,
) -> list[str]:
    """The startup chain minus the per-role mute.

    Commissioning isolates one *physical output* (not a whole role — a stereo
    woofer pair shares a role), so the role-level startup mute is dropped and a
    per-output mute layer is applied in the pipeline instead. Bring-up retains
    the dedicated tweeter high-pass; automatic response measurement removes
    only that extra filter so it measures the applied crossover shoulder.
    """
    excluded = {_driver_mute_name(role)}
    if filter_mode == APPLIED_RESPONSE_FILTER_MODE:
        excluded.add(_protective_tweeter_hp_name(role))
    return [name for name in _driver_filter_chain(preset, role) if name not in excluded]


def _emit_commissioning_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
    audible_outputs: frozenset[int],
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
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
        if filter_mode == COMMISSIONING_FILTER_MODE and protective_freq is not None:
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


def _emit_commissioning_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
) -> str:
    lines = [
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_startup_headroom]",
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ]
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(
            _commissioning_driver_filter_chain(
                preset,
                role,
                filter_mode=filter_mode,
            )
        )
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
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_headroom_db: float = STARTUP_HEADROOM_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
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
    if filter_mode not in {
        COMMISSIONING_FILTER_MODE,
        APPLIED_RESPONSE_FILTER_MODE,
    }:
        raise ActiveSpeakerConfigError(
            f"unsupported commissioning filter mode: {filter_mode!r}"
        )
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
    # CamillaDSP latency knobs (G7): None → env-or-default at call time so a
    # JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} override applies on the next
    # regeneration. Unset env → the literal defaults (byte-identical YAML).
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
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
        filter_mode=filter_mode,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_commissioning_pipeline(
        preset,
        filter_mode=filter_mode,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# audible_outputs={sorted(audible)}",
        f"# audible_gain_db={fmt(audible_gain_db)}",
        f"# filter_mode={filter_mode}",
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
# crossover/limiter chain. Bring-up uses the extra protective high-pass;
# automatic response measurement uses the applied crossover high-pass instead.
# The software volume ceiling remains non-positive in both modes.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
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

    # L0 emit gate (fail-closed): every tweeter output keeps its crossover /
    # protective high-pass even while the per-output commission mask mutes it, so
    # a graph that could later be unmuted onto a bare compression driver is
    # refused here rather than shipped.
    _assert_tweeter_outputs_protected(yaml, preset)

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


# --- channel-routed program graph (crossover measurement conductor, W2) ------
# The v2 crossover measurement flow plays ONE continuous 2-channel program WAV
# (docs/crossover-measurement-productization-design.md §5.4): program capture
# ch0 carries the woofer stimulus, ch1 the tweeter stimulus, sequenced in the
# WAV so the CamillaDSP graph stays static (no reload mid-program). This graph
# maps each program capture channel to its driver's PHYSICAL output path so the
# per-driver crossover / limiter / protective chain — the positions
# graph_safety's proofs inspect — stays exactly where the proven commissioning
# graph puts it (on the output channels, POST-mixer). The filter definitions and
# per-output pipeline are the APPLIED_RESPONSE commissioning graph's, reused
# verbatim; only the routing MIXER differs (role-routed, not stereo-side-routed)
# and every output is audible (a program never mutes a driver — the WAV silences
# it by channel). Measuring the as-crossed branches makes the two driver
# responses directly summable and keeps every tweeter output behind its final
# crossover high-pass throughout.

# LR4 is the shipped crossover slope; 24 dB/oct is the protective-HP floor slope
# a tweeter crossover high-pass must meet by construction (design §5.4).
PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE = 24.0


def _emit_role_routed_mixer(
    preset: ActiveSpeakerPreset,
    role_channels: dict[str, int],
    *,
    apply_region_polarity: bool = True,
) -> str:
    """Emit the program graph's role-routed split mixer.

    Unlike :func:`_emit_split_mixer` (which routes a stereo program bus by output
    *side*), this routes by driver *role*: every physical output of role ``r``
    takes its single source from ``role_channels[r]`` — the program-WAV channel
    carrying that driver's stimulus (ch0 → woofer, ch1 → tweeter, per design
    §5.4). ``channels_in`` is the program channel count (max mapped channel + 1).
    Region polarity is carried exactly as the commissioning mixer carries it, so
    the routed graph differs from the commissioning graph ONLY in its source
    selection, never in polarity or level.

    The emitted mixer is named ``split_active_{way_count}way`` — the SAME name
    :func:`_emit_split_mixer` uses — for two independent reasons that both land
    on the identical spelling: (1) :func:`_emit_commissioning_pipeline`, reused
    verbatim by the program graph, hardcodes a ``Mixer`` pipeline step under
    that exact name (CamillaDSP's ``SetConfig`` refuses to load a pipeline step
    referencing an undefined mixer — W6 hardware run 4 finding I: the program
    graph shipped as ``program_route_{way}way`` here while the pipeline pointed
    at ``split_active_{way}way``, and every program-graph load was rejected);
    (2) ``jasper.active_speaker.environment``'s ``_ACTIVE_SPLIT_RE`` — the
    runtime ecosystem's active-config classifier — recognizes an active-speaker
    config by the presence of a ``split_active_Nway`` mixer name. It is ecosystem
    vocabulary, not a routing claim: this mixer's ROUTING stays role-routed
    (see above), never side-routed like the commissioning/baseline/startup
    split — only the NAME is shared.
    """
    region_polarity = _role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    channels_in = 1 + max(role_channels.values())
    mapping: list[tuple[int, list[tuple[int, float, bool]]]] = [
        (
            output.index,
            [(role_channels[output.driver_role], 0.0, polarity[output.driver_role])],
        )
        for output in outputs
    ]
    labels = [output.label for output in outputs]
    return emit_mixer(
        f"split_active_{preset.way_count}way",
        channels_in=channels_in,
        channels_out=output_count,
        mapping=mapping,
        description=(
            f"program channels -> {output_count} role-routed active outputs"
        ),
        labels=labels,
    )


def _validate_program_role_channels(
    preset: ActiveSpeakerPreset,
    role_channels: dict[str, int],
) -> dict[str, int]:
    """Fail-closed check that every output's role owns one distinct program channel."""

    if preset.local_subwoofer is not None:
        raise ActiveSpeakerConfigError(
            "program graph does not support a local subwoofer (2-way crossover "
            "measurement is out of scope for bass management)"
        )
    normalized: dict[str, int] = {}
    for role, channel in role_channels.items():
        if type(channel) is not int or channel < 0:
            raise ActiveSpeakerConfigError(
                f"program channel for role {role!r} must be a non-negative integer"
            )
        normalized[role] = channel
    required = set(required_driver_roles(preset.way_count))
    output_roles = {output.driver_role for output in preset.channel_map.outputs}
    missing = (output_roles | required) - set(normalized)
    if missing:
        raise ActiveSpeakerConfigError(
            "program role_channels is missing a channel for role(s) "
            + ", ".join(sorted(missing))
        )
    if len(set(normalized.values())) != len(normalized):
        raise ActiveSpeakerConfigError(
            "each driver role must own a distinct program channel"
        )
    channels = sorted(normalized.values())
    if channels != list(range(len(channels))):
        raise ActiveSpeakerConfigError(
            "program channels must be contiguous from 0"
        )
    return normalized


def _assert_tweeter_crossover_hp_satisfies_floor(
    preset: ActiveSpeakerPreset,
    *,
    min_corner_hz: float,
    min_slope_db_per_octave: float,
) -> None:
    """Refuse a preset whose tweeter crossover HP would violate the protective floor.

    In the program graph the tweeter is protected by its TARGET crossover
    high-pass alone (the extra bring-up protective HP is dropped so the measured
    branch is the applied crossover shoulder — design §5.4). That is only safe
    when the crossover Fc / slope already satisfies the declared protective-HP
    floor. This build-time gate proves it from the preset BEFORE any YAML is
    emitted, so a preset that crosses the tweeter too low (or too gently) is
    refused loudly rather than measured behind an under-protective high-pass.
    """
    for role in required_driver_roles(preset.way_count):
        if role != "tweeter":
            continue
        crossover = crossover_highpass_for_role(preset, role)
        if crossover is None:
            raise ActiveSpeakerConfigError(
                "program graph requires a tweeter crossover high-pass; the "
                f"preset declares none for role {role!r}"
            )
        _name, fc_hz, order = crossover
        if fc_hz < min_corner_hz:
            log_event(
                logger,
                "active_speaker.program_emit_gate",
                level=logging.ERROR,
                result="blocked_tweeter_hp_below_floor",
                preset_id=preset.preset_id,
                fc_hz=f"{fc_hz:g}",
                min_corner_hz=f"{min_corner_hz:g}",
            )
            raise ActiveSpeakerConfigError(
                f"tweeter crossover high-pass corner {fc_hz:g} Hz is below the "
                f"declared protective floor {min_corner_hz:g} Hz"
            )
        if order * 6.0 < min_slope_db_per_octave:
            log_event(
                logger,
                "active_speaker.program_emit_gate",
                level=logging.ERROR,
                result="blocked_tweeter_hp_slope_below_floor",
                preset_id=preset.preset_id,
                order=order,
                min_slope_db_per_octave=f"{min_slope_db_per_octave:g}",
            )
            raise ActiveSpeakerConfigError(
                f"tweeter crossover high-pass slope {order * 6.0:g} dB/oct is "
                f"below the declared protective floor "
                f"{min_slope_db_per_octave:g} dB/oct"
            )


def _assert_pipeline_references_closed(
    yaml_text: str, preset: ActiveSpeakerPreset
) -> None:
    """Fail-closed L0 emit gate: refuse a graph whose pipeline points at an
    undefined mixer or filter name.

    Runs on every active-speaker graph THIS module emits, right before it is
    returned or written — the same "prove it against the emitted text" shape as
    :func:`_assert_tweeter_outputs_protected`, but a structural check rather
    than a protection one: it does not reason about channels or filter
    parameters, only whether every ``Mixer.name``/``Filter.names`` entry the
    pipeline references resolves against the graph's own ``mixers:``/
    ``filters:`` sections.

    This closes the W6 hardware run 4 finding I hole: the program graph's
    pipeline (reused verbatim from ``_emit_commissioning_pipeline``) named its
    routing mixer ``split_active_2way`` while the graph's own ``mixers:``
    section defined ``program_route_2way`` — a mismatch CamillaDSP's
    ``SetConfig`` only caught at LOAD time on real hardware ("Use of missing
    mixer 'split_active_2way'"), and even then reported only the first
    dangling reference. Every emitter in this module composes its filter
    definitions, mixer, and pipeline from independent helper calls
    (see ``emit_active_speaker_program_config`` and
    ``emit_active_speaker_baseline_config``); nothing upstream of this gate
    proves the three pieces agree.
    """
    try:
        payload = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ActiveSpeakerConfigError(
            f"emitted active-speaker config did not parse as YAML: {e}"
        ) from e
    errors = pipeline_reference_closure_errors(payload)
    if not errors:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result="blocked_dangling_pipeline_reference",
        preset_id=preset.preset_id,
        detail="; ".join(errors),
    )
    raise ActiveSpeakerConfigError(
        "refusing to emit an active-speaker config whose pipeline references "
        "undefined mixer/filter name(s): " + "; ".join(errors)
    )


def _assert_program_graph_proven(
    yaml_text: str,
    preset: ActiveSpeakerPreset,
    *,
    min_corner_hz: float,
) -> None:
    """Build-and-prove the emitted program graph against graph_safety (fail-closed).

    Runs the reference-closure gate (:func:`_assert_pipeline_references_closed`)
    plus the three L0 tweeter proofs on the EMITTED text — the same evidence a
    later readback would inspect — and refuses to return a graph that does not
    prove all four. This is the program builder's return contract: it cannot
    emit a graph whose pipeline points at an undefined mixer/filter name, nor
    one whose tweeter output is not high-pass protected (against the declared
    floor) AND wrapped by its crossover high-pass + soft-clip limiter in one
    post-mixer step. A pre-split per-channel high-pass (which
    ``output_highpass_protected`` alone could false-PASS on the 2-way preset,
    where program ch1 numerically coincides with tweeter output 1) is rejected
    here because ``tweeter_guard_present`` requires the high-pass AND the limiter
    together on exactly the tweeter output channels.
    """
    _assert_pipeline_references_closed(yaml_text, preset)
    tweeter_channels = _channels_for_role(preset, "tweeter")
    if not tweeter_channels:
        return
    view = view_from_emitted_text(yaml_text)
    tweeter_set = set(tweeter_channels)
    unprotected = unprotected_tweeter_outputs(
        view, tweeter_channels=tweeter_set, min_corner_hz=min_corner_hz
    )
    highpass_ok = all(
        output_highpass_protected(
            view,
            channel=channel,
            allowed_channels=tweeter_set,
            min_corner_hz=min_corner_hz,
        )
        for channel in tweeter_channels
    )
    crossover = crossover_highpass_for_role(preset, "tweeter")
    guard_ok = crossover is not None and tweeter_guard_present(
        view,
        channels=tweeter_set,
        hp_name=crossover[0],
        limiter_name=_driver_limiter_name("tweeter"),
        limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
    )
    if unprotected or not highpass_ok or not guard_ok:
        log_event(
            logger,
            "active_speaker.program_emit_gate",
            level=logging.ERROR,
            result="blocked_unproven_program_graph",
            preset_id=preset.preset_id,
            unprotected=",".join(str(index + 1) for index in unprotected),
            highpass_ok=highpass_ok,
            guard_ok=guard_ok,
        )
        raise ActiveSpeakerConfigError(
            "refusing to emit a program graph whose tweeter output(s) are not "
            "provably high-pass protected and limiter-wrapped on the physical "
            "output channels"
        )


def emit_active_speaker_program_config(
    preset: ActiveSpeakerPreset,
    *,
    role_channels: dict[str, int],
    playback_device: str,
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
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Emit the static channel-routed program graph for CHECK/MEASURE playback.

    The v2 crossover conductor plays one 2-channel program WAV
    (``jasper.audio_measurement.program``) once through ``correction_substream``;
    ``role_channels`` maps each driver role to the program-WAV channel carrying
    its stimulus (ch0 → woofer, ch1 → tweeter — design §5.4). This graph:

    * routes each program channel to its driver's PHYSICAL output path via a
      role-routed mixer (:func:`_emit_role_routed_mixer`);
    * carries the TARGET crossover filter for each driver on its output channel
      (the LR low-pass for the woofer, the LR high-pass for the tweeter) plus the
      per-driver soft-clip limiter — the APPLIED_RESPONSE commissioning filter
      set, reused verbatim so the measured branch is the applied crossover
      shoulder and every tweeter output stays behind its high-pass;
    * keeps the software volume ceiling non-positive and stays static (no reload
      mid-program).

    Two fail-closed gates run before the graph can leave this function: a
    build-time proof that the tweeter crossover Fc/slope satisfies the declared
    protective floor (:func:`_assert_tweeter_crossover_hp_satisfies_floor`), and
    a build-and-prove of the emitted text against ``graph_safety``'s three L0
    tweeter proofs (:func:`_assert_program_graph_proven`). Like the sibling
    emitters it does not load or reload CamillaDSP and refuses the stereo outputd
    lane as a playback device.
    """

    preset.validate()
    # W2 scope gate: the conductor's program topology (2 program channels,
    # woofer/tweeter role routing, the repeat-pair drift estimator) is designed
    # for a 2-way crossover. A 3-way needs a designed reshape (program channel
    # count, mid-band MESM schedule, per-region alignment), not a silent
    # generalization of this emitter.
    if preset.way_count != 2:
        raise ActiveSpeakerConfigError(
            "the crossover-measurement program graph is scoped to 2-way presets; "
            f"way_count={preset.way_count} requires a designed program reshape"
        )
    role_channels = _validate_program_role_channels(preset, role_channels)
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
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    limiter_clip_limit_db = _finite_float(limiter_clip_limit_db, "limiter_clip_limit_db")
    protective_hp_min_corner_hz = _finite_float(
        protective_hp_min_corner_hz, "protective_hp_min_corner_hz"
    )
    protective_hp_min_slope_db_per_octave = _finite_float(
        protective_hp_min_slope_db_per_octave,
        "protective_hp_min_slope_db_per_octave",
    )
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    # Build-time proof: the tweeter's crossover HP alone protects it, so its
    # Fc/slope MUST satisfy the declared protective floor before we emit.
    _assert_tweeter_crossover_hp_satisfies_floor(
        preset,
        min_corner_hz=protective_hp_min_corner_hz,
        min_slope_db_per_octave=protective_hp_min_slope_db_per_octave,
    )

    output_count = _output_count(preset)
    program_channels = 1 + max(role_channels.values())
    # Every output is audible: a program never mutes a driver (the WAV silences
    # it by channel), so the per-output commission mask is all-unmuted at 0 dB.
    # Program headroom is the commissioning headroom (0 dB) so the effective-peak
    # ledger the session-volume plan / admission share is main_volume + program
    # peak with no hidden graph attenuation.
    audible = frozenset(range(output_count))
    filter_yaml = _emit_commissioning_filter_definitions(
        preset,
        startup_headroom_db=COMMISSIONING_HEADROOM_DB,
        limiter_clip_limit_db=limiter_clip_limit_db,
        audible_outputs=audible,
        audible_gain_db=0.0,
        filter_mode=APPLIED_RESPONSE_FILTER_MODE,
    )
    mixer_yaml = _emit_role_routed_mixer(preset, role_channels)
    pipeline_yaml = _emit_commissioning_pipeline(
        preset,
        filter_mode=APPLIED_RESPONSE_FILTER_MODE,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# role_channels={dict(sorted(role_channels.items()))}",
        f"# program_channels={program_channels}",
        f"# filter_mode={APPLIED_RESPONSE_FILTER_MODE}",
    ]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker crossover-measurement program config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_program_config
{metadata_yaml}
# DO NOT HAND-EDIT. Static channel-routed program graph: program capture channel
# c is routed to every physical output of role role_channels^-1(c), each carrying
# its TARGET crossover filter + soft-clip limiter. Played once (no reload
# mid-program) while a 2-channel program WAV sequences the driver stimuli by
# channel. The software volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: true
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
    _assert_tweeter_outputs_protected(yaml, preset)
    # Build-and-prove the program graph's return contract against graph_safety.
    _assert_program_graph_proven(
        yaml, preset, min_corner_hz=protective_hp_min_corner_hz
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


def emit_active_speaker_baseline_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    baseline_headroom_db: float = BASELINE_HEADROOM_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
    bass_extension_profile: BassExtensionProfile | None = None,
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
    # CamillaDSP latency knobs (G7): None → env-or-default at call time so a
    # JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} override applies on the next
    # regeneration. Unset env → the literal defaults (byte-identical YAML).
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
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

    safe_corrections = _validated_driver_corrections(preset, corrections)
    bass_extension = _bass_extension_emission(preset, bass_extension_profile)

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
        bass_extension=bass_extension,
    )
    # apply_region_polarity=False: this graph carries polarity through
    # ``safe_corrections`` (a per-driver Gain filter below), so the mixer must
    # stay a no-op inverter — see the docstring on _emit_split_mixer.
    mixer_yaml = _emit_split_mixer(preset, apply_region_polarity=False)
    pipeline_yaml = _emit_baseline_pipeline(
        preset,
        room_peq_names=[_room_peq_name(i) for i in range(1, len(room_peqs) + 1)],
        preference_filter_names=[spec.name for spec in active_preference_filters],
        bass_extension=bass_extension,
    )
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)
    capture_yaml = f"""  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}"""

    yaml = f"""---
# Auto-generated active-speaker baseline config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config
{metadata_yaml}
# This is a candidate speaker baseline: crossover filters are active, outputs
# are not startup-muted, per-driver correction gain is non-positive, and the
# software volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: true
{capture_yaml}
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

    # L0 emit gate (fail-closed): the durable (unmuted) baseline is the graph a
    # household actually plays through, so re-prove every tweeter output carries
    # its crossover / protective high-pass before it can leave the emitter — a
    # flat/unprotected tweeter baseline is the shrill hot-tweeter hazard.
    _assert_tweeter_outputs_protected(yaml, preset)
    _assert_bass_extension_safe(yaml, preset, bass_extension)
    # Reference-closure gate (fail-closed): the durable baseline shares the
    # same "filters/mixer/pipeline assembled from independent helper calls"
    # shape the program graph does (see the W6 finding I comment on
    # _assert_pipeline_references_closed) — cheap enough to run on every
    # baseline build, and it is what would have caught a dangling mixer/filter
    # name here before it ever reached a live CamillaDSP load.
    _assert_pipeline_references_closed(yaml, preset)

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
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
    bass_extension_profile: BassExtensionProfile | None = None,
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

    The pipeline is ``channel_select (2->2 pick L/R/mono) -> optional
    pair_balance_trim -> split_active_<way>way (2->N) -> per-driver chain``:
    the inter-speaker channel-select runs FIRST (which channel of the pair this
    box plays), THEN the attenuate-only pair trim for this physical member,
    THEN the intra-speaker driver split — exactly
    ``jasper.multiroom.channel_split``'s documented composition order.
    ``program_channel`` is one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``
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
    pair_trim_db = _finite_float(pair_trim_db, "pair_trim_db")
    if pair_trim_db < 0.0 or pair_trim_db > 120.0:
        raise ActiveSpeakerConfigError("pair_trim_db must be between 0 and 120 dB")
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    # CamillaDSP latency knobs (G7): None → env-or-default at call time so a
    # JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} override applies on the next
    # regeneration. Unset env → the literal defaults (byte-identical YAML).
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    # S1: floor the loopback capture chunksize (see FOLLOWER_LOOPBACK_MIN_CHUNKSIZE).
    # Clamp rather than raise — mirrors the knob's malformed->default leniency —
    # and unset env resolves to 1024, so this is a no-op on the default path.
    if chunksize < FOLLOWER_LOOPBACK_MIN_CHUNKSIZE:
        chunksize = FOLLOWER_LOOPBACK_MIN_CHUNKSIZE
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

    safe_corrections = _validated_driver_corrections(preset, corrections)
    bass_extension = _bass_extension_emission(preset, bass_extension_profile)

    output_count = _output_count(preset)
    filter_lines = _emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
        bass_extension=bass_extension,
    )
    filter_lines.extend(emit_gain_filter("pair_balance_trim", -pair_trim_db))
    filter_yaml = "\n".join(filter_lines)
    # channel_select FIRST (inter-speaker pick), then the intra-speaker split.
    # apply_region_polarity=False: this graph carries polarity through
    # ``safe_corrections`` (a per-driver Gain filter above), so the mixer must
    # stay a no-op inverter — see the docstring on _emit_split_mixer.
    mixer_yaml = "\n".join((
        emit_channel_select_mixer(program_channel),
        _emit_split_mixer(preset, apply_region_polarity=False),
    ))
    pipeline_yaml = _emit_driver_domain_pipeline(
        preset,
        pair_trim_db=pair_trim_db,
        bass_extension=bass_extension,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# program_channel={program_channel}",
        f"# pair_trim_db={pair_trim_db:.3f}",
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
# volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
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

    # L0 emit gate (fail-closed): the follower runs Layer A (the split + per-driver
    # crossover chain) on the leader's corrected program, so its tweeter output
    # must still carry the crossover / protective high-pass — re-prove it before
    # the graph leaves the emitter.
    _assert_tweeter_outputs_protected(yaml, preset)
    _assert_bass_extension_safe(yaml, preset, bass_extension)

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
    chunksize: int | None = None,
    target_level: int | None = None,
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
