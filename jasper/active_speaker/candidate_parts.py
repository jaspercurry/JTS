# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compose candidate interventions without carrying their parents' measurement claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .branch_chain import branch_headroom_db, sections_by_role
from .candidate_bank import BankedCandidate, CandidateBankRefusal
from .crossover_v2.room_prescription import ROOM_MEDIAN_FIELD
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    candidate_room_peqs,
    compile_candidate_config,
    prove_candidate_config,
)

COMPOSITION_KIND = "jts_candidate_composition"
_INHERIT = object()


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
    room_correction: Mapping[str, Any] | None = None,
    room_prescription_sha256: str = "",
    room_measured_basis: Mapping[str, Any] | None = None,
    bass_extension: Mapping[str, Any] | object = _INHERIT,
) -> MeasuredCrossoverCandidate:
    """Replace each selected role's filters and trim; retain other base settings.

    ``room_correction`` is a room prescription door's own output, carried onto
    the child whole: the door is the only writer, and the candidate re-derives
    the room layer's limits from it at construction. It cannot travel with a
    tune change -- a room set is fitted to the tune it was measured through, so
    ``roles``, ``alignment`` or ``blend`` beside it is refused; and a base's own
    room set is dropped rather than inherited, disclosed as
    ``analysis["room_source"]["dropped_from_base"]`` (ADR-0256: a room session
    under a moved tune is disclosed-stale).
    """
    if room_correction and (roles or alignment is not None or blend is not None):
        raise CandidateBankRefusal(
            "composition_room_with_tune_change",
            "a room set is measured through one tune: compose the tune change "
            "first, then prescribe the room against a round that played it",
        )
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
    tune_changed = bool(roles or alignment is not None or blend is not None)
    room = dict(
        room_correction
        if room_correction is not None
        else ({} if tune_changed else base.candidate.room_correction)
    )
    bass = dict(
        base.candidate.bass_extension
        if bass_extension is _INHERIT and not tune_changed
        else ({} if bass_extension is _INHERIT else bass_extension)
    )
    analysis: dict[str, Any] = {
        "kind": COMPOSITION_KIND,
        "measurement_status": "unmeasured",
        "base": _source(base),
        "role_sources": {role: _source(source) for role, source in sources.items()},
        "alignment_source": _source(alignment),
        "blend_source": _source(blend),
        "expected_effect": expected_effect,
        "observation_refs": list(observation_refs),
        "rationale": rationale,
    }
    if room:
        measured_basis = dict(room_measured_basis or {})
        measured_candidate = measured_basis.get("speaker_candidate_id") or measured_basis.get("candidate_id")
        analysis["room_source"] = {
            "prescription_sha256": room_prescription_sha256,
            ROOM_MEDIAN_FIELD: room["basis"][ROOM_MEDIAN_FIELD],
            "measured_basis": measured_basis,
            "base_match": (
                "unknown" if not measured_candidate else
                "match" if measured_candidate == base.fingerprint else "different"
            ),
        }
    elif tune_changed and base.candidate.room_correction:
        analysis["room_source"] = {"dropped_from_base": base.fingerprint}
    candidate = MeasuredCrossoverCandidate(
        program_id=COMPOSITION_KIND,
        analysis=analysis,
        source_preset=preset,
        role_attenuations_db=trims,
        alignment=alignment.candidate.alignment,
        linearization=linearization,
        blend_correction=blend.candidate.blend_correction,
        room_correction=room,
        bass_extension=bass,
    )
    # The room set is emitted here so the emitter's headroom charge runs at
    # compose rather than at apply.
    prove_candidate_config(candidate, compile_candidate_config(
        candidate, playback_device="null", room_peqs=candidate_room_peqs(candidate),
    ))
    return candidate
