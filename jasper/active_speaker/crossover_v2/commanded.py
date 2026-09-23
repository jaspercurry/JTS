# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The applied profile's crossover regions and corner."""
from __future__ import annotations

import math
from typing import Any, Mapping

from jasper.active_speaker.branch_chain import sections_by_role
from jasper.active_speaker.profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, CrossoverRegion

from .contracts import CandidateAcousticContext, CrossoverV2ContractError

__all__ = [
    "profile_crossover_fc_hz",
    "profile_crossover_regions",
]


def profile_crossover_regions(profile: Mapping[str, Any] | None) -> tuple[CrossoverRegion, ...]:
    """The applied snapshot's parsed crossover regions, empty when unavailable."""
    snapshot = profile.get("recomposition_snapshot") if isinstance(profile, Mapping) else None
    raw = snapshot.get("preset") if isinstance(snapshot, Mapping) else None
    if not isinstance(raw, Mapping):
        return ()
    try:
        return ActiveSpeakerPreset.from_mapping(dict(raw)).crossover_regions
    except (ActiveSpeakerConfigError, AttributeError, KeyError, TypeError, ValueError):
        return ()


def profile_crossover_fc_hz(profile: Mapping[str, Any] | None) -> float | None:
    """One applied crossover corner, or ``None`` for absent, invalid or split sections."""
    try:
        fc_hz = float(CandidateAcousticContext.from_sections(sections_by_role(profile_crossover_regions(profile))).fc_hz)
    except (CrossoverV2ContractError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return fc_hz if math.isfinite(fc_hz) and fc_hz > 0.0 else None
