# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compose candidate interventions without carrying their parents' measurement claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .branch_chain import branch_headroom_db, sections_by_role
from .candidate_bank import BankedCandidate, CandidateBankRefusal
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    compile_candidate_config,
    prove_candidate_config,
)

COMPOSITION_KIND = "jts_candidate_composition"


def _source(parent: BankedCandidate) -> dict[str, str]:
    return {"fingerprint": parent.fingerprint, "artifact_path": str(parent.path)}


def compose_candidate(
    base: BankedCandidate,
    roles: Mapping[str, BankedCandidate],
    *,
    alignment: BankedCandidate | None = None,
    blend: BankedCandidate | None = None,
    expected_effect: str = "",
    observation_refs: Sequence[str] = (),
    rationale: str = "",
) -> MeasuredCrossoverCandidate:
    """Replace each selected role's filters and trim; retain other base settings."""
    preset = base.candidate.source_preset
    sources = {role: roles.get(role, base) for role in base.candidate.role_attenuations_db}
    if set(roles) - set(sources):
        raise CandidateBankRefusal("composition_role_unknown", "role is not in the base preset")
    alignment, blend = alignment or base, blend or base
    for source in (*sources.values(), alignment, blend):
        if source.candidate.source_preset != preset:
            raise CandidateBankRefusal(
                "composition_preset_mismatch", "all sources must use the same base preset"
            )
    sections = sections_by_role(preset.crossover_regions)
    trims = {}
    linearization: dict[str, Any] = {}
    for role, source in sources.items():
        trims[role] = source.candidate.role_attenuations_db[role]
        entry = source.candidate.linearization.get(role, {})
        filters = entry.get("filters", []) if isinstance(entry, Mapping) else None
        if (
            not isinstance(filters, Sequence) or isinstance(filters, (str, bytes))
            or any(not isinstance(item, Mapping) for item in filters)
        ):
            raise CandidateBankRefusal("composition_filters_invalid", f"invalid filters for {role}")
        linearization[role] = {
            "filters": [dict(item) for item in filters],
            "headroom_cost_db": branch_headroom_db(
                filters, sections=sections.get(role, ()), trim_db=trims[role],
            ),
        }
    candidate = MeasuredCrossoverCandidate(
        program_id=COMPOSITION_KIND,
        analysis={
            "kind": COMPOSITION_KIND,
            "measurement_status": "unmeasured",
            "base": _source(base),
            "role_sources": {role: _source(source) for role, source in sources.items()},
            "alignment_source": _source(alignment),
            "blend_source": _source(blend),
            "expected_effect": expected_effect,
            "observation_refs": list(observation_refs),
            "rationale": rationale,
        },
        source_preset=preset,
        role_attenuations_db=trims,
        alignment=alignment.candidate.alignment,
        linearization=linearization,
        blend_correction=blend.candidate.blend_correction,
    )
    prove_candidate_config(candidate, compile_candidate_config(candidate, playback_device="null"))
    return candidate
