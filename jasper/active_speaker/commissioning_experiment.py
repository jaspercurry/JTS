# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Turn the first design-mark measurement into a candidate for explicit apply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from jasper.output_topology import OutputTopology
from jasper.atomic_io import atomic_write_json
from jasper.audio_measurement.program_analysis.model import TIMING_MEASURED

from .candidate_bank import load_candidate_artifact, publish_authored_candidate
from .candidate_parts import candidate_from_design_draft, compose_candidate
from .crossover_v2.alignment_prescription import (
    ALIGNMENT_PRESCRIPTION_KIND, ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
    PRESCRIPTION_OUT_OF_LOBE, AlignmentPrescription, AlignmentPrescriptionRefused, alignment_delay_search_bounds_us, read_alignment_prescription,
)
from .crossover_v2.planning import alignment_to_candidate_fields
from .alignment_evidence import commissioning_alignment
from .measured_crossover_candidate import MeasuredCrossoverAlignment, MeasuredCrossoverCandidate
from .profile import required_driver_roles


def commissioning_experiment_summary(candidate: MeasuredCrossoverCandidate) -> dict[str, Any]:
    packet = candidate.analysis.get("evidence", {}).get("commissioning") or {}
    return {
        "candidate_fingerprint": candidate.fingerprint if packet else None,
        "alignment": {"status": TIMING_MEASURED if packet.get("status") == "awaiting_apply" else packet.get("status", "declared"),
                      "reason": packet.get("reason") or None},
    }


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
    target: Path, manifest: Mapping[str, Any], sources: Mapping[str, Any], alignments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if sources.get("applied_profile") or (manifest.get("incumbent") or {}).get("speaker"):
        return {}
    draft = sources.get("draft") or {}
    if not draft.get("topology"):
        return {"status": "unavailable", "reason": "commissioning_declaration_unavailable"}
    declared = candidate_from_design_draft(OutputTopology.from_mapping(draft["topology"]), draft)
    selected = commissioning_alignment(alignments, declared.fingerprint)
    if selected is None:
        return {"status": "unavailable", "reason": "commissioning_alignment_unavailable"}
    alignment = dict(selected)
    committed = alignment["committed"]
    if committed["delay_us"] is None or committed["polarity"] not in ("normal", "inverted"):
        return {"status": "unavailable", "reason": "commissioning_alignment_unavailable", "alignment": alignment}
    base = publish_authored_candidate(declared, root=target.parent)
    sections: dict[str, Any] = {"driver": {"role_attenuations_db": alignment["trim_db"], "linearization": {}}}
    reason = ""
    if alignment.get("timing_verdict") != TIMING_MEASURED:
        reason = alignment["objective"] or "commissioning_alignment_unavailable"
    else:
        preset = declared.source_preset
        try:
            prescription = cast(AlignmentPrescription, read_alignment_prescription({
                "kind": ALIGNMENT_PRESCRIPTION_KIND, "artifact_schema_version": ALIGNMENT_PRESCRIPTION_SCHEMA_VERSION,
                "delay_us": committed["delay_us"], "basis_delay_us": alignment["seed"]["delay_us"],
                "polarity": "invert" if committed["polarity"] == "inverted" else "keep",
                "basis_artifacts": [alignment["record_id"]],
            }, fc_hz=preset.crossover_regions[0].fc_hz if preset.crossover_regions else None,
               declared_bounds_us=alignment_delay_search_bounds_us(preset), way_count=preset.way_count))
            fields = alignment_to_candidate_fields({**committed, "alignment_status": alignment["status"]}, roles=required_driver_roles(preset.way_count))
            if prescription.out_of_lobe is not False:
                reason = PRESCRIPTION_OUT_OF_LOBE if prescription.out_of_lobe else "commissioning_alignment_unavailable"
            elif fields[0] is None:
                reason = alignment["status"] or "commissioning_alignment_unavailable"
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
