# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Manifest fixtures for retained rounds."""

import json
import os
from pathlib import Path

from jasper.active_speaker.crossover_v2.measurement_context import capture_basis
from jasper.active_speaker.crossover_v2.position_cycle import take_artifact_path
from jasper.active_speaker.crossover_v2.record_index import measurement_documents
from jasper.active_speaker.crossover_v2.round_inputs import round_artifact_dir, round_inputs
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME, set_basis
from jasper.audio_measurement.bundles import ARTIFACT_MANIFEST_NAME
from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.platform.json_fields import canonical_json_bytes, sha256_file


#: The layers an in-room base take plays cleared (ADR-0421).
IN_ROOM_CLEARED = ("bass_extension", "room_correction")


def write_manifest(round_dir: Path, *, program: str = "speaker", groups=None, probe: bool = False,
                   cleared_layers=()) -> dict:
    return write_bundle_manifest(round_inputs(round_dir).session_dir, program=program, groups=groups, probe=probe,
                                 cleared_layers=cleared_layers)


def write_bundle_manifest(
    session_dir: Path, *, program: str = "speaker", groups=None, selected=None, refused=(), probe: bool = False,
    cleared_layers=(),
) -> dict:
    """The round's finalized run manifest: ``groups``, else one set of every
    banked take (:func:`manifest_set`), each played with ``cleared_layers``. With
    ``probe``, the run's probe of the first set's first take banks first, in a
    set of its own (:func:`probe_set`)."""
    directory, _ = round_artifact_dir(session_dir)
    if directory is None:
        directory = session_dir / "evidence/v1/artifacts/crossover_v2/wired-test"
        directory.mkdir(parents=True, exist_ok=True)
    if groups is None:
        records = [(row.path, record) for row, record in measurement_documents(session_dir)]
        groups = [manifest_set(records, selected=selected, refused=refused, cleared_layers=cleared_layers)]
    if probe and groups:
        groups = [probe_set(groups[0]), *groups]
    manifest = {"kind": "jts_run_manifest", "schema_version": 5, "preset": program,
                "run_id": "fixture", "finalized": True, "status": "complete", "honoured": {"retakes": 0},
                "sets": [_banked(session_dir, index, group) for index, group in enumerate(groups)]}
    (directory / RUN_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return manifest


def probe_set(group: dict) -> dict:
    """The set a run's probe of ``group``'s first take banks (ADR-0403 §4): the
    same basis marked a level probe, and one take the run never selects, banked
    as its own record."""
    first = group["takes"][0]
    return {**group, "set_id": f"probe-{group['set_id']}", "base": False,
            "capture_basis": {**group["capture_basis"], "level_probe": True},
            "takes": [{"take_id": f"probe-{first['take_id']}", "phase": first.get("phase"),
                       "pose": first.get("pose"), "selected": False}]}


def _banked(session_dir: Path, index: int, group: dict) -> dict:
    """``group`` as the executor writes it: each row points at its take's record
    (ADR-0395). A row that already points at one is kept. A hand-built take
    (one with no ``artifacts``) is banked as its own record, which the scan of
    banked takes never finds. A row :func:`manifest_set` built from a banked
    record lends that record the facts the executor banks on it, where the
    record lacks them, and a record named from the bundle is named from the
    artifacts root."""
    root = take_artifact_path(session_dir, "")
    takes = []
    for number, take in enumerate(group["takes"]):
        if "record_id" in take:
            record_id = take["record_id"]
        elif "artifacts" not in take:
            record_id = f"fixture/{index}/{number}.json"
            path = take_artifact_path(session_dir, record_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({key: value for key, value in take.items() if key != "selected"}))
        elif record_id := take["artifacts"]["record_id"]:
            if not (root / record_id).is_file() and (session_dir / record_id).is_file():
                record_id = os.path.relpath(session_dir / record_id, root)
            if (root / record_id).is_file():
                _lend(session_dir, root / record_id, {key: value for key, value in take.items()
                                                      if key not in {"take_id", "selected", "artifacts"}})
        takes.append({"take_id": take["take_id"], "record_id": record_id, "selected": take.get("selected", False)})
    return {**group, "takes": takes}


#: The record keys a fixture manifest lent, which the next one it writes lends afresh.
_LENT = "fixture_lent"


def _lend(session_dir: Path, path: Path, facts: dict) -> None:
    """Bank ``facts`` on the record at ``path`` where it lacks them or an earlier
    fixture manifest lent them, so a take re-banked with another flat pose reads
    that pose, in the encoding the record was written in; the bundle's artifact
    identity of the record stays true."""
    raw = path.read_bytes()
    record = json.loads(raw)
    if not isinstance(record, dict):
        return
    lent = set(record.get(_LENT, ()))
    fresh = {key: value for key, value in facts.items() if key not in record or key in lent}
    if all(record.get(key) == value for key, value in fresh.items()):
        return
    try:
        canonical = canonical_json_bytes(record) == raw
    except ValueError:
        canonical = False
    record = {**record, **fresh, _LENT: sorted(lent | fresh.keys())}
    path.write_bytes(canonical_json_bytes(record) if canonical else json.dumps(record).encode())
    listing = session_dir / ARTIFACT_MANIFEST_NAME
    if not listing.is_file():
        return
    manifest = json.loads(listing.read_text())
    relative = Path(os.path.relpath(path, session_dir)).as_posix()
    for entry in manifest.get("artifacts", ()):
        if entry.get("path") == relative:
            entry.update(sha256=sha256_file(path), byte_size=path.stat().st_size)
    listing.write_text(json.dumps(manifest))


def own_record(row: dict, record: dict, **fields) -> dict:
    """A take built from ``row`` and the ``record`` it points at, with ``fields``,
    that :func:`write_bundle_manifest` banks as its own record."""
    return {**record, **{key: value for key, value in row.items() if key != "artifacts"}, **fields}


def manifest_set(records, *, set_id=None, selected=None, refused=(), cleared_layers=()) -> dict:
    """One set's takes, each with the facts the executor banks on its record
    (:func:`_banked` lends them), ``cleared_layers`` among them when given: every
    take selected unless ``selected`` names the ones that are; a ``refused`` take
    is never selected, as the executor writes it."""
    basis = set_basis(records[0][1] if records else {})
    takes = []
    for path, record in records:
        take_id = record.get("take_id") or record.get("position_id") or path
        played = capture_basis(record)
        takes.append({"take_id": take_id, "phase": record.get("phase"),
                      "pose": {"kind": record.get("pose_kind", "bearing"),
                      "azimuth_deg": record.get("position_deg"), "elevation_deg": record.get("vertical_deg"),
                      "distance_m": record.get("mark_distance_m"), "seat_offset_m": record.get("seat_offset_m")},
                      "level": {**{key: played.get(key) for key in ("level_db", "stimulus_dbfs", "stimulus_id")}, "alignment": {}},
                      "artifacts": {"record_id": path},
                      **({"cleared_layers": list(cleared_layers)} if cleared_layers else {}),
                      "selected": take_id not in refused and (selected is None or take_id in selected)})
    return {"set_id": set_id or json_fingerprint(basis), "capture_basis": basis, "takes": takes}
