# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Join manifest-selected bass views for comparison and fitting."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from jasper.active_speaker.bass_comparison import bass_capture_context, compare_bass_takes, selected_take
from jasper.active_speaker.bass_table import fit_bass_table, level_key
from jasper.active_speaker.candidate_bank import load_candidate_artifact
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.measurement_programs import PURPOSE_BASS, baseline_scope

from ._common import ARTIFACT_BY_VIEW, RoundInputs, RoundSetRefused, SetTakes, default_out, read_run_manifest


def compare_sets(inputs: RoundInputs, args) -> tuple[dict[str, Any], Path]:
    set_ids: list[str] = args.set or []
    if len(set_ids) != 2:
        raise CrossoverV2Refused(code="bass_fit_pairs_unavailable")
    sets = {row["set_id"]: SetTakes(row["set_id"], row["capture_basis"], tuple(row["takes"]))
            for row in read_run_manifest(inputs)["sets"]}
    for set_id in set_ids:
        if set_id not in sets:
            raise RoundSetRefused("round_set_unknown", set_id=set_id, sets=list(sets))
    take_ids = [sets[set_id].take_id() for set_id in set_ids]
    paths = [default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW["bass"].artifact, set_id) for set_id in set_ids]
    takes = [selected_take(json.loads(path.read_text()), take_id) for path, take_id in zip(paths, take_ids)]
    return ({**compare_bass_takes(*takes, change=args.change), "source_views": list(map(str, paths))},
            default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW["bass-compare"].artifact, set_ids[-1]))


def fit_run(inputs: RoundInputs, args) -> dict[str, Any]:
    manifest = read_run_manifest(inputs)
    if manifest["run_id"] != args.run:
        raise CrossoverV2Refused({"run_id": manifest["run_id"]}, code="bass_fit_run_mismatch")
    descriptors = {}
    for path in args.candidate:
        candidate = load_candidate_artifact(path)
        if candidate is None or not candidate.bass_extension:
            raise CrossoverV2Refused({"path": str(path)}, code="bass_fit_candidate_unreadable")
        descriptors[candidate.fingerprint] = candidate.bass_extension
    baseline: dict[tuple, list[dict]] = defaultdict(list)
    candidates: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in manifest["sets"]:
        selected = SetTakes(row["set_id"], row["capture_basis"], tuple(row["takes"]))
        basis = selected.capture_basis
        level = level_key(basis, set_id=selected.set_id)
        path = default_out(inputs, args.round_dir, ARTIFACT_BY_VIEW["bass"].artifact, selected.set_id)
        view = json.loads(path.read_text())
        for entry in selected.takes:
            if not entry["selected"]:
                continue
            take = dict(selected_take(view, entry["take_id"]))
            if level_key(bass_capture_context(take), take_id=entry["take_id"]) != level:
                raise CrossoverV2Refused({"set_id": selected.set_id, "take_id": entry["take_id"]},
                                        code="bass_table_capture_context_changed")
            key = (level, basis.get("side"), basis.get("role"), doc_pose_key(take["record"]),
                   entry.get("repeat", 0), entry.get("stimulus_ordinal", 0))
            if basis.get("graph_scope") == baseline_scope(PURPOSE_BASS):
                baseline[key].append(take)
            else:
                candidate_id = basis.get("candidate_id")
                if candidate_id not in descriptors or take["record"].get("candidate_id") != candidate_id:
                    raise CrossoverV2Refused({"set_id": selected.set_id, "candidate_id": candidate_id},
                                            code="bass_fit_candidate_unreadable")
                candidates[candidate_id][key].append(take)
    if not candidates:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    target = json.loads(args.target.read_text())
    tables = []
    for candidate_id, takes in sorted(candidates.items()):
        if takes.keys() != baseline.keys() or any(len(rows) != 1 for rows in (*baseline.values(), *takes.values())):
            raise CrossoverV2Refused({"candidate_id": candidate_id,
                                     "baseline_take_ids": [row["record"]["take_id"] for rows in baseline.values() for row in rows],
                                     "candidate_take_ids": [row["record"]["take_id"] for rows in takes.values() for row in rows]},
                                    code="bass_fit_pairs_unavailable")
        tables.append(fit_bass_table([(baseline[key][0], rows[0]) for key, rows in takes.items()],
                                    candidate_id=candidate_id, descriptor=descriptors[candidate_id], target=target,
                                    tolerance_db=args.tolerance_db, reference_band_hz=tuple(args.reference_band_hz)))
    return {"schema": "jts_bass_run_table/1", "run_id": manifest["run_id"], "tables": tables}
