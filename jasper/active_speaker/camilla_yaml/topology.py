# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from ..profile import (
    ADJACENT_PAIRS_BY_WAY,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    lowest_driver_role,
    required_driver_roles,
)


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


# Public spelling; `_role_polarity` survives only for two importers outside
# this PR's ratified file set (retiring it is a follow-up).
role_polarity = _role_polarity


def _channels_for_role(preset: ActiveSpeakerPreset, role: str) -> list[int]:
    return sorted(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == role
    )


def _bass_management_active(preset: ActiveSpeakerPreset, role: str) -> bool:
    """True iff ``role`` is the lowest driver AND a local sub is present — the
    side whose lowest driver carries the complementary bass-management high-pass."""
    return (
        preset.local_subwoofer is not None
        and role == lowest_driver_role(preset.way_count)
    )


def _output_count(preset: ActiveSpeakerPreset) -> int:
    indexes = [output.index for output in preset.channel_map.outputs]
    if preset.local_subwoofer is not None:
        indexes.append(preset.local_subwoofer.physical_output_index)
    return max(indexes) + 1


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
