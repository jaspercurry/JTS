# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Manifest fixtures for retained rounds and legacy sidecar captures."""

import json
from pathlib import Path

from jasper.active_speaker.crossover_v2.measurement_context import capture_basis
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.round_inputs import round_artifact_dir, round_inputs
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME
from jasper.audio_measurement.evidence_identity import json_fingerprint


def write_manifest(round_dir: Path, *, program: str = "speaker", groups=None) -> dict:
    inputs = round_inputs(round_dir)
    directory, _ = round_artifact_dir(inputs.session_dir)
    if directory is None:
        directory = inputs.session_dir / "evidence/v1/artifacts/crossover_v2/wired-test"
        directory.mkdir(parents=True, exist_ok=True)
    if groups is None:
        records = [(row.path, record) for row, record in measurement_documents(inputs.session_dir)]
        if not records:
            records = [(str(path.relative_to(inputs.session_dir)), json.loads(path.read_text()))
                       for path in sorted(inputs.session_dir.glob("summed/*.json"))]
        groups = [manifest_set(records)]
    manifest = {"kind": "jts_run_manifest", "schema_version": 1, "program": program,
                "run_id": "fixture", "finalized": True, "status": "complete", "sets": groups}
    (directory / RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return manifest


def manifest_set(records, *, set_id=None, selected=None) -> dict:
    basis = capture_basis(records[0][1] if records else {})
    basis.pop("pose_kind", None)
    takes = []
    for path, record in records:
        take_id = record.get("take_id") or record.get("position_id") or path
        takes.append({"take_id": take_id, "pose": {"kind": record.get("pose_kind", "bearing"),
                      "deg": record.get("position_deg"), "elevation_deg": record.get("vertical_deg"),
                      "distance_m": record.get("mark_distance_m"), "seat_offset_m": record.get("seat_offset_m")},
                      "level": {key: basis.get(key) for key in ("level_db", "stimulus_dbfs", "loudness_volume_db", "program_id")},
                      "quality": {"status": "measured", "fault": None},
                      "artifacts": {"record_id": path, "wav_path": record.get("wav_path"), "wav_sha256": record.get("wav_sha256")},
                      "selected": selected is None or take_id in selected})
    return {"set_id": set_id or json_fingerprint(basis), "capture_basis": basis, "takes": takes}


def write_asked_poses(root: Path, state: dict, poses: list[dict]) -> Path:
    from jasper.active_speaker.run_manifest import RunManifest

    bundle = root / "sessions" / "asked-run"
    directory = bundle / "evidence/v1/artifacts/crossover_v2" / state["session_id"]
    directory.mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(state["session_id"], None, asked={"poses": poses})
    (directory / RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest.to_dict()))
    (bundle / "info.json").write_text(json.dumps({"session_id": bundle.name}))
    state["evidence"] = {"bundle_session_id": bundle.name}
    return bundle.parent
