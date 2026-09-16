# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Join manifest-selected bass views for comparison and fitting."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.bass_comparison import bass_capture_context, selected_take
from jasper.active_speaker.bass_table import fit_bass_table, level_key
from jasper.active_speaker.bass_fit import REFERENCE_BAND_HZ
from jasper.bass_extension.measurement import TARGET, TOLERANCE_DB
from jasper.active_speaker.candidate_bank import CandidateBankRefusal, find_banked_candidate, load_candidate_artifact
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.measurement_programs import PURPOSE_BASS, run_purpose

from jasper.atomic_io import atomic_write_json
from .round_view_artifacts import ARTIFACT_BY_VIEW
from .crossover_v2.round_inputs import SetTakes, default_out, read_run_manifest, round_artifact_dir, round_inputs


def fit_bass_rounds(round_dirs: Sequence[Path], *, candidates: Sequence[Path],
                    target: Mapping[str, Any], tolerance_db: float, reference_band_hz: tuple[float, float]) -> dict[str, Any]:
    descriptors = {}
    for path in candidates:
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
    candidate_takes: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))
    run_ids = []
    for root in round_dirs:
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
            if len(manifest["sets"]) == 1 and not path.is_file():
                path = default_out(inputs, root, ARTIFACT_BY_VIEW["bass"].artifact)
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
                bucket = candidate_takes[candidate_id] if candidate_id in descriptors else baseline
                bucket[key].append(take)
    if not descriptors and baseline:
        baseline_takes = [take for rows in baseline.values() for take in rows]
        candidate_ids = {take["record"]["candidate_id"] for take in baseline_takes}
        tables = [fit_bass_table([(take, take) for take in baseline_takes if take["record"]["candidate_id"] == candidate_id],
                                candidate_id=candidate_id, descriptor=None, target=target,
                                tolerance_db=tolerance_db, reference_band_hz=reference_band_hz)
                  for candidate_id in sorted(candidate_ids)]
        return {"schema": "jts_bass_run_table/1", "run_ids": run_ids, "tables": tables}
    if not candidate_takes:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    tables = []
    for candidate_id, takes in sorted(candidate_takes.items()):
        levels = {key[0] for key in takes}
        paired_base = {key: rows for key, rows in baseline.items() if key[0] in levels}
        if takes.keys() != paired_base.keys() or any(len(rows) != 1 for rows in (*paired_base.values(), *takes.values())):
            raise CrossoverV2Refused({"candidate_id": candidate_id,
                                     "baseline_take_ids": [row["record"]["take_id"] for rows in paired_base.values() for row in rows],
                                     "candidate_take_ids": [row["record"]["take_id"] for rows in takes.values() for row in rows]},
                                    code="bass_fit_pairs_unavailable")
        tables.append(fit_bass_table([(baseline[key][0], rows[0]) for key, rows in takes.items()],
                                    candidate_id=candidate_id, descriptor=descriptors[candidate_id], target=target,
                                    tolerance_db=tolerance_db, reference_band_hz=reference_band_hz))
    return {"schema": "jts_bass_run_table/1", "run_ids": run_ids, "tables": tables}


def join_bass_rounds(round_dirs: Sequence[Path], *, candidates: Sequence[Path]) -> Path:
    """Join the sequence after banking its views, in the last round's packet."""
    if not round_dirs:
        raise CrossoverV2Refused(code="bass_fit_inputs_missing")
    payload = fit_bass_rounds(round_dirs, candidates=candidates, target=TARGET,
                              tolerance_db=TOLERANCE_DB, reference_band_hz=REFERENCE_BAND_HZ)
    directory, reason = round_artifact_dir(round_inputs(round_dirs[-1]).session_dir)
    if directory is None:
        raise CrossoverV2Refused(reason, code="bass_fit_run_mismatch")
    path = directory / ARTIFACT_BY_VIEW["bass-fit-table"].artifact
    atomic_write_json(path, payload)
    return path
