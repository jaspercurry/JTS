# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The resolved inputs a crossover-v2 conductor session opens with.

A value type, kept out of the host whose ``resolve_conductor_context`` writes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["V2ConductorContext"]


@dataclass(frozen=True)
class V2ConductorContext:
    """Everything the production conductor needs, resolved from live status."""

    preset: Any
    roles_bands: tuple
    #: The declared crossover corner, or ``None`` on a 1-way main, which has
    #: none. Never a stand-in figure — see ``resolve_conductor_context``.
    fc_hz: float | None
    driver_caps_dbfs: dict[str, float]
    # Per-role longest admissible ONE sweep, in seconds, from the SAME owner
    # the admission gate reads (``effective_sweep_duration_limit_s``), so a
    # MEASURE segment cannot overshoot the ceiling admission judges it against.
    driver_sweep_duration_limits_s: dict[str, float]
    role_targets: dict[str, str]
    safety_profile: Mapping[str, Any]
    session_volume_db: float
    driver_spacing_m: float
    topology: Any
    playback_device: str
    role_channels: dict[str, int]
    sound_design_revision: int
    # Per-role declared EFFECTIVE sensitivities in dB SPL/2.83V @1m (the
    # datasheet figure with any declared in-line pad folded in), from the
    # design draft, which owns that fact. Threaded into every cap resolution
    # AND the play-time readmission so the composed levels and the admission
    # gate cannot disagree about a derived HF ceiling.
    declared_sensitivities: dict[str, float] = field(default_factory=dict)
    # Per-role declared driver technology class, feeding the conductor's
    # Layer-1a linearization fit (``linearization_envelope.compose_envelope``'s
    # class_prior_limit term). A role absent here fits under the conservative
    # "unknown" class default.
    driver_class_by_role: dict[str, str] = field(default_factory=dict)
    # Per-role declared effective radiating diameter in mm, the ka/beaming
    # prior, which is DISCLOSURE and never a bound. It reaches the conductor by
    # the SAME draft path ``driver_class_by_role`` takes. A role absent here
    # gets no beaming prior, disclosed as such rather than an assumed diameter.
    radiating_diameter_mm_by_role: dict[str, float] = field(default_factory=dict)
    # Per-role confirmed ``measurement_band_hz`` in Hz — the contract-derived
    # echo/null analysis band the cloud-group pipeline reads in place of
    # DEFAULT_ECHO_BAND_HZ's flat constant. A role missing here degrades to
    # that module default, never a refused session: a declared-metadata gap is
    # not a reason to block a measurement the household is entitled to run.
    measurement_band_hz_by_role: Mapping[str, tuple[float, float]] = field(
        default_factory=dict
    )
