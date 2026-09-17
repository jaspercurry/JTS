# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve the plan's bass stimulus against the driven targets' declared caps."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Mapping, TYPE_CHECKING

from jasper.audio_measurement.distortion import required_pre_guard_s, segment_sweep_meta
from jasper.audio_measurement.program import ExcitationProgram, PILOT_AMBIENT_WINDOW_S
from jasper.audio_measurement.repeated_sweep import repeat_summed_program
from jasper.audio_measurement.sweep_levels import sweep_band_sample_ranges

from .bass_fit import REFERENCE_BAND_HZ
from .excitation_safety_plan import (
    ACTIVE_DRIVER_MAX_REPEAT_COUNT,
    declared_minimum_cooldown_s,
    effective_sweep_duration_limit_s,
)
from .measurement_bass import BASS_BANDS_HZ

if TYPE_CHECKING:
    from .crossover_v2.programs import SessionExcitation


class BassStimulusRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_bass_program(
    excitation: SessionExcitation, stimulus: Mapping[str, Any], *,
    safety_profile: Mapping[str, Any], role_targets: Mapping[str, str],
    extra_backoff_db: float = 0.0, courtesy_prelude: bool = True,
) -> ExcitationProgram:
    targets = {target["target_fingerprint"]: target for target in safety_profile.get("targets", ())}
    # role_targets keys are measurement target ids (ADR-0316): a rear variant
    # is a second physical target of the SAME acoustic role, not a third one.
    target_roles = {target_id.split(":", 1)[0] for target_id in role_targets}
    if "woofer" not in role_targets or {role.role for role in excitation.roles} != target_roles:
        raise BassStimulusRefused("bass_stimulus_targets_missing")
    try:
        driven = [targets[fingerprint] for fingerprint in role_targets.values()]
        woofer = targets[role_targets["woofer"]]
        floor, ceiling = float(woofer["hard_excitation_band_hz"][0]), float(stimulus["ceiling_hz"])
        limits = [target["level_duration_limits"] for target in driven]
        passes = min(ACTIVE_DRIVER_MAX_REPEAT_COUNT, *(limit["max_repeat_count"] for limit in limits))
        cooldown = declared_minimum_cooldown_s(safety_profile, role_targets)
        durations = {role: effective_sweep_duration_limit_s(safety_profile, fingerprint)
                     for role, fingerprint in role_targets.items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise BassStimulusRefused("bass_stimulus_caps_missing") from exc
    if (not 0 < floor <= REFERENCE_BAND_HZ[0] < REFERENCE_BAND_HZ[1] < ceiling
            or ceiling > float(woofer["hard_excitation_band_hz"][1])):
        raise BassStimulusRefused("bass_stimulus_band_outside_limits")
    single = replace(excitation, summed_sweep_band_hz=(floor, ceiling), sweep_duration_limits_s=durations).verify_program(
        sweep_s=min(durations.values()), extra_backoff_db=extra_backoff_db,
        courtesy_prelude=courtesy_prelude, leading_pilots=False,
    )
    sweep = segment_sweep_meta(single.segment("sweep_verify"))
    ranges = sweep_band_sample_ranges(sweep, single.sample_rate_hz, BASS_BANDS_HZ)
    if not ranges:
        raise BassStimulusRefused("bass_stimulus_no_bands")
    quiet = max(math.ceil(PILOT_AMBIENT_WINDOW_S * single.sample_rate_hz),
                5 * max(stop - start for _, _, start, stop in ranges),
                math.ceil(required_pre_guard_s(sweep) * single.sample_rate_hz))
    return repeat_summed_program(single, passes=passes, quiet_samples=quiet, cooldown_s=cooldown)
