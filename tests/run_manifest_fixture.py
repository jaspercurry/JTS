# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Manifest fixtures for retained rounds and legacy sidecar captures."""

import json
import os
from pathlib import Path

from jasper.active_speaker.crossover_v2.measurement_context import capture_basis
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.round_inputs import round_artifact_dir, round_inputs
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.audio_measurement.evidence_identity import json_fingerprint


def write_manifest(round_dir: Path, *, program: str = "speaker", groups=None) -> dict:
    return write_bundle_manifest(round_inputs(round_dir).session_dir, program=program, groups=groups)


def write_bundle_manifest(
    session_dir: Path, *, program: str = "speaker", groups=None, selected=None, refused=(),
) -> dict:
    """The round's finalized run manifest: ``groups``, else one set of every
    banked take (:func:`manifest_set`)."""
    directory, _ = round_artifact_dir(session_dir)
    if directory is None:
        directory = session_dir / "evidence/v1/artifacts/crossover_v2/wired-test"
        directory.mkdir(parents=True, exist_ok=True)
    if groups is None:
        records = [(row.path, record) for row, record in measurement_documents(session_dir)]
        if not records:
            records = [(str(path.relative_to(session_dir)), json.loads(path.read_text()))
                       for path in sorted(session_dir.glob("summed/*.json"))]
        groups = [manifest_set(records, selected=selected, refused=refused)]
    manifest = {"kind": "jts_run_manifest", "schema_version": 1, "program": program,
                "run_id": "fixture", "finalized": True, "status": "complete",
                "sets": [_banked(session_dir, index, group) for index, group in enumerate(groups)]}
    (directory / RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return manifest


def _banked(session_dir: Path, index: int, group: dict) -> dict:
    """``group`` with each take pointing where the join reads its record: a
    hand-built take (one with no ``artifacts``) is banked as its own record,
    which the scan of banked takes never finds, and a legacy sidecar named from
    the bundle is named from the artifacts root, as the executor names a record."""
    root = take_artifact_path(session_dir, "")
    takes = []
    for number, take in enumerate(group["takes"]):
        if "artifacts" not in take:
            record_id = f"fixture/{index}/{number}.json"
            path = take_artifact_path(session_dir, record_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({key: value for key, value in take.items() if key != "selected"}))
            take = {**take, "artifacts": {"record_id": record_id}}
        elif (record_id := take["artifacts"]["record_id"]) and not (root / record_id).is_file() \
                and (session_dir / record_id).is_file():
            take = {**take, "artifacts": {**take["artifacts"], "record_id": os.path.relpath(session_dir / record_id, root)}}
        takes.append(take)
    return {**group, "takes": takes}


def own_record(row: dict, record: dict, **fields) -> dict:
    """A take built from ``row`` and the ``record`` it points at, with ``fields``,
    that :func:`write_bundle_manifest` banks as its own record."""
    return {**record, **{key: value for key, value in row.items() if key != "artifacts"}, **fields}


def manifest_set(records, *, set_id=None, selected=None, refused=()) -> dict:
    """One set's rows: every take selected unless ``selected`` names the ones
    that are; a ``refused`` take is never selected, as the executor writes it."""
    basis = capture_basis(records[0][1] if records else {})
    basis.pop("pose_kind", None)
    takes = []
    for path, record in records:
        take_id = record.get("take_id") or record.get("position_id") or path
        takes.append({"take_id": take_id, "phase": record.get("phase"),
                      "pose": {"kind": record.get("pose_kind", "bearing"),
                      "deg": record.get("position_deg"), "elevation_deg": record.get("vertical_deg"),
                      "distance_m": record.get("mark_distance_m"), "seat_offset_m": record.get("seat_offset_m")},
                      "level": {key: basis.get(key) for key in ("level_db", "stimulus_dbfs", "stimulus_id")},
                      "quality": {"status": "refused" if take_id in refused else "measured"},
                      "artifacts": {"record_id": path, "wav_path": record.get("wav_path"), "wav_sha256": record.get("wav_sha256")},
                      "selected": take_id not in refused and (selected is None or take_id in selected)})
    return {"set_id": set_id or json_fingerprint(basis), "capture_basis": basis, "takes": takes}
