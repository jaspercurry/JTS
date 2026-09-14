# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Join manifest-selected bass views for comparison and fitting."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from jasper.active_speaker.bass_comparison import bass_capture_context, selected_take
from jasper.active_speaker.bass_table import fit_bass_table, level_key
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate, load_candidate_artifact
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.measurement_programs import PURPOSE_BASS, run_purpose

from ._common import ARTIFACT_BY_VIEW, SetTakes, default_out, read_run_manifest, round_artifact_dir, round_inputs


def fit_run(args) -> dict[str, Any]:
    descriptors = {}
    for path in args.candidate:
        candidate = load_candidate_artifact(path)
        if candidate is None:
            try:
                candidate = find_banked_candidate(str(path)).candidate
            except CandidateBankRefusal as exc:
                raise CrossoverV2Refused({"path": str(path)}, code="bass_fit_candidate_unreadable") from exc
        if candidate is None or not candidate.bass_extension:
            raise CrossoverV2Refused({"path": str(path)}, code="bass_fit_candidate_unreadable")
        descriptors[candidate.fingerprint] = candidate.bass_extension
    baseline: dict[tuple, list[dict]] = defaultdict(list)
    candidates: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))
    run_ids = []
    for root in args.round_dir:
        inputs = round_inputs(root)
        manifest = read_run_manifest(inputs)
        directory, _ = round_artifact_dir(inputs.session_dir)
        if directory is None or directory.name != manifest["run_id"]:
            raise CrossoverV2Refused({"run_id": manifest["run_id"], "round_dir": str(root)},
                                    code="bass_fit_run_mismatch")
        run_ids.append(manifest["run_id"])
        purpose = run_purpose(manifest.get("program"))
        for row in manifest["sets"]:
            selected = SetTakes.from_row(row)
            entries = [take for take in selected.takes if take["selected"]
                       and take.get("phase") == PHASE_LATERAL
                       and take.get("purpose", purpose) == PURPOSE_BASS]
            if not entries:
                continue
            basis = selected.capture_basis
            level = level_key(basis, set_id=selected.set_id)
            path = default_out(inputs, root, ARTIFACT_BY_VIEW["bass"].artifact, selected.set_id)
            view = json.loads(path.read_text())
            for entry in entries:
                take = dict(selected_take(view, entry["take_id"]))
                if level_key(bass_capture_context(take), take_id=entry["take_id"]) != level:
                    raise CrossoverV2Refused({"set_id": selected.set_id, "take_id": entry["take_id"]},
                                            code="bass_table_capture_context_changed")
                key = (level, basis.get("side"), basis.get("role"), doc_pose_key(take["record"]),
                       entry.get("repeat", 0), entry.get("stimulus_ordinal", 0))
                candidate_id = basis.get("candidate_id")
                if take["record"].get("candidate_id") != candidate_id:
                    raise CrossoverV2Refused({"set_id": selected.set_id, "candidate_id": candidate_id},
                                            code="bass_fit_candidate_unreadable")
                bucket = candidates[candidate_id] if candidate_id in descriptors else baseline
                bucket[key].append(take)
    if not candidates:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    target = json.loads(args.target.read_text())
    tables = []
    for candidate_id, takes in sorted(candidates.items()):
        levels = {key[0] for key in takes}
        paired_base = {key: rows for key, rows in baseline.items() if key[0] in levels}
        if takes.keys() != paired_base.keys() or any(len(rows) != 1 for rows in (*paired_base.values(), *takes.values())):
            raise CrossoverV2Refused({"candidate_id": candidate_id,
                                     "baseline_take_ids": [row["record"]["take_id"] for rows in paired_base.values() for row in rows],
                                     "candidate_take_ids": [row["record"]["take_id"] for rows in takes.values() for row in rows]},
                                    code="bass_fit_pairs_unavailable")
        tables.append(fit_bass_table([(baseline[key][0], rows[0]) for key, rows in takes.items()],
                                    candidate_id=candidate_id, descriptor=descriptors[candidate_id], target=target,
                                    tolerance_db=args.tolerance_db, reference_band_hz=tuple(args.reference_band_hz)))
    return {"schema": "jts_bass_run_table/1", "run_ids": run_ids, "tables": tables}
