# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.camilla_config_contract import DRIVER_DOMAIN_PAIR_TRIM_FILTER
from jasper.camilla_emit import CHANNEL_SELECT_MIXER, emit_mixer, mono_sum_sources
from jasper.fanin_coupling import RING_A_CHANNELS
from jasper.speaker_layout import measurement_target_id

from ..camilla_names import (
    bass_management_hp_name,
    driver_baseline_gain_name,
    driver_baseline_limiter_name,
    driver_delay_name,
    driver_limiter_name,
    output_commission_mute_name,
    protective_tweeter_hp_name,
    sub_baseline_gain_name,
    sub_baseline_limiter_name,
    sub_lowpass_name,
    sub_startup_limiter_name,
)
from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles

if TYPE_CHECKING:
    from ..branch_chain import CrossoverSection
from .devices import _finite_float
from .filters import (
    APPLIED_RESPONSE_FILTER_MODE,
    COMMISSIONING_FILTER_MODE,
    _crossover_filter_name,
    _driver_linearization_chain_names,
    _driver_mute_name,
    _program_protection_name,
    _protective_tweeter_hp_frequency,
    _sub_startup_mute_name,
)
from .topology import (
    _bass_management_active,
    _channels_for_role,
    _ordered_regions,
    _output_count,
    role_polarity,
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
    # Only its RESULT is optionally suppressed: the baseline/driver-domain
    # emitters carry polarity through ``corrections`` instead, so the mixer must
    # stay a no-op inverter there or the two would cancel out.
    region_polarity = role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    # The (dest -> L/R-sum sources) map comes from the preset's driver layout
    # plus per-driver polarity; the YAML spelling is the shared emit_mixer.
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
        # The local subwoofer taps the SAME full-range program as the mains,
        # mono-summed with the clip-safe -6.02 dB recipe. Its band-limiting
        # low-pass and excursion limiter live in the per-output pipeline chain.
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


# The inter-speaker channel-select mixer name, owned by the shared leaf
# (jasper.camilla_emit) and re-exported so the active-speaker verifier has one
# import point.
channel_select_mixer_name = CHANNEL_SELECT_MIXER


def _driver_filter_chain(preset: ActiveSpeakerPreset, role: str) -> list[str]:
    names: list[str] = []
    if _bass_management_active(preset, role):
        names.append(bass_management_hp_name(role))
    protective_freq = _protective_tweeter_hp_frequency(preset, role)
    if protective_freq is not None:
        names.append(protective_tweeter_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    names.append(driver_delay_name(role))
    names.append(_driver_mute_name(role))
    names.append(driver_limiter_name(role))
    return names


def _driver_baseline_filter_chain(
    preset: ActiveSpeakerPreset,
    role: str,
    linearization: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    names: list[str] = []
    # Bass-management high-pass FIRST: the lowest driver's program is
    # high-passed at the sub crossover corner before its own chain. The sub
    # low-pass at the same corner is the complementary lower half.
    if _bass_management_active(preset, role):
        names.append(bass_management_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    # Layer-1a driver linearization: immediately after the crossover HP/LP,
    # before bass-extension. Empty linearization is a no-op.
    names.extend(
        _driver_linearization_chain_names(linearization or {}, role)
    )
    names.append(driver_delay_name(role))
    names.append(driver_baseline_gain_name(role))
    names.append(driver_baseline_limiter_name(role))
    return names


def _sub_baseline_filter_chain(
) -> list[str]:
    """The local-sub baseline lane: band-limit (LR4 low-pass), then the same
    per-driver protection a main gets (non-positive gain + soft-clip limiter)."""
    return [
        sub_lowpass_name(),
        sub_baseline_gain_name(),
        sub_baseline_limiter_name(),
    ]


def _sub_startup_filter_chain() -> list[str]:
    """The local-sub startup lane: band-limit, limiter, then the hard mute."""
    return [
        sub_lowpass_name(),
        sub_startup_limiter_name(),
        _sub_startup_mute_name(),
    ]


def _sub_commissioning_filter_chain() -> list[str]:
    """The local-sub commissioning lane: band-limit + excursion limiter only.

    The per-output commission mute replaces the lane's own startup mute, so
    exactly one physical output is excited through the real graph; the low-pass
    and limiter stay so the output is protected when that mute is lifted."""
    return [
        sub_lowpass_name(),
        sub_startup_limiter_name(),
    ]


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
    linearization: dict[str, list[dict[str, Any]]] | None = None,
    blend_correction_names: Sequence[str] = (),
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
    # Crossover blend correction — pre-split, and only pre-split, for three
    # properties:
    #
    #  1. ONE summed fact, ONE filter. The correction describes the SUM;
    #     per-role emission would be N copies whose only defence is a test.
    #  2. Common-mode by construction. The same B(f) on every role gives
    #     Σ_r sign_r·B·C_r·D_r = B · Σ_r sign_r·C_r·D_r — the sum scales, the
    #     inter-driver complex ratio is untouched. Asymmetry (which would be
    #     ALIGNMENT work) is unrepresentable here rather than merely tested for.
    #  3. Upstream of protection. In the durable baseline the tweeter's
    #     crossover high-pass IS its protection; a pre-split filter cannot push
    #     energy past it.
    #
    # BEFORE active_baseline_headroom so the stage sits where a boost WOULD be
    # absorbable — necessary but not sufficient, since absorption needs a TERM in
    # ``total_headroom_db`` and this stage deliberately has none.
    if blend_correction_names:
        names = ", ".join(blend_correction_names)
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
    # Preference EQ (Layer C) is a PROGRAM-domain transform: it rides the stereo
    # bus on channels [0, 1] strictly BEFORE the split mixer, upstream of every
    # per-driver crossover, limiter and tweeter high-pass. That placement is what
    # makes a preference boost safe — it can neither move a crossover corner nor
    # bypass a driver limiter.
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
            _driver_baseline_filter_chain(
                preset, role, linearization,
            )
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset))
    return "\n".join(lines)


def _sub_baseline_pipeline_lines(
    preset: ActiveSpeakerPreset,
) -> list[str]:
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
    # Driver-domain-only (follower) pipeline, in order: the inter-speaker
    # channel-select (a 2->2 Mixer picking L/R/mono from the leader's corrected
    # program), the pair-balance trim, the intra-speaker 2->N split, then each
    # driver's crossover/delay/gain/limiter chain. One helper owns this
    # ordering so the bass-extension-trimmed and untrimmed cases cannot fork.
    lines = [
        "  - type: Mixer",
        f"    name: {CHANNEL_SELECT_MIXER}",
    ]
    lines.extend([
        "  - type: Filter",
        "    channels: [0, 1]",
        f"    names: [{DRIVER_DOMAIN_PAIR_TRIM_FILTER}]",
    ])
    lines.extend([
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ])
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(
            _driver_baseline_filter_chain(preset, role)
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset))
    return "\n".join(lines)


def _commissioning_driver_filter_chain(
    preset: ActiveSpeakerPreset,
    role: str,
    *,
    filter_mode: str,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    measurement_delay_roles: frozenset[str] = frozenset(),
) -> list[str]:
    """The startup chain minus the per-role mute.

    Commissioning isolates one *physical output* (not a whole role — a stereo
    woofer pair shares a role), so the role-level startup mute is dropped and a
    per-output mute layer is applied in the pipeline instead. Bring-up retains
    the dedicated tweeter high-pass; automatic response measurement removes only
    that extra filter so it measures the applied crossover shoulder.

    ``measurement_delay_roles`` names the roles that carry a ``Delay`` at the
    head of the chain. Position is free (a pure delay is LTI and commutes with
    every stage here); the applied chains place theirs after the crossover.
    """
    if protection_sections_by_role is not None:
        return [
            *([driver_delay_name(role)] if role in measurement_delay_roles else []),
            *(
                _program_protection_name(role, index)
                for index, _section in enumerate(protection_sections_by_role[role])
            ),
            driver_limiter_name(role),
        ]
    excluded = {_driver_mute_name(role)}
    if filter_mode == APPLIED_RESPONSE_FILTER_MODE:
        excluded.add(protective_tweeter_hp_name(role))
    return [name for name in _driver_filter_chain(preset, role) if name not in excluded]


def _emit_commissioning_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    measurement_delay_roles: frozenset[str] = frozenset(),
    capture_channels: int = 2,
) -> str:
    lines = [
        "  - type: Filter",
        f"    channels: [{', '.join(str(ch) for ch in range(capture_channels))}]",
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
                protection_sections_by_role=protection_sections_by_role,
                measurement_delay_roles=measurement_delay_roles,
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


def _validated_inverted_roles(
    preset: ActiveSpeakerPreset, inverted_roles: Sequence[str],
) -> frozenset[str]:
    """The reverse-null's named branches, refused unless this cabinet has them.

    Fail-closed for the same reason :func:`_validate_program_role_channels` is:
    a role no output declares would flip nothing, the graph would emit
    byte-identical to its non-inverted twin, and the banked record would claim
    a reverse-null nobody measured.
    """
    flipped = frozenset(inverted_roles)
    declared = {output.driver_role for output in preset.channel_map.outputs}
    unknown = flipped - declared
    if unknown:
        raise ActiveSpeakerConfigError(
            "cannot invert driver role(s) this preset declares no output for: "
            + ", ".join(sorted(unknown))
        )
    return flipped


def _validated_measurement_trims(
    preset: ActiveSpeakerPreset, trims_db: Mapping[str, float] | None,
) -> dict[str, float]:
    """The measurement's per-role level match, refused unless it can be honoured.

    Fail-closed for :func:`_validated_inverted_roles`'s reason: a trim naming a
    role no output declares would attenuate nothing while the banked record
    claimed a level match nobody played.

    **Attenuation only** — a positive value is refused rather than clamped,
    because this is the one seam that could raise a measurement's drive above
    the level the session was admitted at. Every hearing clamp is untouched:
    ``volume_limit``, the per-driver limiter and the tweeter protection
    high-pass are downstream of this mixer and unreachable from here.
    """
    if not trims_db:
        return {}
    declared = {output.driver_role for output in preset.channel_map.outputs}
    validated: dict[str, float] = {}
    for role, value in trims_db.items():
        if role not in declared:
            raise ActiveSpeakerConfigError(
                "cannot level-match a driver role this preset declares no "
                f"output for: {role}"
            )
        trim_db = _finite_float(value, f"measurement level trim for {role}")
        if trim_db > 0.0:
            raise ActiveSpeakerConfigError(
                "a measurement level trim is attenuation only; "
                f"{role} asked for {trim_db:g} dB"
            )
        validated[role] = trim_db
    return validated


def program_channel_count(role_channels: Mapping[str, int]) -> int:
    """A program graph's capture width: every channel the program routes, and
    never fewer than Ring A carries.

    Capture is always Ring A (:func:`~.devices.capture_device_for_playback`),
    whose ioplug accepts only its own width, so a take routing one channel
    still captures the whole ring; its unrouted channel reaches no output.
    """
    return max(RING_A_CHANNELS, 1 + max(role_channels.values()))


def _emit_role_routed_mixer(
    preset: ActiveSpeakerPreset,
    role_channels: dict[str, int],
    *,
    apply_region_polarity: bool = True,
    inverted_roles: Sequence[str] = (),
    level_trims_db: Mapping[str, float] | None = None,
) -> str:
    """Emit the program graph's role-routed split mixer.

    ``inverted_roles`` is the measurement's reverse-null flip: each named role's
    sign is reversed RELATIVE to whatever polarity this graph would otherwise
    carry, so it XORs onto the region polarity rather than replacing it. It is
    level-neutral by construction — every ``dest`` here has exactly ONE source,
    so flipping ``inverted`` negates each sample and leaves every peak the
    limiter and the volume ceiling answer for bit-identical.

    ``level_trims_db`` is the ONE thing that moves a ``gain``: each named role's
    single source is attenuated so the branches meet the crossover at comparable
    level and a reverse null can form. Attenuation only
    (:func:`_validated_measurement_trims`), so every peak can only fall.

    Unlike :func:`_emit_split_mixer` (which routes a stereo bus by output
    *side*), this routes by PHYSICAL TARGET: a primary output's key is its role,
    a variant output's is ``role:variant`` (``woofer:rear``, ADR-0316). An
    output the map does not name is parked for this take — its dest carries no
    source, which is silence. A rear is never reached by its role's entry: an
    unfitted rear ends in a terminal mute, and routing signal into a muted
    output would record silence as if it were a measurement.
    ``channels_in`` is :func:`program_channel_count`.

    The mixer is named ``split_active_{way_count}way`` — the SAME name
    :func:`_emit_split_mixer` uses — for two reasons landing on one spelling:
    :func:`_emit_commissioning_pipeline`, reused verbatim here, hardcodes a
    ``Mixer`` step under that name (CamillaDSP refuses a pipeline referencing an
    undefined mixer), and ``environment``'s active-config classifier recognises
    a ``split_active_Nway`` name. Ecosystem vocabulary, not a routing claim: the
    ROUTING stays role-routed.
    """
    region_polarity = role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
    flipped = _validated_inverted_roles(preset, inverted_roles)
    trims = _validated_measurement_trims(preset, level_trims_db)
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    channels_in = program_channel_count(role_channels)
    mapping: list[tuple[int, list[tuple[int, float, bool]]]] = []
    for output in outputs:
        role = output.driver_role
        channel = role_channels.get(
            measurement_target_id(role, output.output_variant)
        )
        mapping.append((output.index, [] if channel is None else [(
            channel, trims.get(role, 0.0), polarity[role] != (role in flipped),
        )]))
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
