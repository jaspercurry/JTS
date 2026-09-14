# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Turn the first design-mark measurement into a candidate for explicit apply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, cast

from jasper.output_topology import OutputTopology
from jasper.atomic_io import atomic_write_json
from jasper.audio_measurement.program_analysis import ALIGNMENT_COMMITTED_FLAT_SUM
from jasper.json_fields import finite_float

from .candidate_bank import load_candidate_artifact, publish_authored_candidate
from .candidate_parts import candidate_from_design_draft, compose_candidate
from .crossover_v2.alignment_prescription import (
    ALIGNMENT_PRESCRIPTION_KIND, ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
    AlignmentPrescription, AlignmentPrescriptionRefused, alignment_delay_search_bounds_us, read_alignment_prescription,
)
from .crossover_v2.planning import alignment_to_candidate_fields
from .crossover_v2.round_inputs import SetTakes
from .measured_crossover_candidate import MeasuredCrossoverAlignment, MeasuredCrossoverCandidate
from .profile import required_driver_roles


def commissioning_candidate(
    topology: OutputTopology, draft: Mapping[str, Any], *, root: Path | None = None,
) -> MeasuredCrossoverCandidate:
    from .round_bank import DEFAULT_CAMPAIGN_ROOT  # lazy: round bank imports packet writer

    declared = candidate_from_design_draft(topology, draft)
    try:
        reference = json.loads(((root or DEFAULT_CAMPAIGN_ROOT) / "commissioning.json").read_text())
        measured = load_candidate_artifact(Path(reference["candidate_path"]))
    except (OSError, ValueError, KeyError, TypeError):
        return declared
    if (measured is not None and measured.analysis.get("base", {}).get("fingerprint") == declared.fingerprint
            and measured.analysis.get("evidence", {}).get("commissioning")):
        return measured
    return declared


def bank_commissioning_experiment(
    target: Path, manifest: Mapping[str, Any], sources: Mapping[str, Any],
) -> dict[str, Any]:
    if sources.get("applied_profile") or (manifest.get("incumbent") or {}).get("speaker"):
        return {}
    draft = sources.get("draft") or {}
    if not draft.get("topology"):
        return {"status": "unavailable", "reason": "commissioning_declaration_unavailable"}
    declared = candidate_from_design_draft(OutputTopology.from_mapping(draft["topology"]), draft)
    takes = [take for group in manifest.get("sets", ())
             if group.get("base") or group["capture_basis"].get("candidate_id") in (None, declared.fingerprint)
             for take in SetTakes.from_row(group).on_axis if (take.get("analysis") or {}).get("trim_db")]
    if not takes:
        return {"status": "unavailable", "reason": "commissioning_alignment_unavailable"}
    take = max(takes, key=lambda take: (take.get("timing") or {}).get("ended_s", 0))
    analysis = take["analysis"]
    alignment: dict[str, Any] = dict.fromkeys((
        "delay_us", "polarity", "trim_db", "alignment_status", "alignment_objective", "alignment_confidence",
    ))
    alignment.update((key, value) for key, value in analysis.items() if key in alignment)
    alignment.update(take_id=take["take_id"], record_id=take["artifacts"]["record_id"], pose=take["pose"])
    if alignment["delay_us"] is None or alignment["polarity"] not in ("normal", "inverted"):
        return {"status": "unavailable", "reason": "commissioning_alignment_unavailable", "alignment": alignment}
    base = publish_authored_candidate(declared, root=target.parent)
    sections: dict[str, Any] = {"driver": {"role_attenuations_db": alignment["trim_db"], "linearization": {}}}
    reason = ""
    if alignment["alignment_objective"] != ALIGNMENT_COMMITTED_FLAT_SUM:
        reason = alignment["alignment_objective"] or "commissioning_alignment_unavailable"
    elif (finite_float(alignment["alignment_confidence"]) or 0) <= 0:
        reason = "commissioning_alignment_unavailable"
    else:
        preset = declared.source_preset
        try:
            prescription = cast(AlignmentPrescription, read_alignment_prescription({
                "kind": ALIGNMENT_PRESCRIPTION_KIND, "artifact_schema_version": ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
                "delay_us": alignment["delay_us"], "basis_delay_us": analysis.get("alignment_seed_delay_us"),
                "polarity": "invert" if alignment["polarity"] == "inverted" else "keep",
                "basis_artifacts": [alignment["record_id"]],
            }, fc_hz=preset.crossover_regions[0].fc_hz if preset.crossover_regions else None,
               declared_bounds_us=alignment_delay_search_bounds_us(preset), way_count=preset.way_count))
            fields = alignment_to_candidate_fields(analysis, roles=required_driver_roles(preset.way_count))
            if fields[0] is None:
                reason = alignment["alignment_status"] or "commissioning_alignment_unavailable"
            else:
                sections["alignment"] = MeasuredCrossoverAlignment(*fields)
                alignment["prescription"] = prescription.to_dict()
        except AlignmentPrescriptionRefused as exc:
            reason = exc.reason
    result = {"status": "alignment_unmeasured" if reason else "awaiting_apply", "reason": reason, "alignment": alignment}
    candidate = compose_candidate(base, sections=sections,
        evidence={"commissioning": {"run_id": manifest["run_id"], **result}})
    banked = publish_authored_candidate(candidate, root=target.parent)
    atomic_write_json(target.parent / "commissioning.json", {"candidate_path": str(banked.path)})
    return {**result, "candidate_fingerprint": banked.fingerprint, "candidate_path": str(banked.path)}
