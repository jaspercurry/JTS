# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Turn the first design-mark measurement into a candidate for explicit apply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jasper.output_topology import OutputTopology
from jasper.atomic_io import atomic_write_json

from .candidate_bank import load_candidate_artifact, publish_authored_candidate
from .candidate_parts import candidate_from_design_draft, compose_candidate
from .crossover_v2.alignment_prescription import AlignmentPrescription
from .crossover_v2.round_inputs import SetTakes
from .measured_crossover_candidate import MeasuredCrossoverCandidate


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
    take = takes[-1]
    analysis = take["analysis"]
    alignment = {key: analysis.get(key) for key in (
        "delay_us", "polarity", "trim_db", "alignment_objective", "alignment_confidence",
    )}
    alignment.update(take_id=take["take_id"], record_id=take["artifacts"]["record_id"], pose=take["pose"])
    if alignment["delay_us"] is None or alignment["polarity"] not in ("normal", "inverted"):
        return {"status": "unavailable", "reason": "commissioning_alignment_unavailable", "alignment": alignment}
    base = publish_authored_candidate(declared, root=target.parent)
    candidate = compose_candidate(base, sections={
        "driver": {"role_attenuations_db": alignment["trim_db"], "linearization": {}},
        "alignment": AlignmentPrescription(
            delay_us=alignment["delay_us"], basis_delay_us=alignment["delay_us"],
            polarity="invert" if alignment["polarity"] == "inverted" else "keep",
            basis_artifacts=(alignment["record_id"],),
        ),
    }, evidence={"commissioning": {"run_id": manifest["run_id"], "alignment": alignment}})
    banked = publish_authored_candidate(candidate, root=target.parent)
    atomic_write_json(target.parent / "commissioning.json", {"candidate_path": str(banked.path)})
    return {"status": "awaiting_apply", "candidate_fingerprint": banked.fingerprint,
            "candidate_path": str(banked.path), "alignment": alignment}
