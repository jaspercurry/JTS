# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Intersect two adjacent driver targets into excitation admission values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from jasper.audio_measurement.evidence_identity import (
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
)
from jasper.output_topology import OutputTopology

from .driver_safety import evaluate_driver_safety_profile
from .excitation_safety_plan import declared_level_ceiling_dbfs
from .measurement import active_driver_targets
from .profile import ADJACENT_PAIRS_BY_WAY
from .test_signal_plan import (
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    SUMMED_SWEEP_DURATION_S,
)


class CommissioningRuntimeError(ValueError):
    """A runtime request or one live observation is malformed."""


@dataclass(frozen=True)
class PreparedSummedExcitation:
    """Thin two-target reduction into Shared's existing admission values."""

    target_fingerprints: tuple[str, str]
    request: ExcitationRequest
    limits: ExcitationLimits
    minimum_cooldown_s: float


def prepare_summed_excitation(
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    *,
    target_fingerprints: tuple[str, str],
    evidence_target_fingerprint: str,
    band: FrequencyBand,
    effective_peak_dbfs: float,
    duration_s: float,
    excitation_plan_fingerprint: str,
) -> PreparedSummedExcitation:
    """Intersect two current adjacent driver policies for one-repeat playback."""

    if not isinstance(topology, OutputTopology):
        raise CommissioningRuntimeError("topology must be OutputTopology")
    evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not evaluation.confirmed_and_current or evaluation.profile_fingerprint is None:
        raise CommissioningRuntimeError("driver safety profile is not current")
    if (
        type(target_fingerprints) is not tuple
        or len(target_fingerprints) != 2
        or len(set(target_fingerprints)) != 2
    ):
        raise CommissioningRuntimeError(
            "target_fingerprints must name two distinct adjacent drivers"
        )
    if (
        not isinstance(evidence_target_fingerprint, str)
        or len(evidence_target_fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in evidence_target_fingerprint)
    ):
        raise CommissioningRuntimeError(
            "evidence_target_fingerprint must be a lowercase SHA-256"
        )
    current_by_fingerprint = {
        target["target_fingerprint"]: target for target in active_driver_targets(topology)
    }
    current = [current_by_fingerprint.get(fingerprint) for fingerprint in target_fingerprints]
    if any(target is None for target in current):
        raise CommissioningRuntimeError("summed targets are not current")
    current_targets = cast(list[dict[str, Any]], current)
    if current_targets[0]["speaker_group_id"] != current_targets[1]["speaker_group_id"]:
        raise CommissioningRuntimeError("summed targets must share one speaker group")
    group_id = current_targets[0]["speaker_group_id"]
    group = next((item for item in topology.speaker_groups if item.id == group_id), None)
    if group is None:
        raise CommissioningRuntimeError("summed speaker group is not current")
    way_count = 2 if group.mode == "active_2_way" else 3
    roles = tuple(target["role"] for target in current_targets)
    if roles not in ADJACENT_PAIRS_BY_WAY[way_count]:
        raise CommissioningRuntimeError("summed driver targets must be adjacent")

    profile_targets = safety_profile.get("targets")
    if not isinstance(profile_targets, list):
        raise CommissioningRuntimeError("driver safety profile targets are missing")
    profile_by_fingerprint = {
        item.get("target_fingerprint"): item
        for item in profile_targets
        if isinstance(item, Mapping)
    }
    targets = [profile_by_fingerprint.get(fingerprint) for fingerprint in target_fingerprints]
    if any(not isinstance(target, Mapping) for target in targets):
        raise CommissioningRuntimeError("driver safety profile targets are stale")
    typed_targets = cast(list[Mapping[str, Any]], targets)
    try:
        # ``measurement_band_hz[0]`` is the whole low edge here; this used to
        # ALSO take a max over ``hard_excitation_band_hz[0]``, which restated a
        # rule this module does not own and could never bind.
        #
        # The short argument, and it needs nothing from #2603: every confirmed
        # profile satisfies ``_band_subset(measurement, hard)``, because
        # ``driver_safety._target_issues`` raises
        # ``<role>:measurement_band_outside_hard_band`` otherwise and
        # ``evaluate_driver_safety_profile`` turns any derived issue into a
        # NOT-confirmed verdict -- which the ``confirmed_and_current`` gate
        # above already refused. Subset means ``measurement[0] >= hard[0]``, so
        # the ``hard[0]`` term was dominated on every reachable input.
        #
        # #2603 adds a second, independent guarantee for the same inequality:
        # ``apply_driver_low_limit`` stamps ``hard[0]`` at the declared low
        # limit and ``measurement[0]`` at ``max(published response floor, that
        # limit)``.
        #
        # ``excitation_safety_plan.resolve_driver_excitation_ceilings`` keeps its
        # own ``hard[0]`` term and is not wrong to: on its proven-HP
        # high-frequency branch it EXCLUDES ``measurement[0]``, so there
        # ``hard[0]`` is the binding edge. The summed path has no such branch.
        lower_hz = max(
            MIN_DRIVER_TEST_FREQUENCY_HZ,
            *(float(target["measurement_band_hz"][0]) for target in typed_targets),
        )
        upper_hz = min(
            MAX_DRIVER_TEST_FREQUENCY_HZ,
            *(float(target["hard_excitation_band_hz"][1]) for target in typed_targets),
        )
        upper_hz = min(
            upper_hz,
            *(float(target["measurement_band_hz"][1]) for target in typed_targets),
        )
        limits = [cast(Mapping[str, Any], target["level_duration_limits"]) for target in typed_targets]
        # ``max_effective_peak_dbfs`` is OPTIONAL, so it is read through the one
        # owner of "what is this target's level ceiling" rather than indexed
        # here (2026-08-23). Indexing it made the ordinary reply shape -- a
        # driver whose maker publishes no level limit -- raise KeyError and
        # surface as "limits are incomplete", which described nothing wrong.
        max_peak = min(
            declared_level_ceiling_dbfs(target)[0] for target in typed_targets
        )
        max_duration = min(
            SUMMED_SWEEP_DURATION_S,
            *(float(item["max_sweep_duration_s"]) for item in limits),
        )
        cooldown = max(float(item["minimum_cooldown_s"]) for item in limits)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CommissioningRuntimeError(
            "driver safety profile target limits are incomplete"
        ) from exc
    if lower_hz > upper_hz:
        raise CommissioningRuntimeError("adjacent driver measurement bands do not overlap")
    permitted_band = FrequencyBand(lower_hz, upper_hz)
    if not isinstance(band, FrequencyBand):
        raise CommissioningRuntimeError("band must be FrequencyBand")
    requirement_fingerprint = json_fingerprint(
        {
            "schema_version": 1,
            "kind": "jts_active_summed_protection_requirement",
            "target_fingerprints": list(target_fingerprints),
            "required_filters": [
                target.get("required_protection_filters") for target in typed_targets
            ],
        }
    )
    authority = ExcitationLimits(
        permitted_band=permitted_band,
        maximum_effective_peak_dbfs=max_peak,
        maximum_duration_s=max_duration,
        maximum_repeat_count=1,
        target_fingerprint=evidence_target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        protection_requirement_fingerprint=requirement_fingerprint,
        excitation_plan_fingerprint=excitation_plan_fingerprint,
    )
    request = ExcitationRequest(
        band=band,
        effective_peak_dbfs=effective_peak_dbfs,
        duration_s=duration_s,
        repeat_count=1,
        target_fingerprint=evidence_target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        authority_fingerprint=authority.fingerprint,
        excitation_plan_fingerprint=excitation_plan_fingerprint,
    )
    return PreparedSummedExcitation(
        target_fingerprints=target_fingerprints,
        request=request,
        limits=authority,
        minimum_cooldown_s=cooldown,
    )
